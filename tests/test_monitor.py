import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.monitor import (
    CommandResult,
    DriveCollector,
    HistoryStore,
    MonitorService,
    drive_identity_quality,
    estimate_days_remaining,
    parse_nvme_thresholds,
    parse_smartctl_exit_status,
    parse_smartctl_json,
    parse_smartctl_scan_json,
    stable_drive_id,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text()


def nvme_smart_json(used=7, temperature=42, passed=True, critical_warning=0):
    payload = json.loads(fixture("nvme-smart.json"))
    payload["smart_status"]["passed"] = passed
    payload["temperature"]["current"] = temperature
    payload["nvme_smart_health_information_log"]["percentage_used"] = used
    payload["nvme_smart_health_information_log"]["critical_warning"] = critical_warning
    return json.dumps(payload)


def drive_record(**overrides):
    record = {
        "id": "0123456789abcdef",
        "identity_quality": "serial",
        "device": "nvme0n1",
        "path": "/dev/nvme0n1",
        "transport": "nvme",
        "protocol": "nvme",
        "smartctl_type": "nvme",
        "type": "ssd",
        "model": "Example NVMe",
        "serial": "SERIAL-1",
        "wwn": None,
        "size_bytes": 2_000_000_000_000,
        "smart_status": "healthy",
        "temperature_c": 42.0,
        "endurance_used_percent": 7.0,
        "endurance_remaining_percent": 93.0,
        "endurance_source": "nvme-percentage-used",
        "temperature_warning_c": 69.85,
        "temperature_critical_c": 84.85,
        "temperature_status": "normal",
        "smartctl_exit_status": 0,
        "health_warnings": [],
        "collector_errors": [],
    }
    record.update(overrides)
    return record


def test_nvme_smart_data_includes_endurance_and_failure_indicators():
    result = parse_smartctl_json(fixture("nvme-smart.json"), "nvme")

    assert result["smart_status"] == "healthy"
    assert result["temperature_c"] == 42.0
    assert result["endurance_used_percent"] == 7.0
    assert result["endurance_remaining_percent"] == 93.0
    assert result["available_spare_percent"] == 100.0
    assert result["media_errors"] == 0.0
    assert result["error_log_entries"] == 3.0
    assert result["unsafe_shutdowns"] == 2.0
    assert result["power_on_hours"] == 8760.0
    assert result["data_units_written"] == 1234567.0
    assert result["nvme_critical_warnings"] == []


def test_nvme_critical_warning_overrides_a_passing_generic_status():
    result = parse_smartctl_json(
        nvme_smart_json(critical_warning=4),
        "nvme",
    )

    assert result["nvme_critical_warning"] == 4
    assert result["nvme_critical_warnings"] == ["device reliability is degraded"]
    assert result["smart_status"] == "unhealthy"


def test_nvme_endurance_is_bounded_and_rejects_non_finite_values():
    over = parse_smartctl_json(nvme_smart_json(used=123), "nvme")
    negative = parse_smartctl_json(nvme_smart_json(used=-1), "nvme")
    malformed = parse_smartctl_json('{"temperature":{"current":NaN}}', "nvme")

    assert over["endurance_used_percent"] == 123.0
    assert over["endurance_remaining_percent"] == 0.0
    assert negative["endurance_used_percent"] is None
    assert malformed["temperature_c"] is None


def test_nvme_bitmask_and_percent_metrics_reject_invalid_values():
    payload = json.loads(nvme_smart_json())
    log = payload["nvme_smart_health_information_log"]
    log["critical_warning"] = 1.5
    log["available_spare"] = 101
    log["media_errors"] = -1

    result = parse_smartctl_json(json.dumps(payload), "nvme")

    assert result["nvme_critical_warning"] is None
    assert result["available_spare_percent"] is None
    assert result["media_errors"] is None


def test_sata_life_uses_only_an_exact_clearly_labelled_attribute():
    result = parse_smartctl_json(fixture("sata-smart.json"), "sata")
    misleading = {
        "ata_smart_attributes": {
            "table": [{"name": "Not_Really_SSD_Life_Left_Debug", "value": 90}]
        }
    }

    assert result["endurance_remaining_percent"] == 87.0
    assert result["endurance_used_percent"] == 13.0
    assert result["endurance_source"] == "sata-smart-attribute"
    assert (
        parse_smartctl_json(json.dumps(misleading), "sata")[
            "endurance_remaining_percent"
        ]
        is None
    )


def test_smartctl_exit_mask_separates_command_errors_from_health_warnings():
    result = parse_smartctl_exit_status((1 << 1) | (1 << 3) | (1 << 6))

    assert result["errors"] == ["device could not be opened or identified"]
    assert "SMART reports that the drive is failing" in result["warnings"]
    assert "the device error log contains errors" in result["warnings"]
    all_overlapping_bits = parse_smartctl_exit_status(124)
    assert all_overlapping_bits["errors"] == [
        "a SMART command failed or returned invalid data"
    ]
    assert len(all_overlapping_bits["warnings"]) == 4


def test_execution_failure_is_not_mistaken_for_smart_health_bits():
    lsblk = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "model": "Example",
                    "serial": "SERIAL",
                    "wwn": None,
                    "size": 1000,
                    "type": "disk",
                    "tran": "nvme",
                    "rota": False,
                }
            ]
        }
    )

    def runner(args, timeout):
        if args[0] == "lsblk":
            return CommandResult(0, lsblk)
        if args[:2] == ["smartctl", "--scan-open"]:
            return CommandResult(127, "", execution_error="not-found")
        if args[0] == "smartctl":
            return CommandResult(124, "", execution_error="timeout")
        if args[0] == "nvme":
            return CommandResult(127, "", execution_error="not-found")
        raise AssertionError(args)

    drive = DriveCollector(runner).collect_all()[0]

    assert drive["smart_status"] == "unknown"
    assert drive["smartctl_exit_status"] is None
    assert drive["health_warnings"] == []
    assert drive["collector_errors"] == [
        "smartctl timed out",
        "nvme-cli is not installed",
    ]


