# Operations guide

## Docker deployment

Build and run the local dashboard:

```sh
docker compose up --build -d
docker compose logs -f ssd-life-monitor
```

The default Compose binding is `127.0.0.1:8787`. To permit access from a
trusted LAN, change it deliberately to `0.0.0.0:8787:8787` and put the service
behind authentication and a firewall. The app itself does not provide user
accounts.

The data volume stores the SQLite endurance history:

```sh
docker volume inspect ssd-life-data
docker compose down --volumes   # destructive: also removes history
```

Back up the database before moving hosts or rebuilding the deployment. A simple
copy while the service is stopped is sufficient for this small append/replace
database:

```sh
docker compose stop
docker run --rm -v ssd-life-data:/data -v "$PWD":/backup alpine \
  cp /data/ssd-life.sqlite3 /backup/ssd-life.sqlite3
docker compose start
```

## Device permissions

`privileged: true` is the compatibility-first default. It allows the container
to see the host’s block devices and issue the raw queries needed by
`smartctl`/`nvme-cli`, but it grants much more authority than this app needs.

For a hardened deployment, first identify the exact devices and test a narrower
configuration on the target host. Possible controls include:

- pass only required devices with Docker `devices:` entries;
- add only the capabilities required by the host kernel and utilities, often
  `SYS_RAWIO` and sometimes `SYS_ADMIN` for NVMe operations;
- run the collector as a separate host service and expose only its authenticated
  JSON endpoint to an unprivileged web container;
- bind the HTTP port to localhost or a private management network.

There is no universal least-privilege recipe across SATA, NVMe, USB bridges,
kernel versions, and container runtimes. Verify the actual command behavior on
the target machine before removing `privileged`.

## Health checks

The app endpoint:

```sh
curl -fsS http://127.0.0.1:8787/api/health
curl -fsS http://127.0.0.1:8787/api/drives | jq
```

The first endpoint confirms whether the required utilities are installed. The second
confirms device discovery and reports per-drive collection errors.

Useful host-side comparisons:

```sh
lsblk --nodeps --json --bytes --output NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,ROTA
sudo smartctl -a /dev/nvme0n1 --json
sudo nvme id-ctrl /dev/nvme0n1 --output-format=json
```

If the host commands work but the container does not, the issue is almost
always device visibility, container privileges, or a USB bridge limitation.

## Troubleshooting

### “No NVMe or SATA disks were found”

Check `lsblk` output and confirm the disk is reported as a `disk` with `TRAN` of
`nvme` or `sata`. USB bridges can report a different or empty transport.

### SMART is `unknown`

Run `smartctl` manually with `sudo`. Check that the container has the device,
that the command is not timing out, and that the output is valid JSON. Some
controllers need an explicit `-d` type; this app currently does not infer one.

### Temperature is shown but life is unavailable

This is expected for many SATA SSDs. NVMe has a standardized endurance counter;
ATA SMART life indicators are vendor-specific. Inspect the JSON attribute table
before adding a model-specific mapping.

### Temperature thresholds are unavailable

The thresholds are collected only for NVMe from `nvme id-ctrl`. SATA drives
often provide a current temperature without a standardized warning threshold.
The UI therefore shows the reading without inventing a limit.

### The time projection stays unavailable

The wear counter must increase over at least one hour. If the drive has not
crossed another percentage unit, if history was recently deleted, or if the
drive was replaced, there is not enough reliable information to project a date.

## Updating

Rebuild the image after changing application code or dependencies:

```sh
docker compose build --pull
docker compose up -d
```

The SQLite volume is independent of the image and is retained by this update.
