"""Linux storage discovery, SMART parsing, and endurance history.

The collector deliberately shells out to the standard Linux utilities instead of
trying to speak SMART/NVMe ioctl protocols directly. Those utilities know how to
handle the large variety of SSD controllers and expose machine-readable JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CommandRunner = Callable[[list[str], float], "CommandResult"]
NUMBER_TYPES = (int, float)
DEVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._+:-]+$")
DRIVE_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")


@dataclass(frozen=True)
class CommandResult:
    """The useful part of a completed subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str = ""


class CollectorError(RuntimeError):
    """Raised when the host cannot provide a disk inventory."""


def run_command(args: list[str], timeout: float) -> CommandResult:
    """Run a fixed, argument-vector command without invoking a shell."""

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return CommandResult(returncode=127, stdout="", stderr=str(error))
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode()
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode()
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        return CommandResult(
            returncode=124, stdout=stdout, stderr=f"command timed out: {stderr}".strip()
        )
    except OSError as error:
        return CommandResult(returncode=126, stdout="", stderr=str(error))

    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _number(value: Any) -> float | None:
    """Return real numeric values while rejecting booleans and malformed data."""

    if isinstance(value, NUMBER_TYPES) and not isinstance(value, bool):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _clean_text(value: Any, fallback: str = "Unknown") -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value or fallback