def test_nvme_temperature_thresholds_are_validated_and_converted():
    result = parse_nvme_thresholds(json.dumps({"wctemp": 343, "cctemp": 358}))
    invalid = parse_nvme_thresholds(json.dumps({"wctemp": float("inf")}))

    assert result["temperature_warning_c"] == 69.85
    assert result["temperature_critical_c"] == 84.85
    assert invalid["temperature_warning_c"] is None


def test_smartctl_scan_supports_native_and_usb_devices_and_rejects_paths():
    payload = json.loads(fixture("smartctl-scan.json"))
    payload["devices"].append(
        {"name": "/dev/../etc/passwd", "type": "sat", "protocol": "ATA"}
    )

    result = parse_smartctl_scan_json(json.dumps(payload))

    assert result["/dev/nvme0n1"]["protocol"] == "nvme"
    assert result["/dev/sdc"] == {"smartctl_type": "sat", "protocol": "sata"}
    assert "/dev/../etc/passwd" not in result


def test_collector_discovers_usb_ssd_and_keeps_failures_per_drive():
    lsblk = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "model": "Fast NVMe",
                    "serial": "N1",
                    "wwn": "eui.001",
                    "size": 2_000,
                    "type": "disk",
                    "tran": "nvme",
                    "rota": False,
                },
                {
                    "name": "sdc",
                    "model": "USB SSD",
                    "serial": "U1",
                    "wwn": None,
                    "size": 1_000,
                    "type": "disk",
                    "tran": "usb",
                    "rota": False,
                },
                {
                    "name": "sdb",
                    "model": "Archive HDD",
                    "serial": "H1",
                    "wwn": None,
                    "size": 3_000,
                    "type": "disk",
                    "tran": "sata",
                    "rota": True,
                },
            ]
        }
    )

    def runner(args, timeout):
        if args[0] == "lsblk":
            return CommandResult(0, lsblk)
        if args[:2] == ["smartctl", "--scan-open"]:
            return CommandResult(0, fixture("smartctl-scan.json"))
        if args[0] == "smartctl":
            path = next(value for value in args if value.startswith("/dev/"))
            if path == "/dev/nvme0n1":
                return CommandResult(1 << 6, nvme_smart_json())
            if path == "/dev/sdc":
                assert args[2:4] == ["--device", "sat"]
                return CommandResult(0, fixture("sata-smart.json"))
            raise RuntimeError("simulated controller crash")
        if args[0] == "nvme":
            return CommandResult(0, json.dumps({"wctemp": 343, "cctemp": 358}))
        raise AssertionError(f"unexpected command: {args}")

    drives = DriveCollector(runner).collect_all()

    assert [(drive["device"], drive["type"]) for drive in drives] == [
        ("nvme0n1", "ssd"),
        ("sdc", "ssd"),
        ("sdb", "hdd"),
    ]
    nvme = next(drive for drive in drives if drive["device"] == "nvme0n1")
    usb = next(drive for drive in drives if drive["device"] == "sdc")
    hdd = next(drive for drive in drives if drive["device"] == "sdb")
    assert "the device error log contains errors" in nvme["health_warnings"]
    assert usb["transport"] == "usb"
    assert usb["protocol"] == "sata"
    assert usb["identity_quality"] == "wwn"
    assert usb["endurance_remaining_percent"] == 87.0
    assert hdd["smart_status"] == "unknown"
    assert hdd["collector_errors"][0].startswith("unexpected collector failure")


