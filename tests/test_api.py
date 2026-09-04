import asyncio

import httpx

from app import main
from app.collector_client import CollectorUnavailable


class StubCollectorClient:
    def __init__(self):
        self.requests = []
        self.fail = False

    def get(self, path, params=None):
        self.requests.append((path, params))
        if self.fail:
            raise CollectorUnavailable("socket unavailable")
        if path == "/internal/health":
            return {"status": "ok", "ready": True}
        if path == "/internal/ready":
            return {"status": "ok", "ready": True}
        if path == "/internal/drives":
            return {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "poll_seconds": 15,
                "stale": False,
                "drives": [],
            }
        if path.endswith("/history"):
            return {
                "drive_id": "0123456789abcdef",
                "hours": 24,
                "points": [{"observed_at": 1, "used_percent": 3}],
            }
        raise AssertionError(f"unexpected path: {path}")


def request(path):
    async def send():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)

    return asyncio.run(send())


def test_index_and_proxied_health_are_available(monkeypatch):
    collector = StubCollectorClient()
    monkeypatch.setattr(main, "collector_client", collector)

    index = request("/")
    health = request("/api/health")

    assert index.status_code == 200
    assert "SSD Life Monitor" in index.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["collector_reachable"] is True


def test_health_stays_live_but_ready_fails_when_collector_is_unreachable(monkeypatch):
    collector = StubCollectorClient()
    collector.fail = True
    monkeypatch.setattr(main, "collector_client", collector)

    health = request("/api/health")
    ready = request("/api/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["collector_reachable"] is False
    assert ready.status_code == 503


def test_drive_snapshot_forwards_rate_limited_force_request(monkeypatch):
    collector = StubCollectorClient()
    monkeypatch.setattr(main, "collector_client", collector)

    response = request("/api/drives?force=true")

    assert response.status_code == 200
    assert response.json()["drives"] == []
    assert collector.requests[-1] == ("/internal/drives", {"force": True})


def test_history_route_validates_id_before_proxying(monkeypatch):
    collector = StubCollectorClient()
    monkeypatch.setattr(main, "collector_client", collector)

    invalid = request("/api/drives/not-a-drive/history")
    valid = request("/api/drives/0123456789abcdef/history?hours=24&max_points=500")

    assert invalid.status_code == 400
    assert valid.status_code == 200
    assert valid.json()["points"][0]["used_percent"] == 3
    assert collector.requests[-1] == (
        "/internal/drives/0123456789abcdef/history",
        {"hours": 24, "max_points": 500},
    )
