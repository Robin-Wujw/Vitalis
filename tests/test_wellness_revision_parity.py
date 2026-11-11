"""Synthetic PAI revisions, vendor temperature raw values and SpO2 metadata."""
from datetime import UTC, datetime
import json
import pytest

from vitalis.adapters.zepp.parser import ZeppParser

STAMP = datetime(2026, 9, 1, 16, tzinfo=UTC)


def test_pai_selects_latest_upload_per_local_day_and_preserves_every_zone():
    old = {"time": STAMP.timestamp() * 1000, "uploadTimestamp": STAMP.timestamp() * 1000,
           "dailyPai": 5, "totalPai": 50, "deviceId": "WATCHONE"}
    latest = {**old, "uploadTimestamp": (STAMP.timestamp() + 3600) * 1000, "dailyPai": 25,
              "totalPai": 70, "lowZonePai": 2, "mediumZonePai": 8, "highZonePai": 15,
              "lowZoneMinutes": 30, "mediumZoneMinutes": 10, "highZoneMinutes": 5,
              "lowZoneLowerLimit": 110, "mediumZoneLowerLimit": 130, "highZoneLowerLimit": 155,
              "restHr": 58, "maxHr": 180}
    first, _ = ZeppParser.parse_wellness({"items": [latest, old]}, "wellness:pai:user", timezone_name="Asia/Singapore")
    second, _ = ZeppParser.parse_wellness({"items": [old, latest]}, "wellness:pai:user", timezone_name="Asia/Singapore")
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
    rows = {r.metric: r for r in first}
    assert rows["pai_daily"].value == 25 and rows["pai_total"].value == 70
    assert rows["pai_high_zone_minutes"].value == 5
    assert rows["pai_high_zone_lower_limit"].value == 155
    assert all(str(r.date) == "2026-09-02" for r in first)
    assert rows["pai_daily"].revision_at == datetime(2026, 9, 1, 17, tzinfo=UTC)
    assert rows["pai_daily"].source_field == "dailyPai"


def test_pai_sentinels_rejected_and_device_revisions_not_merged():
    items = [{"time": STAMP.timestamp() * 1000, "dailyPai": value,
              "totalPai": -1, "restHr": 255, "highZoneMinutes": -1,
              "deviceId": device} for value, device in ((1, "WATCHONE"), (2, "WATCHTWO"))]
    rows, _ = ZeppParser.parse_wellness({"items": items}, "wellness:pai:user")
    assert len(rows) == 2 and {row.value for row in rows} == {1, 2}


def test_readiness_raw_temperature_and_status_are_qualified_separately():
    value = {"timestamp": STAMP.timestamp() * 1000, "rdnsScore": 80,
             "skinTempCalibrated": -35, "skinTempBaseLine": 10, "status": 2}
    rows = ZeppParser.parse_readiness_samples({"items": [{"eventType": "readiness",
        "timestamp": (STAMP.timestamp() + 100) * 1000, "value": json.dumps(value)}]}, "Asia/Singapore")
    fields = {row.metric: row for row in rows}
    assert fields["skin_temperature_offset_raw"].value == -35
    assert fields["skin_temperature_offset_raw"].unit == "vendor_raw"
    assert fields["skin_temperature_offset_raw"].qualification == "vendor_unit_unverified"
    assert fields["skin_temp_delta"].value == pytest.approx(-.35)
    assert fields["readiness_status"].value == 2


def test_spo2_dict_or_json_extra_preserves_auto_flag_without_inferring_it():
    rows = [{"subType": "click", "extra": extra} for extra in (
        {"timestamp": STAMP.timestamp() * 1000, "spo2": 97, "isAuto": True},
        json.dumps({"timestamp": (STAMP.timestamp() + 60) * 1000, "spo2": 98, "isAuto": False}),
        {"timestamp": (STAMP.timestamp() + 120) * 1000, "spo2": 99},
    )]
    _, samples = ZeppParser.parse_wellness({"items": rows}, "wellness:spo2:user")
    assert [row.value for row in samples if row.metric == "spo2"] == [97, 98, 99]
    assert [row.value for row in samples if row.metric == "spo2_automatic"] == [1, 0]


@pytest.mark.parametrize("new_first", [False, True])
def test_pai_cross_payload_revision_retracts_missing_fields_and_keeps_devices(new_first):
    from datetime import timedelta
    from types import SimpleNamespace
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
    from vitalis.adapters.zepp.sync_manager import SyncManager
    from vitalis.domain import User

    owner = f"pai-revision-{new_first}"
    old = {"time": STAMP.timestamp() * 1000, "uploadTimestamp": STAMP.timestamp() * 1000,
           "dailyPai": 10, "totalPai": 55, "highZonePai": 8, "deviceId": "WATCHONE"}
    new = {"time": STAMP.timestamp() * 1000, "uploadTimestamp": (STAMP.timestamp() + 3600) * 1000,
           "dailyPai": 20, "deviceId": "WATCHONE"}
    second_device = {**old, "deviceId": "WATCHTWO", "dailyPai": 7}
    manager = SyncManager(SimpleNamespace(connector=SimpleNamespace(source_mode="mock"), timezone_name="Asia/Singapore"))
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(owner)
        db.flush()
        repo.bind_source_mode(owner, "mock")

        def save(item):
            record = FetchedRecord(RawRecord("wellness", "wellness:pai:user:synthetic", STAMP - timedelta(days=1),
                STAMP + timedelta(days=1), {"items": [item]}))
            report = manager._persist_record(record, repo, User(id=owner))
            assert report.status == "success"

        save(second_device)
        for item in ([new, old] if new_first else [old, new]):
            save(item)
        rows = repo.daily_metrics(owner, STAMP.date(), (STAMP + timedelta(days=1)).date())
        one = {r.metric: r.value for r in rows if r.device_id == "WATCHONE"}
        two = {r.metric: r.value for r in rows if r.device_id == "WATCHTWO"}
        assert one == {"pai_daily": 20}
        assert two["pai_daily"] == 7 and two["pai_total"] == 55
        revision = repo.analysis_input_revision(owner)
        save(old)
        assert repo.analysis_input_revision(owner) == revision
        save({**new, "uploadTimestamp": (STAMP.timestamp() + 7200) * 1000, "dailyPai": -1})
        assert not [r for r in repo.daily_metrics(owner, STAMP.date(), (STAMP + timedelta(days=1)).date()) if r.device_id == "WATCHONE"]
        assert repo.analysis_input_revision(owner) > revision
