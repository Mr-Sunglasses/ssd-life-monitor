.PHONY: install lock test lint run run-web run-collector container-smoke docker-up docker-down

RUNTIME_DIR := $(CURDIR)/.run
COLLECTOR_SOCKET := $(RUNTIME_DIR)/collector.sock
DATABASE_PATH := $(CURDIR)/ssd-life.sqlite3

install:
	uv sync --locked

lock:
	uv lock

test:
	uv run --locked python -m pytest

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked mypy app

run: run-web

run-web:
	env COLLECTOR_SOCKET="$(COLLECTOR_SOCKET)" uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8787

run-collector:
	mkdir -p "$(RUNTIME_DIR)"
	env DATABASE_PATH="$(DATABASE_PATH)" COLLECTOR_SOCKET="$(COLLECTOR_SOCKET)" uv run --locked python -m app.run_collector

container-smoke:
	./scripts/container-smoke.sh

docker-up:
	docker compose up --build

docker-down:
	docker compose down
