"""Privileged collector API, intended to listen only on a Unix socket."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from .monitor import DRIVE_ID_PATTERN, CollectorError, MonitorService

LOG = logging.getLogger(__name__)
monitor = MonitorService()


async def collection_loop(service: MonitorService, stop: asyncio.Event) -> None:
    """Collect immediately and continue independently of dashboard traffic."""

    while not stop.is_set():
        try:
            await asyncio.to_thread(service.refresh, False, "background")
        except CollectorError as error:
            LOG.warning("background storage collection unavailable: %s", error)
        except Exception:
            LOG.exception("unexpected background collection failure")
        try:
            await asyncio.wait_for(stop.wait(), timeout=service.collection_interval)
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop = asyncio.Event()
    task = asyncio.create_task(collection_loop(monitor, stop))
    app.state.collection_stop = stop
    app.state.collection_task = task
    try:
        yield
    finally:
        stop.set()
        await task


app = FastAPI(
    title="SSD Life Monitor Collector",
    description="Internal privileged collector API.",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


def _tool_status() -> dict[str, bool]:
    return {
        "lsblk_available": shutil.which("lsblk") is not None,
        "smartctl_available": shutil.which("smartctl") is not None,
        "nvme_cli_available": shutil.which("nvme") is not None,
    }


@app.get("/internal/health")
def health() -> dict[str, Any]:
    task = getattr(app.state, "collection_task", None)
    payload = monitor.health()
    tools = _tool_status()
    background_task_running = bool(task and not task.done())
    ready = bool(
        payload["ready"]
        and tools["lsblk_available"]
        and tools["smartctl_available"]
        and background_task_running
    )
    return {
        **payload,
        **tools,
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "background_task_running": background_task_running,
    }


@app.get("/internal/ready")
def ready() -> dict[str, Any]:
    payload = health()
    if not payload["ready"]:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/internal/drives")
async def drives(
    force: bool = Query(
        default=False,
        description="Request a rate-limited fresh hardware collection",
    ),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(monitor.snapshot, force)
    except CollectorError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/internal/drives/{drive_id}/history")
def drive_history(
    drive_id: str,
    hours: int = Query(default=720, ge=1, le=24 * 90),
    max_points: int = Query(default=1200, ge=100, le=5000),
) -> dict[str, Any]:
    if not DRIVE_ID_PATTERN.fullmatch(drive_id):
        raise HTTPException(status_code=400, detail="invalid drive id")
    try:
        points = monitor.history.points(drive_id, hours, max_points=max_points)
    except sqlite3.Error as error:
        raise HTTPException(
            status_code=503, detail="history database unavailable"
        ) from error
    return {
        "drive_id": drive_id,
        "hours": hours,
        "max_points": max_points,
        "points": points,
    }
