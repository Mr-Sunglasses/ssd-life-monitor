# Security policy

## Deployment scope

SSD Life Monitor is intended for a trusted local Linux management host. The
public web service does not implement accounts, authorization, or TLS. It
reveals device models, serial numbers, paths, and health history, so it must not
be exposed directly to the public internet.

The default Compose deployment isolates the high-authority component:

- only `collector` is privileged;
- `collector` has no network interface or published port;
- collector/web communication uses a 0660 Unix socket owned by their shared
  numeric group in a private Docker volume;
- `web` runs as UID 10001 with all capabilities dropped, no-new-privileges, and
  a read-only root filesystem; and
- HTTP binds to `127.0.0.1` by default.

The collector's `privileged: true` setting still grants broad host access. On a
known machine, narrow it to explicit block devices and tested capabilities as
described in [docs/operations.md](docs/operations.md). Do not mount the Docker
socket or unrelated host directories into either service.

For remote access, use an authenticated reverse proxy with TLS, a restrictive
firewall, and a trusted management network. Treat the Unix socket and
`ssd-life-data` volume as sensitive.

## Collector safety properties

The browser cannot provide a command or device path. Device paths come from
`lsblk`/`smartctl` discovery, are allowlist-validated, and are passed to fixed
argument vectors with `shell=False` and timeouts. Internal collector API docs
are disabled. Manual hardware refreshes are rate-limited.

This project reads controller telemetry but does not issue SMART self-tests,
firmware commands, format, sanitize, trim, benchmark, mount, or write commands.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
security-advisory flow for the repository, or contact the maintainers through a
private channel they publish.

Include the affected revision, deployment configuration, reproduction steps,
impact, and any suggested mitigation. Do not include real device serial numbers
or other sensitive host inventory in a public report.
