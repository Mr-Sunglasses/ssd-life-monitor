.PHONY: install test run docker-up docker-down

install:
	python3 -m pip install -r requirements-dev.txt

test:
	python3 -m pytest

run:
	python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8787

docker-up:
	docker compose up --build

docker-down:
	docker compose down
