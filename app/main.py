"""HTTP application for SSD Life Monitor."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .monitor import DRIVE_ID_PATTERN, CollectorError, MonitorService

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
monitor = MonitorService()
app = FastAPI(
    title="SSD Life Monitor",
    description="A local, read-only Linux dashboard for SSD endurance, SMART health, and temperature.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "lsblk_available": shutil.which("lsblk") is not None,
        "smartctl_available": shutil.which("smartctl") is not None,
        "nvme_cli_available": shutil.which("nvme") is not None,
    }


@app.get("/api/drives")
def drives(
    force: bool = Query(
        default=False, description="Bypass the short server-side cache"
    ),
) -> dict[str, object]:
    try:
        return monitor.snapshot(force=force)
    except CollectorError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/drives/{drive_id}/history")
def drive_history(
    drive_id: str,
    hours: int = Query(default=720, ge=1, le=24 * 90),
) -> dict[str, object]:
    if not DRIVE_ID_PATTERN.fullmatch(drive_id):
        raise HTTPException(status_code=400, detail="invalid drive id")
    return {
        "drive_id": drive_id,
        "hours": hours,
        "points": monitor.history.points(drive_id, hours),
    }
