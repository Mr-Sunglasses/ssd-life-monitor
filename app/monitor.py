"""Linux storage discovery, SMART parsing, and durable endurance history."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CommandRunner = Callable[[list[str], float], "CommandResult"]
NUMBER_TYPES = (int, float)
DEVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._+:-]+$")
DEVICE_PATH_PATTERN = re.compile(r"^/dev/[A-Za-z0-9._+:/-]+$")
SMARTCTL_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9._,+:-]+$")
DRIVE_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")
SCHEMA_VERSION = 2
RETENTION_SECONDS = 90 * 86400
DEFAULT_HISTORY_HOURS = 24 * 90
LOG = logging.getLogger(__name__)

SMARTCTL_EXIT_MESSAGES = {
    0: ("error", "smartctl command line could not be parsed"),
    1: ("error", "device could not be opened or identified"),
    2: ("error", "a SMART command failed or returned invalid data"),
    3: ("critical", "SMART reports that the drive is failing"),
    4: ("critical", "a prefail SMART attribute is at or below its threshold"),
    5: ("warning", "a SMART attribute crossed its threshold in the past"),
    6: ("warning", "the device error log contains errors"),
    7: ("warning", "the device self-test log contains failed tests"),
}

NVME_CRITICAL_WARNING_MESSAGES = {
    0: "available spare is below the controller threshold",
    1: "temperature is outside the controller threshold",
    2: "device reliability is degraded",
    3: "media has entered read-only mode",
    4: "volatile memory backup failed",
    5: "persistent memory region is read-only or unreliable",
}


@dataclass(frozen=True)
class CommandResult:
    """The useful part of a completed subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str = ""
    execution_error: str | None = None


class CollectorError(RuntimeError):
    """Raised when no fresh storage snapshot can be produced."""


