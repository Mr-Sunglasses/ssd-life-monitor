FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/ssd-life.sqlite3 \
    CACHE_SECONDS=15

# smartmontools reads ATA/NVMe health data; nvme-cli provides the NVMe
# controller temperature thresholds; util-linux provides lsblk.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends smartmontools nvme-cli util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data
EXPOSE 8787

# The container normally needs root plus host device access for SMART/NVMe
# ioctls. See docs/operations.md for a least-privilege alternative.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
