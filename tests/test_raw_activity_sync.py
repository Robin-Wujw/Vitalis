"""Synthetic routing tests for the two independent cloud archive streams."""
from datetime import datetime, timedelta, timezone
import sys
from types import SimpleNamespace

from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.zepp.client import MockZeppClient
from vitalis.adapters.zepp.dense_hr import DecodedDenseHeartRate
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import SyncControl, ZeppSyncCoordinator
from vitalis.adapters.zepp import sync_manager
from vitalis.domain import Device, MetricSample


START = datetime(2026, 11, 1, tzinfo=timezone.utc)
WINDOW = FetchWindow(START, START + timedelta(days=1))
NOW = START + timedelta(days=2)


def _index(file_type, devices):
    return {"items": [{"value": {
        "startTime": int(START.timestamp() * 1000),
        "samples": [{
            "s": 0, "e": 60_000, "fileId": "shared-id", "fileType": file_type,
            "dateString": "2026-11-01", "deviceId": device,
        } for device in devices],
    }}]}


class MixedConnector:
    source_mode = "mock"

    def __init__(self):
        self.index_calls = []
        self.downloads = []

    def fetch_file_info_events(self, event_type, sub_type, from_ms, to_ms, limit):
        self.index_calls.append((event_type, sub_type, from_ms, to_ms, limit))
        if event_type == "second_heart_rate":
            return _index("SEC_HR", ["DEVICE000A", "DEVICE000A"])
        assert event_type == "active_origin_data" and sub_type == "real_data"
        return _index("ACTIVE_ORIGIN_DATA", ["DEVICE000A", "DEVICE000B", "DEVICE000B"])

    def download_dense_file(self, file_type, file_id):
        self.downloads.append((file_type, file_id))
        return f"synthetic:{file_type}:{file_id}".encode()