def run_command(args: list[str], timeout: float) -> CommandResult:
    """Run a fixed argument vector without invoking a shell."""

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return CommandResult(
            returncode=127,
            stdout="",
            stderr=str(error),
            execution_error="not-found",
        )
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else (error.stdout or "")
        )
        stderr = (
            error.stderr.decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        return CommandResult(
            returncode=124,
            stdout=stdout,
            stderr=f"command timed out: {stderr}".strip(),
            execution_error="timeout",
        )
    except OSError as error:
        return CommandResult(
            returncode=126,
            stdout="",
            stderr=str(error),
            execution_error="os-error",
        )

    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _number(value: Any) -> float | None:
    """Return finite real numeric values while rejecting booleans."""

    if not isinstance(value, NUMBER_TYPES) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _number_in_range(value: Any, minimum: float, maximum: float) -> float | None:
    number = _number(value)
    if number is None or not minimum <= number <= maximum:
        return None
    return number


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _clean_text(value: Any, fallback: str = "Unknown") -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value or fallback


def _known_text(value: Any) -> str:
    cleaned = _clean_text(value, "")
    return "" if cleaned.casefold() in {"unknown", "none", "n/a"} else cleaned


def _json_object(stdout: str) -> dict[str, Any] | None:
    def reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    try:
        value = json.loads(stdout, parse_constant=reject_nonstandard_constant)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _iso_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _empty_smart_data() -> dict[str, Any]:
    return {
        "smart_status": "unknown",
        "temperature_c": None,
        "endurance_used_percent": None,
        "endurance_remaining_percent": None,
        "endurance_source": None,
        "nvme_critical_warning": None,
        "nvme_critical_warnings": [],
        "available_spare_percent": None,
        "available_spare_threshold_percent": None,
        "media_errors": None,
        "error_log_entries": None,
        "unsafe_shutdowns": None,
        "power_on_hours": None,
        "data_units_written": None,
    }


def _temperature_from_smart(data: dict[str, Any]) -> float | None:
    temperature = data.get("temperature")
    if not isinstance(temperature, dict):
        return None
    return _number_in_range(temperature.get("current"), -273.15, 1000.0)


def _sata_remaining_from_smart(data: dict[str, Any]) -> tuple[float | None, str | None]:
    """Read only clearly-labelled SATA remaining-life attributes."""

    attribute_data = data.get("ata_smart_attributes")
    if not isinstance(attribute_data, dict):
        return None, None
    attributes = attribute_data.get("table", [])
    if not isinstance(attributes, list):
        return None, None

    accepted_names = {
        "percentlifetimeremain",
        "percentliferemaining",
        "percentagelifetimeremaining",
        "ssdlifeleft",
        "mediawearoutindicator",
    }
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        name = re.sub(r"[^a-z0-9]", "", _clean_text(attribute.get("name"), "").lower())
        if name not in accepted_names:
            continue
        value = _number_in_range(attribute.get("value"), 0.0, 100.0)
        if value is not None:
            return value, "sata-smart-attribute"
    return None, None


def _nvme_metric(log: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _number(log.get(name))
        if value is not None:
            return value
    return None


def _nvme_nonnegative_metric(log: dict[str, Any], *names: str) -> float | None:
    value = _nvme_metric(log, *names)
    return value if value is not None and value >= 0 else None


def _nvme_critical_warnings(value: int | None) -> list[str]:
    if value is None:
        return []
    return [
        message
        for bit, message in NVME_CRITICAL_WARNING_MESSAGES.items()
        if value & (1 << bit)
    ]


def parse_smartctl_json(stdout: str, protocol: str) -> dict[str, Any]:
    """Extract normalized health and endurance fields from smartctl JSON."""

    data = _json_object(stdout)
    if data is None:
        return _empty_smart_data()

    result = _empty_smart_data()
    smart_status = data.get("smart_status")
    passed = smart_status.get("passed") if isinstance(smart_status, dict) else None
    if passed is True:
        result["smart_status"] = "healthy"
    elif passed is False:
        result["smart_status"] = "unhealthy"

    result["temperature_c"] = _temperature_from_smart(data)
    nvme_log = data.get("nvme_smart_health_information_log")
    if isinstance(nvme_log, dict):
        used = _nvme_metric(nvme_log, "percentage_used", "percent_used")
        if used is not None and 0.0 <= used <= 255.0:
            result["endurance_used_percent"] = used
            result["endurance_remaining_percent"] = max(0.0, min(100.0, 100.0 - used))
            result["endurance_source"] = "nvme-percentage-used"

        critical_warning = _number_in_range(
            _nvme_metric(nvme_log, "critical_warning"), 0.0, 255.0
        )
        critical_warning_integer = _integer(critical_warning)
        result.update(
            {
                "nvme_critical_warning": critical_warning_integer,
                "nvme_critical_warnings": _nvme_critical_warnings(
                    critical_warning_integer
                ),
                "available_spare_percent": _number_in_range(
                    _nvme_metric(nvme_log, "available_spare", "avail_spare"),
                    0.0,
                    100.0,
                ),
                "available_spare_threshold_percent": _number_in_range(
                    _nvme_metric(nvme_log, "available_spare_threshold", "spare_thresh"),
                    0.0,
                    100.0,
                ),
                "media_errors": _nvme_nonnegative_metric(nvme_log, "media_errors"),
                "error_log_entries": _nvme_nonnegative_metric(
                    nvme_log, "num_err_log_entries", "error_information_log_entries"
                ),
                "unsafe_shutdowns": _nvme_nonnegative_metric(
                    nvme_log, "unsafe_shutdowns"
                ),
                "power_on_hours": _nvme_nonnegative_metric(nvme_log, "power_on_hours"),
                "data_units_written": _nvme_nonnegative_metric(
                    nvme_log, "data_units_written"
                ),
            }
        )
        if critical_warning_integer is not None and critical_warning_integer != 0:
            result["smart_status"] = "unhealthy"

    if result["endurance_used_percent"] is None and protocol == "sata":
        remaining, source = _sata_remaining_from_smart(data)
        if remaining is not None:
            result["endurance_remaining_percent"] = remaining
            result["endurance_used_percent"] = 100.0 - remaining
            result["endurance_source"] = source

    return result


def parse_nvme_thresholds(stdout: str) -> dict[str, float | None]:
    """Convert NVMe controller warning and critical thresholds from Kelvin."""

    data = _json_object(stdout)
    if data is None:
        return {"temperature_warning_c": None, "temperature_critical_c": None}

    def kelvin_to_celsius(value: Any) -> float | None:
        kelvin = _number_in_range(value, 1.0, 2000.0)
        return round(kelvin - 273.15, 2) if kelvin is not None else None

    return {
        "temperature_warning_c": kelvin_to_celsius(data.get("wctemp")),
        "temperature_critical_c": kelvin_to_celsius(data.get("cctemp")),
    }


def parse_smartctl_exit_status(returncode: int) -> dict[str, list[str]]:
    """Decode smartctl's documented eight-bit status mask."""

    if returncode == 0 or not 0 <= returncode <= 255:
        return {"errors": [], "warnings": []}

    errors: list[str] = []
    warnings: list[str] = []
    for bit, (severity, message) in SMARTCTL_EXIT_MESSAGES.items():
        if not returncode & (1 << bit):
            continue
        if severity == "error":
            errors.append(message)
        else:
            warnings.append(message)
    return {"errors": errors, "warnings": warnings}


def parse_smartctl_scan_json(stdout: str) -> dict[str, dict[str, str]]:
    """Return safe smartctl device metadata indexed by /dev path."""

    data = _json_object(stdout)
    devices = data.get("devices") if data is not None else None
    if not isinstance(devices, list):
        return {}

    parsed: dict[str, dict[str, str]] = {}
    for item in devices:
        if not isinstance(item, dict):
            continue
        path = _clean_text(item.get("name"), "")
        device_type = _clean_text(item.get("type"), "")
        if (
            not DEVICE_PATH_PATTERN.fullmatch(path)
            or ".." in Path(path).parts
            or (device_type and not SMARTCTL_TYPE_PATTERN.fullmatch(device_type))
        ):
            continue
        raw_protocol = _clean_text(item.get("protocol"), "").casefold()
        if "nvme" in raw_protocol:
            protocol = "nvme"
        elif "ata" in raw_protocol:
            protocol = "sata"
        elif "scsi" in raw_protocol:
            protocol = "scsi"
        else:
            protocol = "unknown"
        parsed[path] = {"smartctl_type": device_type, "protocol": protocol}
    return parsed


def _smart_identity(data: dict[str, Any] | None) -> tuple[str, str, str]:
    if data is None:
        return "", "", ""
    serial = _known_text(data.get("serial_number"))
    model = _known_text(data.get("model_name"))
    raw_wwn = data.get("wwn")
    if isinstance(raw_wwn, dict):
        parts = []
        for name in ("naa", "oui", "id"):
            value = raw_wwn.get(name)
            if isinstance(value, int) and value >= 0:
                parts.append(format(value, "x"))
            elif text := _known_text(value):
                parts.append(text)
        wwn = "-".join(parts)
    else:
        wwn = _known_text(raw_wwn)
    return serial, model, wwn


def stable_drive_id(
    transport: str,
    serial: str,
    model: str,
    device_name: str,
    wwn: str = "",
    size_bytes: int = 0,
) -> str:
    """Create an opaque identifier, preferring hardware-stable identities."""

    known_wwn = _known_text(wwn)
    known_serial = _known_text(serial)
    if known_wwn:
        identity = f"wwn:{known_wwn.casefold()}"
    elif known_serial:
        identity = f"serial:{known_serial.casefold()}"
    else:
        identity = f"path:{transport}:{device_name}:{model}:{size_bytes}"
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def drive_identity_quality(serial: str, wwn: str) -> str:
    if _known_text(wwn):
        return "wwn"
    if _known_text(serial):
        return "serial"
    return "path-fallback"


def _device_path(device_name: str) -> str:
    if device_name in {".", ".."} or not DEVICE_NAME_PATTERN.fullmatch(device_name):
        raise CollectorError(f"unsupported block-device name: {device_name!r}")
    return f"/dev/{device_name}"


def _unknown_drive(discovered: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        **discovered,
        **_empty_smart_data(),
        "temperature_warning_c": None,
        "temperature_critical_c": None,
        "temperature_status": "unknown",
        "smartctl_exit_status": None,
        "health_warnings": [],
        "collector_errors": [error],
    }


class DriveCollector:
    """Discover block devices and collect health data with per-drive isolation."""

    def __init__(
        self, runner: CommandRunner = run_command, command_timeout: float = 12.0
    ):
        self.runner = runner
        self.command_timeout = command_timeout

    def _smartctl_scan(self) -> dict[str, dict[str, str]]:
        result = self.runner(
            ["smartctl", "--scan-open", "--json"], self.command_timeout
        )
        if result.execution_error:
            return {}
        return parse_smartctl_scan_json(result.stdout)

    def discover(self) -> list[dict[str, Any]]:
        result = self.runner(
            [
                "lsblk",
                "--nodeps",
                "--json",
                "--bytes",
                "--output",
                "NAME,MODEL,SERIAL,WWN,SIZE,TYPE,TRAN,ROTA",
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

        scan = self._smartctl_scan()
        drives: list[dict[str, Any]] = []
        for item in blockdevices:
            if not isinstance(item, dict) or item.get("type") != "disk":
                continue
            device_name = _clean_text(item.get("name"), "")
            if not device_name:
                continue
            try:
                device_path = _device_path(device_name)
            except CollectorError:
                continue

            transport = _clean_text(item.get("tran"), "").casefold() or "unknown"
            scan_item = scan.get(device_path, {})
            rota = item.get("rota")
            is_non_rotating = rota is False or rota == 0
            if (
                not scan_item
                and transport not in {"nvme", "sata"}
                and not is_non_rotating
            ):
                continue

            protocol = scan_item.get("protocol", "unknown")
            if protocol == "unknown" and transport == "nvme":
                protocol = "nvme"
            elif protocol == "unknown" and transport == "sata":
                protocol = "sata"

            model = _clean_text(item.get("model"))
            serial = _clean_text(item.get("serial"))
            wwn = _clean_text(item.get("wwn"), "")
            size_bytes = _integer(item.get("size")) or 0
            is_ssd = protocol == "nvme" or transport == "nvme" or is_non_rotating
            drives.append(
                {
                    "id": stable_drive_id(
                        transport, serial, model, device_name, wwn, size_bytes
                    ),
                    "identity_quality": drive_identity_quality(serial, wwn),
                    "device": device_name,
                    "path": device_path,
                    "transport": transport,
                    "protocol": protocol,
                    "smartctl_type": scan_item.get("smartctl_type", ""),
                    "type": "ssd" if is_ssd else "hdd",
                    "model": model,
                    "serial": serial,
                    "wwn": wwn or None,
                    "size_bytes": size_bytes,
                }
            )
        return drives

    def collect_one(self, discovered: dict[str, Any]) -> dict[str, Any]:
        path = discovered["path"]
        protocol = discovered["protocol"]
        smartctl_args = ["smartctl", "-a"]
        if discovered.get("smartctl_type"):
            smartctl_args.extend(["--device", discovered["smartctl_type"]])
        smartctl_args.extend([path, "--json"])
        smartctl_result = self.runner(smartctl_args, self.command_timeout)
        smart_payload = _json_object(smartctl_result.stdout)
        smart = parse_smartctl_json(smartctl_result.stdout, protocol)

        enriched = dict(discovered)
        smart_serial, smart_model, smart_wwn = _smart_identity(smart_payload)
        if not _known_text(enriched.get("serial")) and smart_serial:
            enriched["serial"] = smart_serial
        if not _known_text(enriched.get("model")) and smart_model:
            enriched["model"] = smart_model
        if not _known_text(enriched.get("wwn")) and smart_wwn:
            enriched["wwn"] = smart_wwn
        enriched["id"] = stable_drive_id(
            enriched["transport"],
            enriched["serial"],
            enriched["model"],
            enriched["device"],
            enriched.get("wwn") or "",
            enriched["size_bytes"],
        )
        enriched["identity_quality"] = drive_identity_quality(
            enriched["serial"], enriched.get("wwn") or ""
        )

        command_errors: list[str] = []
        health_warnings: list[str] = []
        if smartctl_result.execution_error == "not-found":
            command_errors.append("smartctl is not installed")
        elif smartctl_result.execution_error == "os-error":
            command_errors.append(
                smartctl_result.stderr.strip() or "smartctl could not run"
            )
        elif smartctl_result.execution_error == "timeout":
            command_errors.append("smartctl timed out")
        elif not smartctl_result.stdout.strip():
            command_errors.append(
                smartctl_result.stderr.strip() or "smartctl returned no JSON"
            )
        elif smart_payload is None:
            command_errors.append("smartctl returned malformed JSON")

        if smartctl_result.execution_error is None:
            exit_details = parse_smartctl_exit_status(smartctl_result.returncode)
            command_errors.extend(exit_details["errors"])
            health_warnings.extend(exit_details["warnings"])
            if 0 <= smartctl_result.returncode <= 255 and smartctl_result.returncode & (
                (1 << 3) | (1 << 4)
            ):
                smart["smart_status"] = "unhealthy"

        thresholds: dict[str, float | None] = {
            "temperature_warning_c": None,
            "temperature_critical_c": None,
        }
        if enriched["transport"] == "nvme":
            threshold_result = self.runner(
                ["nvme", "id-ctrl", path, "--output-format=json"],
                self.command_timeout,
            )
            thresholds = parse_nvme_thresholds(threshold_result.stdout)
            if threshold_result.execution_error == "not-found":
                command_errors.append("nvme-cli is not installed")
            elif threshold_result.execution_error == "os-error":
                command_errors.append(
                    threshold_result.stderr.strip() or "nvme id-ctrl could not run"
                )
            elif threshold_result.execution_error == "timeout":
                command_errors.append("nvme id-ctrl timed out")
            elif threshold_result.returncode != 0 or (
                threshold_result.stdout.strip()
                and _json_object(threshold_result.stdout) is None
            ):
                command_errors.append("nvme id-ctrl returned unusable data")

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
            **enriched,
            **smart,
            **thresholds,
            "temperature_status": temperature_status,
            "smartctl_exit_status": (
                smartctl_result.returncode
                if smartctl_result.execution_error is None
                and 0 <= smartctl_result.returncode <= 255
                else None
            ),
            "health_warnings": list(dict.fromkeys(health_warnings)),
            "collector_errors": list(
                dict.fromkeys(error for error in command_errors if error)
            ),
        }

    def collect_all(self) -> list[dict[str, Any]]:
        discovered = self.discover()
        if not discovered:
            return []

        collected: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(discovered))) as executor:
            futures = {
                executor.submit(self.collect_one, drive): drive for drive in discovered
            }
            for future in as_completed(futures):
                drive = futures[future]
                try:
                    collected.append(future.result())
                except Exception as error:  # keep one controller from hiding all others
                    LOG.exception("drive collection failed for id=%s", drive["id"])
                    collected.append(
                        _unknown_drive(drive, f"unexpected collector failure: {error}")
                    )

        return sorted(
            collected,
            key=lambda drive: (
                drive["type"] != "ssd",
                drive["model"].casefold(),
                drive["device"],
            ),
        )


def _projection_result(status: str, **values: Any) -> dict[str, Any]:
    result = {
        "status": status,
        "days_remaining": None,
        "days_remaining_low": None,
        "days_remaining_high": None,
        "daily_rate_percent": None,
        "confidence": None,
        "history_days": None,
        "wear_delta_percent": None,
        "observations": 0,
        "reset_detected": False,
    }
    result.update(values)
    return result


def estimate_days_remaining(
    points: Iterable[dict[str, Any]],
    current_used_percent: float | None,
    minimum_history_seconds: float = 14 * 86400.0,
    minimum_wear_delta: float = 2.0,
    minimum_distinct_values: int = 3,
    identity_reliable: bool = True,
    endurance_source: str | None = "nvme-percentage-used",
) -> dict[str, Any]:
    """Estimate rated-endurance depletion with quantization-aware bounds.

    The standardized NVMe percentage is intentionally coarse. A projection is
    withheld until a stable identity has at least two observed counter steps
    over two weeks. A decrease starts a new segment instead of joining data
    across a reset or replacement.
    """

    current = _number_in_range(current_used_percent, 0.0, 255.0)
    if endurance_source != "nvme-percentage-used":
        return _projection_result("unsupported-source")
    if not identity_reliable:
        return _projection_result("unstable-identity")
    if current is None:
        return _projection_result("insufficient-history")

    ordered = sorted(
        {
            (timestamp, used)
            for point in points
            if (timestamp := _number(point.get("observed_at"))) is not None
            and (used := _number_in_range(point.get("used_percent"), 0.0, 255.0))
            is not None
        },
        key=lambda pair: pair[0],
    )
    if len(ordered) < 2:
        return _projection_result("insufficient-history", observations=len(ordered))

    segment_start = 0
    reset_detected = False
    for index in range(1, len(ordered)):
        if ordered[index][1] < ordered[index - 1][1]:
            segment_start = index
            reset_detected = True
    usable = ordered[segment_start:]
    if len(usable) < 2:
        return _projection_result(
            "counter-reset",
            observations=len(usable),
            reset_detected=reset_detected,
        )

    first_timestamp, first_used = usable[0]
    last_timestamp, last_used = usable[-1]
    elapsed = last_timestamp - first_timestamp
    elapsed_days = elapsed / 86400.0
    used_delta = last_used - first_used
    common = {
        "history_days": round(max(0.0, elapsed_days), 1),
        "wear_delta_percent": round(used_delta, 2),
        "observations": len(usable),
        "reset_detected": reset_detected,
    }
    if elapsed < minimum_history_seconds:
        status = "counter-reset" if reset_detected else "insufficient-history"
        return _projection_result(status, **common)
    if used_delta <= 0:
        return _projection_result("no-wear-observed", **common)

    distinct_values = len({used for _, used in usable})
    if used_delta < minimum_wear_delta or distinct_values < minimum_distinct_values:
        return _projection_result("insufficient-wear-change", **common)

    daily_rate = used_delta / elapsed_days
    remaining = max(0.0, 100.0 - current)
    quantization = 1.0
    lower_rate = max(0.0, used_delta - quantization) / elapsed_days
    upper_rate = (used_delta + quantization) / elapsed_days
    days = remaining / daily_rate if daily_rate > 0 else 0.0
    low_days = remaining / upper_rate if upper_rate > 0 else 0.0
    high_days = remaining / lower_rate if lower_rate > 0 else None

    if elapsed_days >= 60 and used_delta >= 5 and len(usable) >= 30:
        confidence = "high"
    elif elapsed_days >= 30 and used_delta >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return _projection_result(
        "estimated",
        days_remaining=round(days, 1),
        days_remaining_low=round(low_days, 1),
        days_remaining_high=round(high_days, 1) if high_days is not None else None,
        daily_rate_percent=round(daily_rate, 5),
        confidence=confidence,
        **common,
    )


class HistoryStore:
    """SQLite store for observations and the latest successful snapshot."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_lock = threading.Lock()
        self._last_prune = 0.0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    device_id TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    used_percent REAL,
                    temperature_c REAL,
                    smart_status TEXT NOT NULL,
                    PRIMARY KEY (device_id, observed_at),
                    CHECK (used_percent IS NULL OR used_percent BETWEEN 0 AND 255),
                    CHECK (temperature_c IS NULL OR temperature_c BETWEEN -273.15 AND 1000)
                );
                CREATE INDEX IF NOT EXISTS observations_device_time
                    ON observations (device_id, observed_at);
                """
            )
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'observations'"
            ).fetchone()["sql"]
            if (
                "CHECK (used_percent IS NULL OR used_percent BETWEEN 0 AND 255)"
                not in definition
            ):
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE observations_v2 (
                        device_id TEXT NOT NULL,
                        observed_at INTEGER NOT NULL,
                        used_percent REAL,
                        temperature_c REAL,
                        smart_status TEXT NOT NULL,
                        PRIMARY KEY (device_id, observed_at),
                        CHECK (used_percent IS NULL OR used_percent BETWEEN 0 AND 255),
                        CHECK (temperature_c IS NULL OR temperature_c BETWEEN -273.15 AND 1000)
                    );
                    INSERT INTO observations_v2
                        (device_id, observed_at, used_percent, temperature_c, smart_status)
                    SELECT
                        device_id,
                        observed_at,
                        CASE
                            WHEN used_percent BETWEEN 0 AND 255 THEN used_percent
                            ELSE NULL
                        END,
                        CASE
                            WHEN temperature_c BETWEEN -273.15 AND 1000 THEN temperature_c
                            ELSE NULL
                        END,
                        smart_status
                    FROM observations;
                    DROP TABLE observations;
                    ALTER TABLE observations_v2 RENAME TO observations;
                    CREATE INDEX observations_device_time
                        ON observations (device_id, observed_at);
                    COMMIT;
                    """
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS latest_snapshot (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    generated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'last_prune'"
            ).fetchone()
        if row is not None:
            try:
                persisted = float(row["value"])
            except (TypeError, ValueError):
                persisted = None
            if persisted is not None and math.isfinite(persisted):
                self._last_prune = persisted

    def record(self, device: dict[str, Any], timestamp: float | None = None) -> None:
        timestamp = timestamp if timestamp is not None else time.time()
        bucket = int(timestamp // 60 * 60)
        used = _number_in_range(device.get("endurance_used_percent"), 0.0, 255.0)
        temperature = _number_in_range(device.get("temperature_c"), -273.15, 1000.0)
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
                    used,
                    temperature,
                    device.get("smart_status", "unknown"),
                ),
            )

    def prune(self, timestamp: float | None = None) -> None:
        timestamp = timestamp if timestamp is not None else time.time()
        with self._prune_lock:
            if timestamp - self._last_prune < 86400:
                return
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM observations WHERE observed_at < ?",
                    (int(timestamp - RETENTION_SECONDS),),
                )
                connection.execute(
                    """
                    INSERT INTO metadata (key, value) VALUES ('last_prune', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(timestamp),),
                )
            self._last_prune = timestamp

    def points(
        self,
        device_id: str,
        hours: int = DEFAULT_HISTORY_HOURS,
        max_points: int | None = None,
    ) -> list[dict[str, Any]]:
        bounded_hours = max(1, min(hours, 24 * 90))
        since = int(time.time() - bounded_hours * 3600)
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
        points = [dict(row) for row in rows]
        if max_points is None or len(points) <= max_points:
            return points
        bounded_max = max(2, min(max_points, 5000))
        step = (len(points) - 1) / (bounded_max - 1)
        indexes = [round(index * step) for index in range(bounded_max)]
        return [points[index] for index in dict.fromkeys(indexes)]

    def save_snapshot(self, snapshot: dict[str, Any], timestamp: float) -> None:
        payload = json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO latest_snapshot (singleton, generated_at, payload)
                VALUES (1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    payload = excluded.payload
                """,
                (timestamp, payload),
            )

    def load_snapshot(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generated_at, payload FROM latest_snapshot WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        payload = _json_object(row["payload"])
        generated_at = _number(row["generated_at"])
        if (
            payload is None
            or not isinstance(payload.get("drives"), list)
            or generated_at is None
            or generated_at < 0
        ):
            return None
        payload["generated_at_epoch"] = generated_at
        return payload

    def health(self) -> dict[str, Any]:
        with self._connect() as connection:
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES ('healthcheck', 'ok')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            connection.rollback()
        return {
            "ok": quick_check == "ok" and schema_version == SCHEMA_VERSION,
            "quick_check": quick_check,
            "schema_version": schema_version,
            "writable": True,
        }


def _environment_float(
    name: str, default: float, minimum: float, maximum: float
) -> tuple[float, str | None]:
    raw = os.getenv(name)
    if raw is None:
        return default, None
    try:
        value = float(raw)
    except ValueError:
        return default, f"{name} must be numeric; using {default}"
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return (
            default,
            f"{name} must be between {minimum} and {maximum}; using {default}",
        )
    return value, None


class MonitorService:
    """Background-friendly collector facade with durable stale fallback."""

    def __init__(
        self,
        collector: DriveCollector | None = None,
        history: HistoryStore | None = None,
        collection_interval: float | None = None,
        stale_after: float | None = None,
        force_min_interval: float | None = None,
        cache_seconds: float | None = None,
    ):
        database_path = os.getenv("DATABASE_PATH", "ssd-life.sqlite3")
        self.collector = collector or DriveCollector()
        self.history = history or HistoryStore(database_path)

        interval, interval_warning = _environment_float(
            "COLLECTION_INTERVAL_SECONDS", 60.0, 15.0, 86400.0
        )
        if collection_interval is None and cache_seconds is not None:
            collection_interval = cache_seconds
        self.collection_interval = collection_interval or interval
        default_stale = max(180.0, self.collection_interval * 3)
        stale_value, stale_warning = _environment_float(
            "STALE_AFTER_SECONDS", default_stale, 30.0, 604800.0
        )
        force_value, force_warning = _environment_float(
            "FORCE_MIN_INTERVAL_SECONDS", 30.0, 5.0, 3600.0
        )
        self.stale_after = stale_after or stale_value
        self.force_min_interval = force_min_interval or force_value
        self.configuration_warnings = [
            warning
            for warning in (interval_warning, stale_warning, force_warning)
            if warning
        ]

        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._snapshot = self.history.load_snapshot()
        self._last_success = (
            _number(self._snapshot.get("generated_at_epoch"))
            if self._snapshot is not None
            else None
        )
        self._last_attempt: float | None = None
        self._last_manual_attempt: float | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    def _decorate_snapshot(
        self, snapshot: dict[str, Any], force_deferred: bool = False
    ) -> dict[str, Any]:
        result = copy.deepcopy(snapshot)
        now = time.time()
        with self._state_lock:
            last_success = self._last_success
            last_attempt = self._last_attempt
            last_error = self._last_error
            failures = self._consecutive_failures
        age = now - last_success if last_success is not None else None
        stale = bool(last_error) or age is None or age > self.stale_after
        result.update(
            {
                "stale": stale,
                "snapshot_age_seconds": round(max(0.0, age), 1)
                if age is not None
                else None,
                "last_success_at": _iso_timestamp(last_success)
                if last_success
                else None,
                "last_attempt_at": _iso_timestamp(last_attempt)
                if last_attempt
                else None,
                "collector_error": last_error,
                "consecutive_failures": failures,
                "force_deferred": force_deferred,
                "collection_interval_seconds": self.collection_interval,
                "poll_seconds": 15,
            }
        )
        return result

    def current_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = self._snapshot
        if snapshot is None:
            raise CollectorError("the first storage collection has not completed")
        return self._decorate_snapshot(snapshot)

    def refresh(
        self, force: bool = False, reason: str = "background"
    ) -> dict[str, Any]:
        now = time.time()
        if force and reason == "manual":
            with self._state_lock:
                too_soon = (
                    self._last_manual_attempt is not None
                    and now - self._last_manual_attempt < self.force_min_interval
                )
                snapshot = self._snapshot
                if not too_soon:
                    self._last_manual_attempt = now
            if too_soon and snapshot is not None:
                return self._decorate_snapshot(snapshot, force_deferred=True)

        if not self._refresh_lock.acquire(blocking=False):
            with self._state_lock:
                snapshot = self._snapshot
            if snapshot is not None:
                return self._decorate_snapshot(snapshot, force_deferred=force)
            raise CollectorError("storage collection is already in progress")

        with self._state_lock:
            self._last_attempt = now
        try:
            drives = self.collector.collect_all()
            for drive in drives:
                self.history.record(drive, now)
                drive["projection"] = estimate_days_remaining(
                    self.history.points(drive["id"]),
                    drive.get("endurance_used_percent"),
                    identity_reliable=drive.get("identity_quality") != "path-fallback",
                    endurance_source=drive.get("endurance_source"),
                )
            self.history.prune(now)

            snapshot = {
                "generated_at": _iso_timestamp(now),
                "generated_at_epoch": now,
                "drives": drives,
            }
            self.history.save_snapshot(snapshot, now)
            with self._state_lock:
                self._snapshot = snapshot
                self._last_success = now
                self._last_error = None
                self._consecutive_failures = 0
            return self._decorate_snapshot(snapshot)
        except Exception as error:
            message = str(error) or type(error).__name__
            LOG.exception("storage collection failed")
            with self._state_lock:
                self._last_error = message
                self._consecutive_failures += 1
                snapshot = self._snapshot
            if snapshot is not None:
                return self._decorate_snapshot(snapshot)
            if isinstance(error, CollectorError):
                raise
            raise CollectorError(message) from error
        finally:
            self._refresh_lock.release()

    def snapshot(self, force: bool = False) -> dict[str, Any]:
        """Compatibility facade for callers and tests."""

        if force:
            return self.refresh(force=True, reason="manual")
        try:
            return self.current_snapshot()
        except CollectorError:
            return self.refresh(reason="request")

    def health(self) -> dict[str, Any]:
        try:
            database = self.history.health()
        except sqlite3.Error as error:
            database = {
                "ok": False,
                "quick_check": str(error),
                "schema_version": None,
                "writable": False,
            }
        with self._state_lock:
            last_success = self._last_success
            last_attempt = self._last_attempt
            last_error = self._last_error
            failures = self._consecutive_failures
            has_snapshot = self._snapshot is not None
        age = time.time() - last_success if last_success is not None else None
        snapshot_fresh = age is not None and age <= self.stale_after and not last_error
        ready = bool(database["ok"] and has_snapshot and snapshot_fresh)
        return {
            "status": "ok" if ready else "degraded",
            "ready": ready,
            "database": database,
            "has_snapshot": has_snapshot,
            "snapshot_age_seconds": round(max(0.0, age), 1)
            if age is not None
            else None,
            "last_success_at": _iso_timestamp(last_success) if last_success else None,
            "last_attempt_at": _iso_timestamp(last_attempt) if last_attempt else None,
            "collector_error": last_error,
            "consecutive_failures": failures,
            "configuration_warnings": self.configuration_warnings,
            "collection_interval_seconds": self.collection_interval,
            "stale_after_seconds": self.stale_after,
        }
