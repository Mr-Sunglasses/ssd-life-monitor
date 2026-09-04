import asyncio
import threading

from app import collector_api
from app.collector_api import collection_loop


def test_background_collection_runs_without_dashboard_requests():
    class Service:
        collection_interval = 0.01

        def __init__(self):
            self.calls = 0
            self.second_call = threading.Event()

        def refresh(self, force, reason):
            self.calls += 1
            assert force is False
            assert reason == "background"
            if self.calls >= 2:
                self.second_call.set()

    async def exercise():
        service = Service()
        stop = asyncio.Event()
        task = asyncio.create_task(collection_loop(service, stop))
        reached_second_call = await asyncio.to_thread(service.second_call.wait, 1)
        stop.set()
        await task
        return service.calls, reached_second_call

    calls, reached_second_call = asyncio.run(exercise())
    assert reached_second_call is True
    assert calls >= 2


def test_background_collection_recovers_from_unexpected_failure():
    class Service:
        collection_interval = 0.01

        def __init__(self):
            self.calls = 0
            self.second_call = threading.Event()

        def refresh(self, force, reason):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("one bad cycle")
            self.second_call.set()

    async def exercise():
        service = Service()
        stop = asyncio.Event()
        task = asyncio.create_task(collection_loop(service, stop))
        reached_second_call = await asyncio.to_thread(service.second_call.wait, 1)
        stop.set()
        await task
        return service.calls, reached_second_call

    calls, reached_second_call = asyncio.run(exercise())
    assert reached_second_call is True
    assert calls >= 2


def test_collector_health_requires_background_task_and_required_tools(monkeypatch):
    class Monitor:
        def health(self):
            return {"status": "ok", "ready": True}

    class Task:
        def done(self):
            return False

    monkeypatch.setattr(collector_api, "monitor", Monitor())
    monkeypatch.setattr(
        collector_api.app.state, "collection_task", Task(), raising=False
    )
    monkeypatch.setattr(
        collector_api.shutil,
        "which",
        lambda command: None if command == "smartctl" else f"/usr/bin/{command}",
    )

    payload = collector_api.health()

    assert payload["ready"] is False
    assert payload["status"] == "degraded"
    assert payload["background_task_running"] is True
    assert payload["smartctl_available"] is False
