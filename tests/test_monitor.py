import json
import time

from app.monitor import (
    CommandResult,
    DriveCollector,
    HistoryStore,
    estimate_days_remaining,
    parse_nvme_thresholds,
    parse_smartctl_json,
)


def nvme_smart_json(used=7, temperature=42, passed=True):
    return json.dumps(
        {
            "smart_status": {"passed": passed},
            "temperature": {"current": temperature},
            "nvme_smart_health_information_log": {"percentage_used": used},
        }
    )


def test_nvme_smart_data_is_normalized_even_when_smartctl_has_warning_exit_code():
    result = parse_smartctl_json(nvme_smart_json(), "nvme")

    assert result == {
        "smart_status": "healthy",
        "temperature_c": 42.0,
        "endurance_used_percent": 7.0,
        "endurance_remaining_percent": 93.0,
        "endurance_source": "nvme-percentage-used",
    }


def test_nvme_endurance_can_exceed_rated_endurance_but_remaining_is_clamped():
    result = parse_smartctl_json(nvme_smart_json(used=123), "nvme")

    assert result["endurance_used_percent"] == 123.0
    assert result["endurance_remaining_percent"] == 0.0


def test_missing_smart_status_is_unknown_not_healthy():
    result = parse_smartctl_json(json.dumps({"temperature": {"current": 39}}), "nvme")

    assert result["smart_status"] == "unknown"
    assert result["temperature_c"] == 39.0


def test_sata_life_only_accepts_a_clearly_labelled_remaining_attribute():
    sata = {
        "smart_status": {"passed": True},
        "temperature": {"current": 36},
        "ata_smart_attributes": {
            "table": [
                {"name": "Wear_Leveling_Count", "value": 98, "raw": {"value": 12}},
                {"name": "Media_Wearout_Indicator", "value": 87, "raw": {"value": 13}},
            ]
        },
    }

    result = parse_smartctl_json(json.dumps(sata), "sata")

    assert result["endurance_remaining_percent"] == 87.0
    assert result["endurance_used_percent"] == 13.0
    assert result["endurance_source"] == "sata-smart-attribute"


def test_sata_generic_wear_counter_is_not_misreported_as_life_percentage():
    sata = {
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [{"name": "Wear_Leveling_Count", "value": 98}]
        },
    }

    result = parse_smartctl_json(json.dumps(sata), "sata")

    assert result["endurance_remaining_percent"] is None


def test_malformed_sata_attribute_data_is_treated_as_unavailable():
    sata = {"smart_status": {"passed": True}, "ata_smart_attributes": None}

    result = parse_smartctl_json(json.dumps(sata), "sata")

    assert result["smart_status"] == "healthy"
    assert result["endurance_remaining_percent"] is None


def test_nvme_temperature_thresholds_are_converted_from_kelvin():
    result = parse_nvme_thresholds(json.dumps({"wctemp": 343, "cctemp": 358}))

    assert result["temperature_warning_c"] == 69.85
    assert result["temperature_critical_c"] == 84.85


