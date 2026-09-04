import asyncio

import httpx

from app import main


class StubHistory:
    def points(self, drive_id, hours=720):
        return [
            {
                "observed_at": 1,
                "used_percent": 3,
                "temperature_c": 40,
                "smart_status": "healthy",
            }
        ]


class StubMonitor:
    history = StubHistory()

    def snapshot(self, force=False):
        return {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "poll_seconds": 15,
            "drives": [],
        }


def test_index_and_health_routes_are_available():
    index = request("/")
    health = request("/api/health")

    assert index.status_code == 200
    assert "SSD Life Monitor" in index.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


def test_drive_snapshot_route_uses_the_monitor_service(monkeypatch):
    monkeypatch.setattr(main, "monitor", StubMonitor())
    response = request("/api/drives?force=true")

    assert response.status_code == 200
    assert response.json()["drives"] == []


def test_history_route_validates_the_opaque_drive_id(monkeypatch):
    monkeypatch.setattr(main, "monitor", StubMonitor())
    invalid = request("/api/drives/../../etc/passwd/history")
    valid = request("/api/drives/0123456789abcdef/history?hours=24")

    assert invalid.status_code in {400, 404}
    assert valid.status_code == 200
    assert valid.json()["points"][0]["used_percent"] == 3


def request(path):
    async def send():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(path)

    return asyncio.run(send())
