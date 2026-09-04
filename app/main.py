"""Unprivileged HTTP application for SSD Life Monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .collector_client import CollectorClient, CollectorUnavailable
from .monitor import DRIVE_ID_PATTERN

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
collector_client = CollectorClient()
app = FastAPI(
    title="SSD Life Monitor",
    description="A local Linux dashboard for SSD endurance, SMART health, and temperature.",
    version="1.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _collector_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return collector_client.get(path, params=params)
    except CollectorUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        collector = collector_client.get("/internal/health")
    except CollectorUnavailable as error:
        return {
            "status": "degraded",
            "ready": False,
            "web": "ok",
            "collector_reachable": False,
            "collector_error": str(error),
        }
    return {**collector, "web": "ok", "collector_reachable": True}


@app.get("/api/ready")
def ready() -> dict[str, Any]:
    return _collector_get("/internal/ready")


@app.get("/api/drives")
def drives(
    force: bool = Query(
        default=False,
        description="Request a rate-limited fresh hardware collection",
    ),
) -> dict[str, Any]:
    return _collector_get("/internal/drives", params={"force": force})


@app.get("/api/drives/{drive_id}/history")
def drive_history(
    drive_id: str,
    hours: int = Query(default=720, ge=1, le=24 * 90),
    max_points: int = Query(default=1200, ge=100, le=5000),
) -> dict[str, Any]:
    if not DRIVE_ID_PATTERN.fullmatch(drive_id):
        raise HTTPException(status_code=400, detail="invalid drive id")
    return _collector_get(
        f"/internal/drives/{drive_id}/history",
        params={"hours": hours, "max_points": max_points},
    )