def test_hardware_identity_survives_device_renames_and_marks_fallbacks():
    first = stable_drive_id("usb", "SERIAL-X", "Model", "sda")
    renamed = stable_drive_id("sata", "SERIAL-X", "Changed model", "sdc")
    replaced = stable_drive_id("sata", "SERIAL-Y", "Model", "sda")

    assert first == renamed
    assert first != replaced
    assert drive_identity_quality("Unknown", "") == "path-fallback"
    assert drive_identity_quality("SERIAL-X", "") == "serial"
    assert drive_identity_quality("SERIAL-X", "eui.1") == "wwn"


def test_projection_requires_two_counter_steps_over_two_weeks():
    now = time.time()
    one_step = [
        {"observed_at": now - 30 * 86400, "used_percent": 4},
        {"observed_at": now, "used_percent": 5},
    ]
    enough = [
        {"observed_at": now - 30 * 86400, "used_percent": 4},
        {"observed_at": now - 15 * 86400, "used_percent": 5},
        {"observed_at": now, "used_percent": 7},
    ]

    assert estimate_days_remaining(one_step, 5)["status"] == "insufficient-wear-change"
    projection = estimate_days_remaining(enough, 7)
    assert projection["status"] == "estimated"
    assert projection["confidence"] == "medium"
    assert projection["days_remaining_low"] < projection["days_remaining"]
    assert projection["days_remaining"] < projection["days_remaining_high"]


def test_projection_starts_over_after_a_counter_reset():
    now = time.time()
    points = [
        {"observed_at": now - 80 * 86400, "used_percent": 40},
        {"observed_at": now - 60 * 86400, "used_percent": 41},
        {"observed_at": now - 45 * 86400, "used_percent": 1},
        {"observed_at": now - 22 * 86400, "used_percent": 2},
        {"observed_at": now, "used_percent": 3},
    ]

    projection = estimate_days_remaining(points, 3)

    assert projection["status"] == "estimated"
    assert projection["reset_detected"] is True
    assert projection["wear_delta_percent"] == 2


@pytest.mark.parametrize(
    ("identity_reliable", "source", "expected"),
    [
        (False, "nvme-percentage-used", "unstable-identity"),
        (True, "sata-smart-attribute", "unsupported-source"),
    ],
)
def test_projection_rejects_unstable_identity_and_vendor_specific_sources(
    identity_reliable, source, expected
):
    assert (
        estimate_days_remaining(
            [],
            5,
            identity_reliable=identity_reliable,
            endurance_source=source,
        )["status"]
        == expected
    )


