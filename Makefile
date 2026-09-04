.PHONY: install lock test lint run docker-up docker-down

install:
	uv sync --locked

lock:
	uv lock

test:
	uv run --locked python -m pytest

lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

run:
	uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8787

docker-up:
	docker compose up --build

docker-down:
	docker compose down
