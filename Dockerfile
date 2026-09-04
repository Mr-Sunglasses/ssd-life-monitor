FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# Keep the package manager reproducible independently of the mutable image tag.
COPY --from=ghcr.io/astral-sh/uv@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/ssd-life.sqlite3 \
    CACHE_SECONDS=15 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# smartmontools reads ATA/NVMe health data; nvme-cli provides the NVMe
# controller temperature thresholds; util-linux provides lsblk.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends smartmontools nvme-cli util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

COPY app ./app
COPY static ./static

RUN mkdir -p /data
EXPOSE 8787

# The container normally needs root plus host device access for SMART/NVMe
# ioctls. See docs/operations.md for a least-privilege alternative.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
