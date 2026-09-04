# Architecture and data semantics

## Security boundary

SSD health collection and HTTP serving have different privilege needs, so they
run as separate processes and separate Docker targets.

```text
┌─────────┐       TCP 127.0.0.1:8787       ┌────────────────────────────┐
│ Browser │ ──────────────────────────────> │ web                        │
└─────────┘                                 │ UID 10001, no capabilities │
                                            │ read-only root filesystem  │
                                            └─────────────┬──────────────┘
                                                          │ HTTP over UDS
                                                          │ collector.sock
                                            ┌─────────────▼──────────────┐
                                            │ collector                  │
                                            │ device privileges          │
                                            │ network_mode: none          │
                                            ├─────────────┬──────────────┤
                                            │ lsblk / smartctl / nvme    │
                                            │ SQLite WAL history         │
                                            └─────────────┴──────────────┘
```

The collector's FastAPI documentation and OpenAPI routes are disabled. Its
0660 Unix socket is owned by the shared numeric group 10001 and mounted
read-only in the web container. The web service validates drive IDs and proxies
a fixed set of read-only routes; users cannot supply a device path or command.

## Collection sequence

One background cycle performs these steps:

1. Run `lsblk --nodeps --json --bytes` for the block-device inventory.
2. Run `smartctl --scan-open --json` for protocol and USB bridge types.
3. Merge entries by validated `/dev/...` path.
4. Query each drive concurrently with `smartctl -a --device TYPE PATH --json`.
5. For native NVMe devices, query `nvme id-ctrl PATH --output-format=json` for
   warning and critical temperature thresholds.
6. Normalize finite, bounded fields and decode all eight documented smartctl
   status bits.
7. Record one observation per drive per minute, calculate eligible projections,
   and atomically replace the durable latest snapshot.

Commands are passed as argument vectors to `subprocess.run` with `shell=False`
and a 12-second timeout. Device names and smartctl types are allowlist-validated
before use.

## Drive identity

Historical trends must not silently join different hardware. IDs therefore
prefer identifiers in this order:

1. world-wide name (WWN);
2. serial number; or
3. transport, current device path, model, and size as a last-resort fallback.

The fallback is marked `path-fallback`. Its history can still be displayed, but
the app refuses to calculate a lifetime projection because Linux device names
can change after reboot or reconnection. Identity is enriched from smartctl
when `lsblk` omits a serial or WWN.

## Endurance semantics

### NVMe

NVMe `percentage_used` is a controller estimate of rated endurance consumed.
The API retains valid values from 0 through 255 and normalizes the displayed
remaining value to the 0–100 range:

```text
remaining = clamp(100 - percentage_used, 0, 100)
```

The collector also exposes critical-warning bits, available spare and its
threshold, media errors, error-log entries, unsafe shutdowns, power-on hours,
and data units written. Any non-zero NVMe critical-warning mask marks normalized
SMART state as unhealthy even if a generic pass field says otherwise.

### SATA

ATA SMART attribute IDs and normalized values are vendor-specific. The parser
uses an exact normalized-name allowlist for fields such as `SSD_Life_Left` and
`Media_Wearout_Indicator`. Substring matches and generic
`Wear_Leveling_Count` values are rejected. SATA endurance is useful as a
vendor-reported display value but is not used for time projection.

## Projection model

The projection operates on the standardized NVMe integer percentage counter.
It requires a stable identity, at least 14 days, at least 2% wear, and at least
three distinct counter values. If the counter decreases, only observations
after the latest decrease are considered.

For the usable segment:

```text
rate = wear_delta / elapsed_days
central_days = remaining / rate
fast_rate = (wear_delta + 1) / elapsed_days
slow_rate = (wear_delta - 1) / elapsed_days
range = remaining / fast_rate ... remaining / slow_rate
```

The ±1 term reflects the counter's coarse quantization. Confidence labels are
deterministic:

| Confidence | Minimum evidence |
| --- | --- |
| Low | Projection eligibility only |
| Medium | 30 days and 3% observed wear |
| High | 60 days, 5% observed wear, and 30 observations |

This extrapolates when the drive could consume its rated endurance if the
observed workload continues. It does not model random hardware failure,
retention loss, workload changes, write amplification, or manufacturer-specific
behavior.

## Persistence and concurrency

SQLite stores one-minute observations for 90 days and one latest-successful
snapshot. Connections use WAL mode, `synchronous=NORMAL`, a five-second busy
timeout, value constraints, and a ten-second connection timeout. Retention is
pruned at most once per day.

The monitor has separate state and refresh locks. Only one hardware cycle can
run at a time; concurrent requests receive the current snapshot. Manual forced
refreshes have their own minimum interval. Per-drive commands run in a bounded
thread pool, and an unexpected failure is converted into an unavailable record
for that drive instead of aborting the inventory.

## Failure behavior

| Failure | Result |
| --- | --- |
| Inventory command fails before any success | Drives endpoint returns HTTP 503 |
| Inventory fails after a success | Last snapshot is returned with `stale: true` |
| One drive command fails | Other drives remain available; failed drive has `collector_errors` |
| smartctl health bits are non-zero | Valid JSON is kept and health warnings are decoded |
| Database is unavailable or corrupt | Readiness fails with database details |
| Collector task stops | Readiness fails |
| Collector socket is unavailable | Web health remains reachable but reports degraded |
| Browser refresh/history request fails | Previously rendered data remains visible |

The snapshot carries its age, last attempt/success timestamps, last collector
error, and consecutive failure count. That makes stale data visible rather than
silently presenting it as current.
