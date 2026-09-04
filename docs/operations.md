# Operations guide

## Start and inspect the deployment

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f collector web
```

The dashboard listens on `127.0.0.1:8787`. The collector publishes no TCP port
and has `network_mode: none`; its only application interface is a Unix socket
in the `ssd-life-runtime` volume.

The services have distinct responsibilities:

| Service | Runtime authority | Persistent access |
| --- | --- | --- |
| `collector` | Root/device access, privileged by default, no network | Read/write history and Unix socket |
| `web` | UID 10001, all capabilities dropped, read-only root | Read-only access to Unix socket volume |

## Configuration

Collector settings are environment variables in `docker-compose.yml`:

| Variable | Default | Accepted range | Purpose |
| --- | ---: | ---: | --- |
| `DATABASE_PATH` | `/data/ssd-life.sqlite3` | Absolute or process-relative path | SQLite database location |
| `COLLECTOR_SOCKET` | `/run/ssd-life/collector.sock` | Absolute path | Internal Unix socket |
| `COLLECTION_INTERVAL_SECONDS` | `60` | 15–86,400 | Background collection cadence |
| `STALE_AFTER_SECONDS` | `180` | 30–604,800 | Age after which a snapshot is stale |
| `FORCE_MIN_INTERVAL_SECONDS` | `30` | 5–3,600 | Minimum time between manual hardware refreshes |

Invalid numeric values fall back to defaults and appear in
`configuration_warnings` on the health endpoint. Keep the stale threshold
comfortably above the collection interval.

## Device permissions and hardening

`privileged: true` is a compatibility default, not a security requirement of
the web UI. It grants the collector broad host authority and should be treated
accordingly.

On a known Linux host, test a narrower collector configuration by removing
`privileged: true` and adding only the physical devices it needs:

```yaml
services:
  collector:
    devices:
      - /dev/nvme0:/dev/nvme0
      - /dev/nvme0n1:/dev/nvme0n1
    cap_add:
      - SYS_RAWIO
```

Some NVMe kernels/configurations also require `SYS_ADMIN`; SATA and USB bridge
requirements vary. There is no universal capability set. Validate all three
commands inside the resulting container before adopting the narrower policy:

```sh
docker compose exec collector lsblk --nodeps --json --bytes \
  --output NAME,MODEL,SERIAL,WWN,SIZE,TYPE,TRAN,ROTA
docker compose exec collector smartctl --scan-open --json
docker compose exec collector smartctl -a /dev/nvme0n1 --json
docker compose exec collector nvme id-ctrl /dev/nvme0n1 --output-format=json
```

Keep the dashboard on localhost unless an authenticated reverse proxy, TLS,
firewall, and trusted management network are in place. Serial numbers and
storage health are sensitive inventory data.

## Health and readiness

```sh
curl -sS http://127.0.0.1:8787/api/health | jq
curl -fsS http://127.0.0.1:8787/api/ready | jq
curl -fsS http://127.0.0.1:8787/api/drives | jq
```

Health includes:

- web-to-collector reachability;
- required tool availability;
- background task state;
- first-snapshot and freshness state;
- collection timestamps and failures; and
- SQLite quick-check, schema, and write-lock state.

`/api/health` returns HTTP 200 with `status: degraded` so monitoring can still
read the diagnosis. `/api/ready` returns HTTP 503 whenever serving current drive
data is unsafe or impossible.

## Stale data behavior

After every successful cycle, the complete normalized snapshot is persisted.
If a later inventory or database operation fails, the API and dashboard retain
that last reading and clearly mark it stale. Check these fields:

```text
stale
snapshot_age_seconds
last_success_at
last_attempt_at
collector_error
consecutive_failures
```

A per-drive SMART error is different from a global stale snapshot: the cycle
can still be fresh while one drive contains `collector_errors`.

## Data, backup, and restore

History is held in the named `ssd-life-data` volume. Normal image rebuilds and
`docker compose down` preserve it. This command permanently deletes it:

```sh
docker compose down --volumes
```

For a consistent backup, stop only the collector, copy the SQLite database,
then start it again:

```sh
docker compose stop collector
docker run --rm \
  -v ssd-life-data:/data:ro \
  -v "$PWD":/backup \
  alpine cp /data/ssd-life.sqlite3 /backup/ssd-life.sqlite3
docker compose start collector
```

Also copy `ssd-life.sqlite3-wal` and `ssd-life.sqlite3-shm` if you deliberately
back up while the collector is running. Stopping it first is safer.

To restore, stop the deployment, copy the database into a fresh named volume,
then start the services. Preserve ownership that allows the root collector to
write the file.

## Troubleshooting

### No disks are listed

Compare host and collector `lsblk` output. Confirm block devices are visible in
the container and that `type` is `disk`. A rotating disk can be listed for
SMART health, but only a non-rotating disk is treated as an SSD.

### A USB SSD is missing or SMART is unknown

Run `smartctl --scan-open --json` on the host. The enclosure must pass SMART
commands through and report a usable device type such as `sat`. The app uses
that type automatically; it cannot recover fields that the bridge hides.

### Endurance is unavailable

NVMe should expose `percentage_used`. SATA has no universal endurance field, so
unrecognized models intentionally show no percentage. Inspect the raw
smartctl JSON before proposing an exact vendor mapping.

### Projection is unavailable

Check `projection.status` in `/api/drives`. Common reasons are fewer than 14
days of history, fewer than two counter increments, a recent counter reset, a
missing stable identity, or a SATA/vendor-specific source.

### Readiness is degraded

Inspect `/api/health`, then check both service logs:

```sh
docker compose logs --tail=200 collector web
docker compose ps
```

If `database.quick_check` is not `ok`, stop the collector and preserve the
volume before attempting SQLite recovery.

## Updating

```sh
git pull --ff-only
docker compose build --pull
docker compose up -d
curl -fsS http://127.0.0.1:8787/api/ready | jq
```

The data volume is independent of both images. Review release notes for schema
or deployment changes before updating an unattended host. On the first 1.1
startup, the collector migrates an older observations table to the constrained
schema and preserves valid history; invalid legacy numeric values become
unavailable rather than blocking startup. The stronger hardware IDs can start
a new history series for drives first recorded by version 1.0.