def _json_object(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _temperature_from_smart(data: dict[str, Any]) -> float | None:
    temperature = data.get("temperature")
    if isinstance(temperature, dict):
        return _number(temperature.get("current"))
    return None


def _sata_remaining_from_smart(data: dict[str, Any]) -> tuple[float | None, str | None]:
    """Read only clearly-labelled SATA remaining-life attributes.

    ATA SMART attributes are not standardized in the same way as NVMe's
    percentage_used field. A value is accepted only when the attribute name
    explicitly describes a percentage/life-remaining metric. Generic wear-level
    counters are intentionally not guessed at.
    """

    attribute_data = data.get("ata_smart_attributes")
    if not isinstance(attribute_data, dict):
        return None, None
    attributes = attribute_data.get("table", [])
    if not isinstance(attributes, list):
        return None, None

    accepted_names = (
        "percentlifetimeremain",
        "percentliferemaining",
        "percentagelifetimeremaining",
        "ssdlifeleft",
        "mediawearoutindicator",
    )
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        name = re.sub(r"[^a-z0-9]", "", _clean_text(attribute.get("name"), "").lower())
        if not any(candidate in name for candidate in accepted_names):
            continue
        value = _number(attribute.get("value"))
        if value is None:
            continue
        return max(0.0, min(100.0, value)), "sata-smart-attribute"
    return None, None


def parse_smartctl_json(stdout: str, transport: str) -> dict[str, Any]:
    """Extract normalized health data from smartctl JSON.

    smartctl uses exit-code bits for health conditions, so callers must parse
    stdout even when returncode is non-zero. This function therefore only uses
    the JSON payload and does not infer health from the process exit code.
    """

    data = _json_object(stdout)
    if data is None:
        return {
            "smart_status": "unknown",
            "temperature_c": None,
            "endurance_used_percent": None,
            "endurance_remaining_percent": None,
            "endurance_source": None,
        }

    smart_status = data.get("smart_status")
    passed = smart_status.get("passed") if isinstance(smart_status, dict) else None
    if passed is True:
        health = "healthy"
    elif passed is False:
        health = "unhealthy"
    else:
        health = "unknown"

    temperature = _temperature_from_smart(data)
    used: float | None = None
    remaining: float | None = None
    source: str | None = None

    nvme_log = data.get("nvme_smart_health_information_log")
    if isinstance(nvme_log, dict):
        used = _number(nvme_log.get("percentage_used"))
        if used is None:
            # Older nvme-cli JSON calls this field percent_used.
            used = _number(nvme_log.get("percent_used"))
        if used is not None:
            source = "nvme-percentage-used"
            remaining = max(0.0, 100.0 - used)

    if used is None and transport == "sata":
        remaining, source = _sata_remaining_from_smart(data)
        if remaining is not None:
            used = max(0.0, 100.0 - remaining)

    return {
        "smart_status": health,
        "temperature_c": temperature,
        "endurance_used_percent": used,
        "endurance_remaining_percent": remaining,
        "endurance_source": source,
    }


def parse_nvme_thresholds(stdout: str) -> dict[str, float | None]:
    """Convert NVMe controller warning/critical thresholds from Kelvin."""

    data = _json_object(stdout)
    if data is None:
        return {"temperature_warning_c": None, "temperature_critical_c": None}

    def kelvin_to_celsius(value: Any) -> float | None:
        kelvin = _number(value)
        return round(kelvin - 273.15, 2) if kelvin is not None and kelvin > 0 else None

    return {
        "temperature_warning_c": kelvin_to_celsius(data.get("wctemp")),
        "temperature_critical_c": kelvin_to_celsius(data.get("cctemp")),
    }


def stable_drive_id(transport: str, serial: str, model: str, device_name: str) -> str:
    """Create a stable, non-sensitive URL identifier for a physical disk."""

    identity = (
        f"{transport}:{serial}:{model}"
        if serial.lower() not in {"", "unknown"}
        else f"{transport}:{device_name}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _device_path(device_name: str) -> str:
    """Build a safe /dev path from lsblk's basename output."""

    if device_name in {".", ".."} or not DEVICE_NAME_PATTERN.fullmatch(device_name):
        raise CollectorError(f"unsupported block-device name: {device_name!r}")
    return f"/dev/{device_name}"


class DriveCollector:
    """Discover supported block devices and collect their health data."""

    def __init__(
        self, runner: CommandRunner = run_command, command_timeout: float = 12.0
    ):
        self.runner = runner
        self.command_timeout = command_timeout

    def discover(self) -> list[dict[str, Any]]:
        result = self.runner(
            [
                "lsblk",
                "--nodeps",
                "--json",
                "--bytes",
                "--output",
                "NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,ROTA",
            ],
            self.command_timeout,
        )
        data = _json_object(result.stdout)
        if result.returncode != 0 or data is None:
            detail = result.stderr.strip() or "lsblk returned no usable JSON"
            raise CollectorError(f"could not discover storage devices: {detail}")

        blockdevices = data.get("blockdevices")
        if not isinstance(blockdevices, list):
            raise CollectorError("lsblk JSON did not contain blockdevices")

        drives: list[dict[str, Any]] = []
        for item in blockdevices:
            if not isinstance(item, dict) or item.get("type") != "disk":
                continue
            transport = _clean_text(item.get("tran"), "").lower()
            if transport not in {"nvme", "sata"}:
                continue

            device_name = _clean_text(item.get("name"), "")
            if not device_name:
                continue
            try:
                device_path = _device_path(device_name)
            except CollectorError:
                # lsblk should never return a path-like name, but ignore a
                # malformed record rather than turning one device into a
                # whole-inventory failure.
                continue
            model = _clean_text(item.get("model"))
            serial = _clean_text(item.get("serial"))
            rota = item.get("rota")
            is_ssd = transport == "nvme" or rota is False or rota == 0
            drives.append(
                {
                    "id": stable_drive_id(transport, serial, model, device_name),
                    "device": device_name,
                    "path": device_path,
                    "transport": transport,
                    "type": "ssd" if is_ssd else "hdd",
                    "model": model,
                    "serial": serial,
                    "size_bytes": _integer(item.get("size")) or 0,
                }
            )
        return drives

    def collect_one(self, discovered: dict[str, Any]) -> dict[str, Any]:
        path = discovered["path"]
        transport = discovered["transport"]
        smartctl_result = self.runner(
            ["smartctl", "-a", path, "--json"],
            self.command_timeout,
        )
        smart = parse_smartctl_json(smartctl_result.stdout, transport)

        thresholds = {"temperature_warning_c": None, "temperature_critical_c": None}
        command_errors: list[str] = []
        if smartctl_result.returncode == 127:
            command_errors.append("smartctl is not installed")
        elif smartctl_result.returncode == 124:
            command_errors.append("smartctl timed out")
        elif not smartctl_result.stdout.strip():
            command_errors.append(
                smartctl_result.stderr.strip() or "smartctl returned no JSON"
            )

        if transport == "nvme":
            threshold_result = self.runner(
                ["nvme", "id-ctrl", path, "--output-format=json"],
                self.command_timeout,
            )
            thresholds = parse_nvme_thresholds(threshold_result.stdout)
            if threshold_result.returncode == 127:
                command_errors.append("nvme-cli is not installed")
            elif threshold_result.returncode == 124:
                command_errors.append("nvme id-ctrl timed out")

        current_temperature = smart["temperature_c"]
        warning_temperature = thresholds["temperature_warning_c"]
        critical_temperature = thresholds["temperature_critical_c"]
        if (
            critical_temperature is not None
            and current_temperature is not None
            and current_temperature >= critical_temperature
        ):
            temperature_status = "critical"
        elif (
            warning_temperature is not None
            and current_temperature is not None
            and current_temperature >= warning_temperature
        ):
            temperature_status = "warning"
        elif current_temperature is not None:
            temperature_status = "normal"
        else:
            temperature_status = "unknown"

        return {
            **discovered,
            **smart,
            **thresholds,
            "temperature_status": temperature_status,
            "collector_errors": [error for error in command_errors if error],
        }

    def collect_all(self) -> list[dict[str, Any]]:
        discovered = self.discover()
        if not discovered:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(discovered))) as executor:
            collected = list(executor.map(self.collect_one, discovered))
        return sorted(
            collected,
            key=lambda drive: (
                drive["type"] != "ssd",
                drive["model"].casefold(),
                drive["device"],
            ),
        )


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def estimate_days_remaining(
    points: Iterable[dict[str, Any]],
    current_used_percent: float | None,
    minimum_history_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Estimate time to rated endurance from monotonic historical samples.

    This is intentionally conservative: the result is unavailable until there
    are two samples with increasing wear and at least an hour between them.
    """

    ordered = sorted(
        (
            (_number(point.get("observed_at")), _number(point.get("used_percent")))
            for point in points
        ),
        key=lambda pair: pair[0] or 0,
    )
    usable = [
        (timestamp, used)
        for timestamp, used in ordered
        if timestamp is not None and used is not None
    ]
    if current_used_percent is None or len(usable) < 2:
        return {
            "status": "insufficient-history",
            "days_remaining": None,
            "daily_rate_percent": None,
        }

    first_timestamp, first_used = usable[0]
    last_timestamp, last_used = usable[-1]
    elapsed = last_timestamp - first_timestamp
    used_delta = last_used - first_used
    if elapsed < minimum_history_seconds:
        return {
            "status": "insufficient-history",
            "days_remaining": None,
            "daily_rate_percent": None,
        }
    if used_delta <= 0:
        return {
            "status": "no-wear-observed",
            "days_remaining": None,
            "daily_rate_percent": None,
        }

    daily_rate = used_delta / (elapsed / 86400.0)
    days = max(0.0, (100.0 - current_used_percent) / daily_rate)
    return {
        "status": "estimated",
        "days_remaining": round(days, 1),
        "daily_rate_percent": round(daily_rate, 5),
    }


class HistoryStore:
    """Small SQLite store for one-minute endurance observations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    device_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    used_percent REAL,
                    temperature_c REAL,
                    smart_status TEXT NOT NULL,
                    PRIMARY KEY (device_id, observed_at)
                );
                CREATE INDEX IF NOT EXISTS observations_device_time
                    ON observations (device_id, observed_at);
                """
            )

    def record(self, device: dict[str, Any], timestamp: float | None = None) -> None:
        timestamp = timestamp if timestamp is not None else time.time()
        bucket = int(timestamp // 60 * 60)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO observations
                    (device_id, observed_at, used_percent, temperature_c, smart_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, observed_at) DO UPDATE SET
                    used_percent = COALESCE(excluded.used_percent, observations.used_percent),
                    temperature_c = COALESCE(excluded.temperature_c, observations.temperature_c),
                    smart_status = excluded.smart_status
                """,
                (
                    device["id"],
                    bucket,
                    device.get("endurance_used_percent"),
                    device.get("temperature_c"),
                    device.get("smart_status", "unknown"),
                ),
            )
            connection.execute(
                "DELETE FROM observations WHERE observed_at < ?",
                (int(timestamp - 90 * 86400),),
            )

    def points(self, device_id: str, hours: int = 720) -> list[dict[str, Any]]:
        since = int(time.time() - max(1, min(hours, 24 * 90)) * 3600)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, used_percent, temperature_c, smart_status
                FROM observations
                WHERE device_id = ? AND observed_at >= ?
                ORDER BY observed_at ASC
                """,
                (device_id, since),
            ).fetchall()
        return [dict(row) for row in rows]


class MonitorService:
    """Cached collector facade used by the HTTP routes."""

    def __init__(
        self,
        collector: DriveCollector | None = None,
        history: HistoryStore | None = None,
        cache_seconds: float | None = None,
    ):
        # Docker sets DATABASE_PATH=/data/ssd-life.sqlite3. A relative default
        # keeps importing the app harmless on a development machine where /data
        # may not exist or be writable.
        database_path = os.getenv("DATABASE_PATH", "ssd-life.sqlite3")
        self.collector = collector or DriveCollector()
        self.history = history or HistoryStore(database_path)
        self.cache_seconds = (
            cache_seconds
            if cache_seconds is not None
            else float(os.getenv("CACHE_SECONDS", "15"))
        )
        self._cache: dict[str, Any] | None = None
        self._cache_time = 0.0
        self._lock = threading.Lock()

    def snapshot(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if (
                not force
                and self._cache is not None
                and now - self._cache_time < self.cache_seconds
            ):
                return self._cache

            drives = self.collector.collect_all()
            for drive in drives:
                self.history.record(drive, now)
                drive["projection"] = estimate_days_remaining(
                    self.history.points(drive["id"]),
                    drive.get("endurance_used_percent"),
                )

            snapshot = {
                "generated_at": _iso_timestamp(now),
                "poll_seconds": self.cache_seconds,
                "drives": drives,
            }
            self._cache = snapshot
            self._cache_time = now
            return snapshot
