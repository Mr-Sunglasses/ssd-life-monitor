# SSD Life Monitor

SSD Life Monitor is a small, local-first Linux web app that reports what the
storage device itself knows about its condition:

- NVMe rated endurance consumed and remaining
- SMART health status
- Current temperature
- NVMe controller warning and critical temperature thresholds
- A cautious, history-based endurance projection after enough samples exist
- Drive model, serial number, transport, size, and device path

It is intentionally read-only. It does not mount, format, write to, benchmark,
trim, or otherwise modify a drive.

## What “life remaining” means

For NVMe, the app reads the standardized SMART health-log field
`percentage_used`. This is the manufacturer/controller estimate of the portion
of the drive’s *rated endurance* that has been consumed. The app displays:

```text
endurance remaining = max(0, 100 - percentage_used)
```

This is not free capacity, a guaranteed failure date, or a prediction of how
many years the SSD will operate. `percentage_used` may exceed 100 when a drive
has exceeded its rated endurance; the displayed remaining value is then zero.

The optional time projection is calculated locally from stored readings. It is
shown only after at least two readings with increasing wear and one hour of
history. It is a rough projection at the observed wear rate, not a warranty or
failure prediction.

## Quick start with Docker

Docker is the recommended installation because the image includes the Linux
utilities required by the collector.

```sh
git clone https://github.com/Mr-Sunglasses/ssd-life-monitor.git
cd ssd-life-monitor
docker compose up --build
```

Open <http://127.0.0.1:8787>.

The Compose file uses `privileged: true` because SMART and NVMe controller
queries commonly require raw access to host block devices. This is a powerful
permission. Keep the dashboard bound to localhost unless you add authentication
and a trusted network boundary. See [docs/operations.md](docs/operations.md)
for safer device/capability configurations and troubleshooting.

To stop it:

```sh
docker compose down
```

The SQLite history lives in the named `ssd-life-data` volume. Removing that
volume also removes the history:

```sh
docker compose down --volumes
```

## Run directly on Linux

Install Python 3.12+, `smartmontools`, `nvme-cli`, and `util-linux` using the
host distribution’s package manager. Then:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
sudo -E env DATABASE_PATH="$PWD/ssd-life.sqlite3" "$PWD/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

The process needs permission to query the drives. Running the whole web server
as root is convenient for a private machine but is not ideal; a dedicated
collector service with a small authenticated API is safer for a shared host.

## API

FastAPI publishes interactive documentation at `/docs` and an OpenAPI schema
at `/openapi.json`.

### `GET /api/health`

Returns application status and whether `lsblk`, `smartctl`, and `nvme` are
present in the runtime environment.

### `GET /api/drives`

Discovers `disk` devices reported by `lsblk` with `nvme` or `sata` transport,
then collects health data. The result contains a normalized record for each
device. A short server-side cache avoids running hardware queries repeatedly
when several browsers are open. Use `?force=true` for a fresh reading.

Important fields include:

| Field | Meaning |
| --- | --- |
| `smart_status` | `healthy`, `unhealthy`, or `unknown` from SMART JSON |
| `temperature_c` | Current temperature reported by `smartctl` |
| `endurance_used_percent` | Standardized NVMe usage or a clearly-labelled SATA attribute |
| `endurance_remaining_percent` | The normalized remaining endurance value |
| `endurance_source` | How the endurance value was obtained |
| `projection` | Conservative history-based projection, if available |

### `GET /api/drives/{id}/history?hours=720`

Returns the stored one-minute endurance/temperature samples for a drive. The
server accepts only the generated hexadecimal drive identifier, never an
arbitrary path or command.

## Supported devices and limitations

### NVMe SSDs

NVMe is the strongest-supported path. The collector uses:

```sh
smartctl -a /dev/nvme0n1 --json
nvme id-ctrl /dev/nvme0n1 --output-format=json
```

It reads `percentage_used`, `temperature.current`, `wctemp`, and `cctemp`.

### SATA SSDs

SMART health and temperature are generally available. SSD endurance is not a
universal ATA field, so the app accepts only clearly-labelled attributes such
as `Media_Wearout_Indicator`, `SSD_Life_Left`, or percentage-lifetime-remaining
attributes. It deliberately does not reinterpret generic
`Wear_Leveling_Count` values, because vendor meanings differ. Unsupported
SATA models show endurance as unavailable rather than inventing a percentage.

### USB enclosures and virtual disks

Some USB-to-SATA/NVMe bridges do not pass SMART commands through. Depending on
the enclosure, a host may need an explicit smartctl device type such as
`smartctl -d sat`; this project does not guess device types automatically.
Virtual disks and RAID controller abstractions may also hide the physical SSD
health log.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
make install
make test
make run
```

The tests use fake command output and do not require a physical SSD. The
collector is dependency-injected so SMART parsing, failure handling, and
history calculations can be tested without invoking host commands.

See [docs/architecture.md](docs/architecture.md) for the data flow and design
decisions, and [docs/operations.md](docs/operations.md) for deployment,
permissions, backups, and troubleshooting.

## Safety and privacy

The app does not upload telemetry. Model, serial, device path, temperatures,
and history stay on the host unless somebody can reach the HTTP server. Do not
expose it directly to the internet. Add authentication, TLS, and a narrow
network policy before making it reachable beyond a trusted local machine.

The application has no license file yet. Add the license that matches the
intended distribution before publishing it as a reusable open-source project.
