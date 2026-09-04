#!/bin/sh
set -eu

script_dir=$(dirname -- "$0")
project_dir=$(cd "$script_dir/.." && pwd)
run_id="ssd-life-smoke-$$"
collector_image="$run_id-collector"
web_image="$run_id-web"
collector_name="$run_id-collector"
web_name="$run_id-web"
data_volume="$run_id-data"
runtime_volume="$run_id-runtime"
scratch_dir=$(mktemp -d)

cleanup() {
  docker rm --force "$web_name" "$collector_name" >/dev/null 2>&1 || true
  docker volume rm "$runtime_volume" "$data_volume" >/dev/null 2>&1 || true
  docker image rm "$web_image" "$collector_image" >/dev/null 2>&1 || true
  rm -rf "$scratch_dir"
}
trap cleanup EXIT INT TERM

mkdir -p "$scratch_dir/state"
docker volume create "$data_volume" >/dev/null
docker volume create "$runtime_volume" >/dev/null

docker build --quiet --target collector --tag "$collector_image" "$project_dir" >/dev/null
docker build --quiet --target web --tag "$web_image" "$project_dir" >/dev/null

docker run --detach \
  --name "$collector_name" \
  --network none \
  --read-only \
  --tmpfs /tmp:noexec,nosuid,size=16m \
  --env "PATH=/fake-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  --env DATABASE_PATH=/data/ssd-life.sqlite3 \
  --env COLLECTION_INTERVAL_SECONDS=15 \
  --env STALE_AFTER_SECONDS=180 \
  --env FORCE_MIN_INTERVAL_SECONDS=5 \
  --volume "$data_volume:/data" \
  --volume "$runtime_volume:/run/ssd-life" \
  --volume "$project_dir/tests/fake-bin:/fake-bin:ro" \
  --volume "$scratch_dir/state:/state:ro" \
  "$collector_image" >/dev/null

docker run --detach \
  --name "$web_name" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:noexec,nosuid,size=16m \
  --publish 127.0.0.1::8787 \
  --volume "$runtime_volume:/run/ssd-life:ro" \
  "$web_image" >/dev/null

binding=$(docker port "$web_name" 8787/tcp)
port=${binding##*:}
base_url="http://127.0.0.1:$port"

attempt=0
until curl --fail --silent "$base_url/api/ready" >"$scratch_dir/ready.json" 2>/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    docker logs "$collector_name"
    docker logs "$web_name"
    exit 1
  fi
  sleep 0.25
done

curl --fail --silent --show-error "$base_url/api/drives" >"$scratch_dir/drives.json"
curl --fail --silent --show-error "$base_url/" >"$scratch_dir/index.html"
grep -q 'id="drives"' "$scratch_dir/index.html"
grep -q 'src="/static/app.js"' "$scratch_dir/index.html"
uv run --directory "$project_dir" --locked python -c '
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert payload["stale"] is False
assert len(payload["drives"]) == 1
drive = payload["drives"][0]
assert drive["model"] == "Fixture NVMe 2TB"
assert drive["endurance_used_percent"] == 7
assert drive["endurance_remaining_percent"] == 93
assert drive["temperature_c"] == 42
' "$scratch_dir/drives.json"

test "$(docker exec "$web_name" id -u)" = "10001"
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$collector_name")" = "none"
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$web_name")" = "true"
test "$(docker exec "$collector_name" stat -c %a /run/ssd-life/collector.sock)" = "660"
test "$(docker exec "$collector_name" stat -c %g /run/ssd-life/collector.sock)" = "10001"

touch "$scratch_dir/state/fail"
curl --fail --silent --show-error "$base_url/api/drives?force=true" >"$scratch_dir/stale.json"
uv run --directory "$project_dir" --locked python -c '
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert payload["stale"] is True
assert payload["collector_error"] == "could not discover storage devices: simulated inventory failure"
assert len(payload["drives"]) == 1
' "$scratch_dir/stale.json"

printf '%s\n' "container smoke test passed"