def _create(user_id, connector, **kwargs):
    with session_scope() as db:
        HealthRepository(db).delete_for_user(user_id)
    coordinator = ZeppSyncCoordinator(connector=connector, wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt(user_id, window=WINDOW, timezone_name="Asia/Shanghai", **kwargs)
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(attempt.id):
            if chunk.stream != "dense_files":
                chunk.status = "succeeded"
    return coordinator, attempt


def test_normal_manifest_includes_activity_origin_index_without_expanding_probes():
    coordinator = ZeppSyncCoordinator()
    roots = [row for row in coordinator._manifest(WINDOW, {}) if row["stream"] == "dense_files"]
    assert {(row["partition"], row["health_stream"]) for row in roots} == {
        ("dense_index", "heart_rate/dense_file"),
        ("activity_origin_index", "activity/raw_file"),
    }
    activity = next(row for row in roots if row["partition"] == "activity_origin_index")
    assert activity["stages"]["params"]["event_type"] == "active_origin_data"
    assert activity["stages"]["params"]["sub_type"] == "real_data"
    for options in ({"sleep_only": True}, {"workout_only": True}):
        assert not any(row["stream"] == "dense_files" for row in coordinator._manifest(WINDOW, options))


def test_activity_index_fetch_uses_event_params_and_distinct_source_key():
    connector = MixedConnector()
    coordinator = ZeppSyncCoordinator(connector=connector)
    chunk = coordinator._spec(
        "dense_files", "activity_origin_index", WINDOW,
        health_stream="activity/raw_file", operation="fetch_file_info_events",
        params={"event_type": "active_origin_data", "sub_type": "real_data",
                "from_ms": int(START.timestamp() * 1000), "to_ms": int(WINDOW.end.timestamp() * 1000), "limit": 2000},
    )
    result = coordinator._fetch_chunk(chunk, SyncControl(), connector, "Asia/Shanghai")
    assert connector.index_calls[0][:2] == ("active_origin_data", "real_data")
    assert result.record.raw.source_key.startswith("file_info:active_origin_data:")


def test_same_id_different_types_are_queued_once_and_decode_is_stream_scoped(monkeypatch):
    calls = []

    def decode_sec(archive, files):
        assert archive == b"synthetic:SEC_HR:shared-id"
        assert {(item.stream, item.file_type) for item in files} == {("second_heart_rate", "SEC_HR")}
        calls.append(("sec", {item.device_id for item in files}))
        return DecodedDenseHeartRate(
            samples=[MetricSample(metric="heart_rate", timestamp=START, value=61, unit="bpm",
                                  source_scope="device", device_id="DEVICE000A", source_record_id="original-sec-id")],
            files=[item.model_copy(update={"parse_status": "decoded", "sample_count": 1}) for item in files],
            entry_count=1,
        )

    def decode_activity(archive, files, timezone_name, device_aliases):
        assert archive == b"synthetic:ACTIVE_ORIGIN_DATA:shared-id"
        assert timezone_name == "Asia/Shanghai"
        assert device_aliases["SERIAL000A"] == "DEVICE000A"
        assert device_aliases["SERIAL000B"] == "DEVICE000B"
        assert {(item.stream, item.file_type) for item in files} == {("activity_origin", "ACTIVE_ORIGIN_DATA")}
        calls.append(("activity", {item.device_id for item in files}))
        identities = [("device", "DEVICE000A", 62), ("device", "DEVICE000B", 70), ("unknown", None, 65)]
        return DecodedDenseHeartRate(
            samples=[MetricSample(metric="heart_rate", timestamp=START, value=value, unit="bpm",
                                  source_scope=scope, device_id=device, source_record_id=f"original-activity-{scope}-{device}")
                     for scope, device, value in identities],
            files=[item.model_copy(update={"parse_status": "decoded", "sample_count": 1}) for item in files],
            entry_count=3,
        )

    monkeypatch.setattr(sync_manager, "decode_sec_hr_archive", decode_sec)
    monkeypatch.setitem(sys.modules, "vitalis.adapters.zepp.raw_activity", SimpleNamespace(decode_activity_archive=decode_activity))
    connector = MixedConnector()
    coordinator, attempt = _create("activity-durable-routing", connector)
    with session_scope() as db:
        repo = HealthRepository(db)
        for suffix in ("A", "B"):
            repo.upsert_device(Device(
                user_id=attempt.user_id, source="zepp", device_id=f"DEVICE000{suffix}",
                aliases=[f"SERIAL000{suffix}"],
            ))
    coordinator.drain_once(attempt.id)
    coordinator.drain_once(attempt.id)
    chunks = coordinator.status(attempt.id)["chunks"]
    archives = [row for row in chunks if row["stream"] == "dense_archive"]
    assert len(archives) == 2
    assert {row["health_stream"] for row in archives} == {"activity/raw_archive", "heart_rate/dense_archive"}
    assert connector.downloads == []
    for _ in range(2):
        coordinator.drain_once(attempt.id)
    assert coordinator.report(attempt.id).success
    assert set(connector.downloads) == {("SEC_HR", "shared-id"), ("ACTIVE_ORIGIN_DATA", "shared-id")}
    assert len(connector.downloads) == 2
    assert set(kind for kind, _devices in calls) == {"sec", "activity"}
    with session_scope() as db:
        repo = HealthRepository(db)
        assert len(repo.dense_data_file_group(attempt.user_id, "second_heart_rate", "shared-id")) == 1
        assert len(repo.dense_data_file_group(attempt.user_id, "activity_origin", "shared-id")) == 2
        rows = db.execute(select(orm.MetricSample).where(orm.MetricSample.user_id == attempt.user_id)).scalars().all()
        assert {(row.source_scope, row.device_id, row.value) for row in rows} == {
            ("device", "DEVICE000A", 61), ("device", "DEVICE000A", 62),
            ("device", "DEVICE000B", 70), ("unknown", "", 65),
        }
        assert {row.source_record_id for row in rows} == {
            "original-sec-id", "original-activity-device-DEVICE000A", "original-activity-device-DEVICE000B", "original-activity-unknown-None",
        }


def test_activity_metadata_only_is_partial_and_preserves_stream_namespace():
    connector = MixedConnector()
    coordinator, attempt = _create("activity-index-only", connector, options={"decode_dense_files": False})
    report = coordinator.run_attempt(attempt.id)
    assert not report.success
    assert connector.downloads == []
    activity = next(stream for stream in report.streams if stream.stream == "activity/raw_file")
    assert activity.capability == "indexed" and activity.error_kind == "partial_coverage"
    with session_scope() as db:
        rows = HealthRepository(db).dense_data_file_group(attempt.user_id, "activity_origin", "shared-id")
        assert len(rows) == 2
        assert {row.parse_status for row in rows} == {"indexed"}


def test_mock_activity_index_is_explicitly_empty():
    assert MockZeppClient().fetch_file_info_events("active_origin_data", "real_data", 0, 1000) == {"items": []}
