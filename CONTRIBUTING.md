# Contributing

## Development setup

Install Python 3.12 and [`uv`](https://docs.astral.sh/uv/), then create the
locked development environment:

```sh
uv sync --locked
```

Dependency changes must go through `uv`:

```sh
uv add package-name
uv add --dev package-name
uv lock --check
```

Commit `pyproject.toml` and `uv.lock` together. Do not add generated
requirements files or direct `pip install` steps.

## Quality checks

Run the same static and unit checks used by CI:

```sh
make lint
make test
uv run --locked python -m compileall -q app
node --check static/app.js
shellcheck scripts/container-smoke.sh tests/fake-bin/*
docker compose config --quiet
```

When Docker is available, verify the actual process boundary and stale fallback:

```sh
make container-smoke
```

The smoke test builds both Docker targets, starts the Unix-socket deployment
with deterministic fake host tools, validates the normalized SSD values, checks
that the web process is non-root/read-only, confirms collector network
isolation, and forces an inventory failure to verify last-good behavior.

## Tests and fixtures

Unit tests must not inspect the developer's physical drives. Inject a command
runner into `DriveCollector` and use sanitized smartctl/lsblk/nvme JSON under
`tests/fixtures/`. Fixtures should preserve the real tool's JSON shape while
using invented model names, serial numbers, and identifiers.

Add regression coverage for malformed, missing, non-finite, out-of-range, and
non-zero-exit cases whenever parser behavior changes. Reliability changes
should also cover restart or concurrency behavior where relevant.

## Pull requests

Keep changes focused and explain:

- the problem and user-visible behavior;
- any change to SSD-life semantics or confidence;
- security/privilege implications;
- migration and rollback considerations; and
- the exact verification performed.

Never present the endurance percentage or time projection as a guaranteed
remaining lifetime. Update the relevant documentation whenever API fields,
configuration, architecture, or operational behavior changes.
