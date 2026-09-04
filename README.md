# SSD Life Monitor

SSD Life Monitor is a small, local-first Linux dashboard for the health signals
reported by storage devices. Its main view is SSD rated-endurance remaining,
alongside SMART status, temperature, warning indicators, and a cautious
history-based lifetime projection.

It is read-only: it does not mount, format, benchmark, trim, or write to a
drive.

## What it reports

- NVMe rated endurance used and remaining
- SMART pass/fail status and all documented smartctl exit-status warnings
- Current temperature and NVMe warning/critical temperature thresholds
- NVMe critical warnings, available spare, media errors, unsafe shutdowns,
  power-on hours, and data written
- Conservative SATA remaining-life attributes when a drive exposes an
  explicitly labelled value
- USB-attached SSDs that `smartctl --scan-open` can identify
- A 90-day chart and a quantization-aware endurance projection
- Explicit stale-data, collector, and database health state

## The important meaning of “SSD life”

For NVMe, the app reads the standardized `percentage_used` health-log field.
That value estimates the percentage of the drive's *rated flash endurance*
that has been consumed:

```text
rated endurance remaining = max(0, 100 - percentage_used)
```

This is not free disk space, battery-style physical health, or a guaranteed
failure date. An SSD can fail before reaching 100%, and many drives continue
working after exceeding their rated endurance. The raw used value can exceed
100%; the remaining display stops at zero.

For SATA, SMART attribute meanings are vendor-specific. The app accepts only a
small exact allowlist of attributes explicitly describing remaining life. It
does not reinterpret generic wear-level counters.

## Quick start

Requirements: Linux, Docker Engine, and Docker Compose v2.

```sh
git clone https://github.com/Mr-Sunglasses/ssd-life-monitor.git
cd ssd-life-monitor
docker compose up --build -d
docker compose ps
```

Open <http://127.0.0.1:8787>.

The deployment intentionally has two containers:

```text
browser -> unprivileged web container -> Unix socket -> privileged collector
                                                     -> SMART/NVMe tools
                                                     -> SQLite history
```

Only `collector` has host-device privileges. It has no network interface and
does not publish a port. Its socket is restricted to group 10001. `web` runs as
UID/GID 10001 with no Linux capabilities and a read-only root filesystem. The
HTTP port is bound to localhost by default.

The broad `privileged: true` collector setting is the compatibility default
because access requirements differ across NVMe, SATA, USB bridges, kernels, and
container runtimes. Narrow it to explicit devices and capabilities after
testing on the target host. See [the operations guide](docs/operations.md).

To stop the app without deleting history:

```sh
docker compose down
```

## Lifetime projection

The projection is deliberately harder to obtain than a simple two-point trend.
It is shown only when all of these conditions hold:

- the source is the standardized NVMe percentage-used counter;
- the drive has a stable serial number or WWN;
- at least 14 days of usable history exist;
- wear increased by at least two percentage points; and
- at least three distinct counter values were observed.

Because the counter commonly advances in coarse 1% steps, the app displays a
range and a low/medium/high confidence label. A counter decrease starts a new
trend segment instead of combining data across a reset or drive replacement.
The result is still a rated-endurance extrapolation—not a predicted failure
date or warranty statement.

## Reliability behavior

- Collection runs every 60 seconds even when no browser is open.
- Multiple drives are queried concurrently, and one failed controller does not
  hide other drives.
- The last successful snapshot is stored in SQLite and survives restarts.
- A failed refresh returns that snapshot with `stale: true`, its age, the error,
  and the consecutive-failure count.
- Browser request and history failures preserve the last rendered reading.
- Manual refreshes are rate-limited to protect the host from repeated hardware
  commands.
- SQLite uses WAL mode, a busy timeout, bounded values, daily retention cleanup,
  and health checks.

## Supported storage

| Device path | Support | Notes |
| --- | --- | --- |
| Native NVMe SSD | Best | Standard endurance, health metrics, and controller temperature thresholds |
| Native SATA SSD | Partial | SMART and temperature; endurance only for exact recognized attributes |
| USB SATA/NVMe SSD | Bridge-dependent | Automatically uses the type reported by `smartctl --scan-open` |
| HDD | Health only | May be listed, but SSD endurance is unavailable |
| RAID/virtual disk | Controller-dependent | The abstraction may hide physical SMART data |

## API and health checks

Interactive API documentation is available at <http://127.0.0.1:8787/docs>.

```sh
curl -fsS http://127.0.0.1:8787/api/health | jq
curl -fsS http://127.0.0.1:8787/api/ready | jq
curl -fsS http://127.0.0.1:8787/api/drives | jq
curl -fsS http://127.0.0.1:8787/api/drives/DRIVE_ID/history?hours=720 | jq
```

`/api/health` always reports the web process and returns degraded details when
the collector cannot be reached. `/api/ready` returns HTTP 503 until the
collector, database, tools, background task, and first fresh snapshot are ready.
See [the API reference](docs/api.md) for field-level details.

## Development with uv

Python dependencies are managed only with
[`uv`](https://docs.astral.sh/uv/). The lockfile is committed and all automated
commands use `--locked`.

```sh
uv sync --locked
make lint
make test
```

Use `uv add <package>` or `uv add --dev <package>` to change dependencies. Do
not add a `requirements.txt` or use direct `pip install` commands for this
project.

To run directly on Linux, first install `smartmontools`, `nvme-cli`, and
`util-linux`. Start the collector and web process in separate terminals:

```sh
mkdir -p .run
sudo -g "$(id -gn)" env \
  DATABASE_PATH="$PWD/ssd-life.sqlite3" \
  COLLECTOR_SOCKET="$PWD/.run/collector.sock" \
  "$PWD/.venv/bin/python" -m app.run_collector
```

```sh
env COLLECTOR_SOCKET="$PWD/.run/collector.sock" \
  uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8787
```

The test suite uses sanitized realistic JSON fixtures and injected command
runners, so unit tests never query physical disks. The container smoke test
boots both production targets with deterministic fake tools:

```sh
./scripts/container-smoke.sh
```

## Documentation

- [Architecture and data semantics](docs/architecture.md)
- [Operations, hardening, backup, and troubleshooting](docs/operations.md)
- [HTTP API reference](docs/api.md)
- [Security policy](SECURITY.md)

The project currently has no license file. Add the intended license before
redistributing it as an open-source package.