def test_collector_discovers_ssds_and_hdds_and_keeps_per_drive_failures_local():
    lsblk = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "nvme0n1",
                    "model": "Fast NVMe",
                    "serial": "N1",
                    "size": 2_000,
                    "type": "disk",
                    "tran": "nvme",
                    "rota": False,
                },
                {
                    "name": "sda",
                    "model": "SATA SSD",
                    "serial": "S1",
                    "size": 1_000,
                    "type": "disk",
                    "tran": "sata",
                    "rota": False,
                },
                {
                    "name": "sdb",
                    "model": "Archive HDD",
                    "serial": "H1",
                    "size": 3_000,
                    "type": "disk",
                    "tran": "sata",
                    "rota": True,
                },
                {
                    "name": "sdc1",
                    "model": "Partition",
                    "serial": "P1",
                    "size": 100,
                    "type": "part",
                    "tran": "sata",
                    "rota": False,
                },
                {
                    "name": "..",
                    "model": "Malformed",
                    "serial": "M1",
                    "size": 100,
                    "type": "disk",
                    "tran": "sata",
                    "rota": False,
                },
            ]
        }
    )

    def runner(args, timeout):
        if args[0] == "lsblk":
            return CommandResult(0, lsblk)
        if args[0] == "smartctl" and args[2] == "/dev/nvme0n1":
            return CommandResult(2, nvme_smart_json(used=7, temperature=44))
        if args[0] == "smartctl" and args[2] == "/dev/sda":
            return CommandResult(
                0,
                json.dumps(
                    {"smart_status": {"passed": True}, "temperature": {"current": 35}}
                ),
            )
        if args[0] == "smartctl" and args[2] == "/dev/sdb":
            return CommandResult(127, "", "smartctl missing")
        if args[0] == "nvme":
            return CommandResult(0, json.dumps({"wctemp": 343, "cctemp": 358}))
        raise AssertionError(f"unexpected command: {args}")

    drives = DriveCollector(runner).collect_all()

    assert [(drive["device"], drive["type"]) for drive in drives] == [
        ("nvme0n1", "ssd"),
        ("sda", "ssd"),
        ("sdb", "hdd"),
    ]
    nvme = next(drive for drive in drives if drive["device"] == "nvme0n1")
    assert nvme["endurance_remaining_percent"] == 93.0
    assert nvme["temperature_warning_c"] == 69.85
    hdd = next(drive for drive in drives if drive["device"] == "sdb")
    assert hdd["smart_status"] == "unknown"
    assert hdd["collector_errors"] == ["smartctl is not installed"]


def test_projection_requires_an_hour_and_increasing_wear():
    now = float(int(time.time() // 60 * 60) + 10)
    points = [
        {"observed_at": now - 2 * 86400, "used_percent": 4},
        {"observed_at": now, "used_percent": 6},
    ]

    projection = estimate_days_remaining(points, 6)

    assert projection["status"] == "estimated"
    assert projection["daily_rate_percent"] == 1.0
    assert projection["days_remaining"] == 94.0


def test_projection_does_not_invent_a_date_when_wear_has_not_changed():
    now = time.time()
    projection = estimate_days_remaining(
        [
            {"observed_at": now - 2 * 86400, "used_percent": 4},
            {"observed_at": now, "used_percent": 4},
        ],
        4,
    )

    assert projection["status"] == "no-wear-observed"
    assert projection["days_remaining"] is None


def test_history_store_buckets_samples_and_returns_recent_points(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    now = float(int(time.time() // 60 * 60) + 10)
    drive = {
        "id": "0123456789abcdef",
        "endurance_used_percent": 5,
        "temperature_c": 40,
        "smart_status": "healthy",
    }

    store.record(drive, now)
    store.record({**drive, "endurance_used_percent": 6}, now + 20)
    points = store.points(drive["id"], hours=1)

    assert len(points) == 1
    assert points[0]["used_percent"] == 6


def test_history_store_does_not_erase_a_valid_sample_after_a_transient_read_failure(
    tmp_path,
):
    store = HistoryStore(tmp_path / "history.sqlite3")
    now = float(int(time.time() // 60 * 60) + 10)
    drive = {
        "id": "0011223344556677",
        "endurance_used_percent": 5,
        "temperature_c": 40,
        "smart_status": "healthy",
    }

    store.record(drive, now)
    store.record(
        {
            **drive,
            "endurance_used_percent": None,
            "temperature_c": None,
            "smart_status": "unknown",
        },
        now + 20,
    )
    points = store.points(drive["id"], hours=1)

    assert points[0]["used_percent"] == 5
    assert points[0]["temperature_c"] == 40
    assert points[0]["smart_status"] == "unknown"


def test_history_store_rejects_unbounded_history_windows(tmp_path):
    store = HistoryStore(tmp_path / "history.sqlite3")
    now = time.time()
    store.record(
        {
            "id": "fedcba9876543210",
            "endurance_used_percent": 5,
            "temperature_c": 40,
            "smart_status": "healthy",
        },
        now,
    )

    assert store.points("fedcba9876543210", hours=24 * 365) == store.points(
        "fedcba9876543210", hours=24 * 90
    )
