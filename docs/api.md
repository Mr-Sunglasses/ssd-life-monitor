# HTTP API reference

The public API is served by the unprivileged web process. It proxies a fixed
read-only interface to the collector over a Unix socket. Interactive OpenAPI
documentation is available at `/docs` and the schema at `/openapi.json`.

## `GET /api/health`

Always returns HTTP 200 when the web process is running. A healthy response
contains `status: "ok"`, `ready: true`, and `collector_reachable: true`.
Degraded responses include diagnostic fields without making the health endpoint
itself unavailable.

Important fields:

| Field | Meaning |
| --- | --- |
| `ready` | Collector can serve a fresh, durable snapshot |
| `background_task_running` | Independent scheduled collection loop is alive |
| `has_snapshot` | At least one successful collection exists |
| `snapshot_age_seconds` | Age of the latest successful collection |
| `collector_error` | Last global collection error, or `null` |
| `consecutive_failures` | Failed cycles since the latest success |
| `database` | SQLite quick-check, schema, and writability state |
| `lsblk_available` | Required inventory tool is present |
| `smartctl_available` | Required SMART tool is present |
| `nvme_cli_available` | Optional NVMe threshold tool is present |

## `GET /api/ready`

Returns HTTP 200 only when all required tools exist, the background task is
running, SQLite is healthy, and a fresh snapshot is available. Otherwise it
returns HTTP 503. Use this endpoint for container readiness checks.

## `GET /api/drives`

Returns the current snapshot. Collection happens in the background; ordinary
requests do not execute hardware commands.

Query parameter:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `force` | `false` | Ask for a fresh collection, subject to the configured rate limit |

Snapshot fields:

| Field | Meaning |
| --- | --- |
| `generated_at` | UTC timestamp of the returned successful snapshot |
| `drives` | Normalized drive records |
| `stale` | `true` when the snapshot is old or a newer collection failed |
| `snapshot_age_seconds` | Current age of the successful snapshot |
| `last_success_at` / `last_attempt_at` | Collection timing |
| `collector_error` | Last global failure reason |
| `force_deferred` | Manual refresh was rate-limited or another refresh was active |
| `collection_interval_seconds` | Background collection cadence |
| `poll_seconds` | Suggested browser polling cadence |

Drive fields:

| Field | Meaning |
| --- | --- |
| `id` | Opaque 16-character hardware-derived identifier |
| `identity_quality` | `wwn`, `serial`, or `path-fallback` |
| `path`, `transport`, `protocol`, `smartctl_type` | Discovery and query metadata |
| `type` | `ssd` or `hdd` |
| `smart_status` | `healthy`, `unhealthy`, or `unknown` |
| `temperature_c` / `temperature_status` | Reading and normalized threshold state |
| `endurance_used_percent` | Raw valid controller/vendor endurance value |
| `endurance_remaining_percent` | Remaining value clamped to 0–100 |
| `endurance_source` | `nvme-percentage-used`, `sata-smart-attribute`, or `null` |
| `nvme_critical_warning` | Raw NVMe critical-warning bitmask |
| `nvme_critical_warnings` | Decoded active warning messages |
| `available_spare_percent` | Current NVMe spare percentage |
| `media_errors`, `error_log_entries` | NVMe reliability counters |
| `unsafe_shutdowns`, `power_on_hours`, `data_units_written` | NVMe usage counters |
| `health_warnings` | Decoded smartctl health/history exit bits |
| `collector_errors` | Command/tool failures limited to this drive |
| `projection` | Eligibility status, range, rate, confidence, and evidence |

All unavailable numeric fields are JSON `null`; the API does not invent zeroes.

## `GET /api/drives/{id}/history`

Returns time-ordered, one-minute observation buckets for a generated drive ID.

| Parameter | Default | Range |
| --- | ---: | ---: |
| `hours` | 720 | 1–2160 |
| `max_points` | 1200 | 100–5000 |

Response points contain `observed_at` as Unix seconds, `used_percent`,
`temperature_c`, and `smart_status`. Transient missing numeric readings do not
erase a valid value already recorded in the same one-minute bucket. Long series
are evenly downsampled to `max_points` while retaining their first and last
observations.

Invalid IDs return HTTP 400. The route never accepts a device path.

## Errors

HTTP 503 means the collector is unreachable, no successful snapshot exists, or
the requested collector/database operation is unavailable. When a durable
snapshot does exist, global collection failures normally return HTTP 200 with
`stale: true` so consumers can distinguish known-old data from no data.
