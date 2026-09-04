FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS dependencies

# Keep the package manager reproducible independently of the mutable image tag.
COPY --from=ghcr.io/astral-sh/uv@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM dependencies AS collector

# Only this image contains host-inspection tools. It has no HTTP network in
# Compose and exposes normalized, read-only data over a Unix socket.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends smartmontools nvme-cli util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system --gid 10001 ssd-life \
    && mkdir -p /data /run/ssd-life

COPY app ./app

ENV DATABASE_PATH=/data/ssd-life.sqlite3 \
    COLLECTOR_SOCKET=/run/ssd-life/collector.sock \
    COLLECTION_INTERVAL_SECONDS=60 \
    STALE_AFTER_SECONDS=180 \
    FORCE_MIN_INTERVAL_SECONDS=30

USER 0:10001
CMD ["/app/.venv/bin/python", "-m", "app.run_collector"]


FROM dependencies AS web

RUN addgroup --system --gid 10001 ssd-life \
    && adduser --system --uid 10001 --ingroup ssd-life --home /nonexistent --no-create-home ssd-life

COPY app ./app
COPY static ./static

ENV COLLECTOR_SOCKET=/run/ssd-life/collector.sock

USER 10001:10001
EXPOSE 8787
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
