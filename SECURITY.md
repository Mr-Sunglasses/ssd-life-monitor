# Security policy

## Scope

SSD Life Monitor is designed for a trusted local management network. It has no
authentication, authorization, or TLS layer of its own, and its Docker runtime
may have raw access to host block devices.

Do not expose the service directly to the public internet. Put it behind an
authenticated reverse proxy and a restrictive firewall if remote access is
needed.

## Reporting a vulnerability

Please do not open a public issue for a suspected security vulnerability. Use a
private GitHub security advisory or contact the repository maintainers through
the private channel configured for the published repository.