def test_history_store_enables_wal_persists_snapshots_and_reports_health(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    now = time.time()
    drive = drive_record()
    store.record(drive, now)
    store.save_snapshot({"generated_at": "now", "drives": [drive]}, now)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.points(drive["id"], hours=1)[0]["used_percent"] == 7
    assert store.load_snapshot()["drives"][0]["serial"] == "SERIAL-1"
    assert store.health()["ok"] is True


def test_history_store_migrates_legacy_rows_and_enforces_constraints(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE observations (
                device_id TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                used_percent REAL,
                temperature_c REAL,
                smart_status TEXT NOT NULL,
                PRIMARY KEY (device_id, observed_at)
            );
            INSERT INTO observations VALUES ('0123456789abcdef', 1, 7, 42, 'healthy');
            INSERT INTO observations VALUES ('0123456789abcdef', 2, 999, 9999, 'unknown');
            """
        )

    HistoryStore(path)

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT used_percent, temperature_c FROM observations ORDER BY observed_at"
        ).fetchall()
        assert rows == [(7.0, 42.0), (None, None)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO observations VALUES ('fedcba9876543210', 3, 999, 42, 'unknown')"
            )


def test_history_store_rejects_corrupt_nonfinite_snapshot(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO latest_snapshot VALUES (1, 1, ?)",
            ('{"drives": [{"temperature_c": Infinity}]}',),
        )

    assert store.load_snapshot() is None


def test_history_store_preserves_valid_values_during_transient_failure(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    now = float(int(time.time() // 60 * 60) + 10)
    store.record(drive_record(), now)
    store.record(
        drive_record(
            endurance_used_percent=None,
            temperature_c=None,
            smart_status="unknown",
        ),
        now + 20,
    )

    point = store.points("0123456789abcdef", hours=1)[0]
    assert point["used_percent"] == 7
    assert point["temperature_c"] == 42
    assert point["smart_status"] == "unknown"


def test_history_store_downsamples_api_series_and_preserves_endpoints(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    start = time.time() - 10 * 60
    for index in range(10):
        store.record(
            drive_record(endurance_used_percent=float(index)), start + index * 60
        )

    points = store.points("0123456789abcdef", hours=1, max_points=3)

    assert len(points) == 3
    assert points[0]["used_percent"] == 0
    assert points[-1]["used_percent"] == 9


def test_history_store_persists_retention_schedule(tmp_path):
    path = tmp_path / "history.sqlite3"
    now = time.time()
    store = HistoryStore(path)
    store.prune(now)

    reopened = HistoryStore(path)

    assert reopened._last_prune == now


def test_monitor_returns_persisted_stale_snapshot_after_collection_failure(tmp_path):
    class Collector:
        def __init__(self):
            self.fail = False

        def collect_all(self):
            if self.fail:
                raise RuntimeError("temporary inventory failure")
            return [drive_record()]

    path = tmp_path / "history.sqlite3"
    collector = Collector()
    first = MonitorService(
        collector=collector,
        history=HistoryStore(path),
        collection_interval=60,
        stale_after=180,
    )
    fresh = first.refresh()
    collector.fail = True
    stale = first.refresh()

    assert fresh["stale"] is False
    assert stale["stale"] is True
    assert stale["collector_error"] == "temporary inventory failure"
    restarted = MonitorService(
        collector=collector,
        history=HistoryStore(path),
        collection_interval=60,
        stale_after=180,
    )
    assert restarted.current_snapshot()["drives"][0]["serial"] == "SERIAL-1"


def test_manual_force_refresh_is_rate_limited(tmp_path):
    class Collector:
        def __init__(self):
            self.calls = 0

        def collect_all(self):
            self.calls += 1
            return [drive_record()]

    collector = Collector()
    service = MonitorService(
        collector=collector,
        history=HistoryStore(tmp_path / "history.sqlite3"),
        force_min_interval=60,
    )

    service.refresh(force=True, reason="manual")
    deferred = service.refresh(force=True, reason="manual")

    assert collector.calls == 1
    assert deferred["force_deferred"] is True
