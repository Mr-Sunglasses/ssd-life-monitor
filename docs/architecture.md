# Architecture

## Design goal

SSD Life Monitor answers three separate questions without confusing them:

1. Is the drive’s SMART health status passing?
2. How much of the SSD’s rated flash endurance does its controller say has been used?
3. What temperature is the drive reporting right now?

Free capacity is intentionally not part of the health calculation. A nearly
full but healthy SSD and an empty but worn SSD are different situations.

## Data flow

```text
lsblk --json
    │
    ├── identify disk, transport, model, serial, size, rotational flag
    │
    ├── smartctl -a <device> --json
    │       ├── SMART pass/fail
    │       ├── current temperature
    │       └── NVMe percentage_used or conservative SATA attribute mapping
    │
    └── nvme id-ctrl <device> --output-format=json   (NVMe only)
            ├── wctemp
            └── cctemp
             │
             ▼
       normalized drive record
             │
       one-minute SQLite observation
             │
             ▼
       FastAPI JSON endpoints ── vanilla HTML/CSS/JS dashboard
```

The collector uses argument vectors with `subprocess.run(..., shell=False)`.
The browser cannot submit a device path or command. Device names originate in
`lsblk` and are validated before becoming `/dev/<name>` paths.

## Endurance semantics

For NVMe, `percentage_used` is the controller’s estimate of rated endurance
consumed. It is not a direct measurement of remaining physical flash and it is
not a calendar prediction. The normalized calculation is:

```text
used = percentage_used
remaining = max(0, 100 - used)
```

The raw used value is retained so values above 100 remain visible to API
consumers. The UI clamps the ring to zero at that point.

ATA SMART attributes do not have one universal endurance field. The parser only
uses attribute names that explicitly describe a remaining-life percentage. A
generic wear counter is returned as unavailable instead of being presented as
a misleading percentage.

## Time projection

The history store keeps one sample per drive per minute and retains 90 days.
The projection uses the oldest and newest usable sample in the requested
history window:

```text
daily wear rate = (latest used - oldest used) / elapsed days
days remaining = (100 - latest used) / daily wear rate
```

It remains unavailable when there are fewer than two samples, less than one
hour of history, or no increase in the wear counter. A drive replacement or
counter reset should also invalidate the trend; this first version chooses
conservative non-estimation over silently joining unrelated histories.

## Caching and refresh

`MonitorService` serializes snapshots with a lock and caches them for 15
seconds by default. This protects a host from several browser tabs issuing
simultaneous SMART queries. `?force=true` bypasses that cache. The frontend
refreshes every 15 seconds and separately loads the last 30 days of chart data.

## Failure behavior

- A failed `lsblk` inventory returns HTTP 503 because the app cannot know which
  devices exist.
- A failed per-drive SMART command leaves that drive visible with `unknown`
  health and a collector error.
- SMART non-zero exit codes do not automatically mean command failure. SMART
  tools use bit flags for health conditions, so valid JSON is parsed even when
  the process exits non-zero.
- Missing fields are represented as unavailable. The app does not substitute
  generic temperatures or life percentages.
