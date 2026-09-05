"""测试 Zepp 同步管理器（SyncManager）。"""

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy.exc import OperationalError

from vitalis.connectors.zepp.client import ZeppAuthError
from vitalis.connectors.zepp.fetcher import (
    FetchBatch,
    FetchedRecord,
    FetchWindow,
    PartialFetchError,
    RawRecord,
)
from vitalis.connectors.zepp.sync_manager import SyncManager
from vitalis.models import User, Workout, WorkoutType
from vitalis.storage import HealthRepository, init_db, session_scope


@pytest.mark.parametrize(
    ("stream", "source_key", "expected"),
    [
        ("wellness", "wellness:spo2:user:2026-08-01:2026-08-02", "wellness/spo2_point"),
        ("wellness", "wellness:spo2:user_day:2026-08-01:2026-08-02:odi", "wellness/spo2_odi"),
        ("wellness", "wellness:spo2:user_day:2026-08-01:2026-08-02:osa_event", "wellness/spo2_osa"),
        ("heart_rate", "heart_rate:1:2", "heart_rate/minute_endpoint"),
        ("dense_files", "file_info:second_heart_rate:2026-08-01:2026-08-02", "heart_rate/dense_file"),
    ],
)
def test_diagnostic_streams_keep_independent_substream_health(
    stream, source_key, expected
):
    record = FetchedRecord(raw=RawRecord(
        stream=stream,
        source_key=source_key,
        start_utc=datetime.now(timezone.utc),
        end_utc=datetime.now(timezone.utc),
        payload={"items": []},
    ))

    assert SyncManager._diagnostic_stream(record) == expected


class MockDataFetcher:
    """Mock DataFetcher：返回空但格式正确的记录。"""

    def __init__(self):
        self.calls: list[str] = []

    def fetch_heart_rate_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("heart_rate")
        return [
            FetchedRecord(
                raw=RawRecord(
                    stream="heart_rate",
                    source_key="hr:test",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload={"items": [{"timestamp": 1, "value": 72}]},
                )
            )
        ]

    def fetch_daily_statistics_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("daily_summary")
        return [
            FetchedRecord(
                raw=RawRecord(
                    stream="daily_summary",
                    source_key="ds:test",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload={"data": {"items": []}},
                )
            )
        ]

    def fetch_workout_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("workouts")
        return [
            FetchedRecord(
                raw=RawRecord(
                    stream="workouts",
                    source_key="wo:test",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload={"data": {"items": []}},
                )
            )
        ]

    def fetch_sleep_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("sleep")
        return [
            FetchedRecord(
                raw=RawRecord(
                    stream="sleep",
                    source_key="sl:test",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload={"data": {"items": []}},
                )
            )
        ]

    def fetch_hrv_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("hrv")
        return [
            FetchedRecord(
                raw=RawRecord(
                    stream="hrv",
                    source_key="hrv:test",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload={"data": {"items": []}},
                )
            )
        ]

    def fetch_wellness_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("wellness")
        return []

    def fetch_dense_file_records(self, window: FetchWindow) -> list[FetchedRecord]:
        self.calls.append("dense_files")
        return []


def _varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _dense_archive(start: datetime, values: list[int]) -> bytes:
    inner = b"\x08" + _varint(int(start.timestamp()))
    inner += b"".join(b"\x10" + _varint(value) for value in values)
    protobuf = b"\x0a" + _varint(len(inner)) + inner
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as zipped:
        zipped.writestr("heart.pb", protobuf)
    return output.getvalue()


@pytest.fixture(scope="function")
def setup_db():
    init_db()
    yield


@pytest.fixture
def mock_fetcher():
    return MockDataFetcher()


class TestSyncManager:
    def test_sync_report_success(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="test-001")
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user("test-001", name="Test", source="zepp")
            report = manager.sync_report(user, days=7, repo=repo)
            states = {row.stream: row for row in repo.sync_stream_states(user.id)}

        assert report.success is True
        stream_names = [s.stream for s in report.streams]
        assert "heart_rate" in stream_names
        assert "daily_summary" in stream_names
        assert "workouts" in stream_names
        assert "sleep" in stream_names
        assert "hrv" in stream_names
        assert "wellness" in stream_names
        assert "dense_files" in stream_names
        assert report.records_written >= 0
        assert states["heart_rate"].fetch_status == "success"
        assert states["heart_rate"].parse_status == "success"
        assert states["heart_rate"].write_status == "success"
        assert states["heart_rate"].last_sample_at == datetime(1970, 1, 1, 0, 0, 1)
        assert states["wellness"].fetch_status == "unavailable"

    def test_valid_duplicate_upsert_remains_successful(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="duplicate-heart-rate-user")
        record = mock_fetcher.fetch_heart_rate_records(FetchWindow.days_back(1))[0]
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            first = manager._persist_record(record, repo, user)
            duplicate = manager._persist_record(record, repo, user)

        assert first.write_status == "success"
        assert duplicate.status == "success"
        assert duplicate.parse_status == "success"
        assert duplicate.write_status == "success"
        assert duplicate.error_kind is None

    def test_aggregate_marks_partial_unrecognized_core_data_unverified(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher)
        user = User(id="mixed-heart-rate-user")
        window = FetchWindow.days_back(1)
        valid = mock_fetcher.fetch_heart_rate_records(window)[0]
        unknown = FetchedRecord(raw=RawRecord(
            stream="heart_rate",
            source_key="hr:unknown",
            start_utc=window.start,
            end_utc=window.end,
            payload={"items": [{"unknown": "shape"}]},
        ))
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            aggregate = manager._persist_records(
                "heart_rate", [valid, unknown], repo, user
            )
            manager._save_stream_health(repo, user, [aggregate])
            state = repo.sync_stream_states(user.id)[0]

        assert aggregate.status == "unverified"
        assert aggregate.parse_status == "unrecognized"
        assert state.stream == "heart_rate"
        assert state.parse_status == "unrecognized"
        assert state.write_status == "success"
        assert state.error_kind == "unrecognized_payload"

    def test_sync_refreshes_device_inventory_for_analysis_labels(
        self, mock_fetcher, setup_db
    ):
        class InventoryClient:
            @staticmethod
            def fetch_devices():
                return {"items": [{
                    "macAddress": "CE:4A:84:92:1F:A6",
                    "additionalInfo": '{"productId":"157"}',
                }]}

        mock_fetcher.connector = InventoryClient()
        manager = SyncManager(mock_fetcher)
        user = User(id="device-inventory-user")
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager.sync_report(user, days=2, repo=repo)
            devices = repo.devices(user.id)

        assert report.success is True
        assert [(item.model, item.device_id) for item in devices] == [
            ("Amazfit Helio Strap", "CE4A84921FA6")
        ]

    def test_sync_report_cancel(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="test-002")

        def cancel_on_first(_progress):
            manager.request_cancel()

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user("test-002", name="Test", source="zepp")
            with pytest.raises(Exception, match="同步已取消"):
                manager.sync_report(user, days=7, repo=repo, on_progress=cancel_on_first)

    def test_sync_timeout_keeps_completed_streams(self, mock_fetcher, setup_db, monkeypatch):
        from vitalis.connectors.zepp import sync_manager as sync_module

        clock = iter([0, 0, 100])
        monkeypatch.setattr(sync_module.time, "monotonic", lambda: next(clock))
        user = User(id="partial-timeout-user")

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = SyncManager(mock_fetcher).sync_report(user, days=7, repo=repo)

        assert report.success is False
        assert report.records_written == 1
        assert "已保存此前完成的数据流" in report.message
        assert report.streams[0].stream == "heart_rate"
        assert report.streams[-1].stream == "daily_summary"
        assert report.streams[-1].status == "failed"

    def test_failure_report(self, mock_fetcher):
        manager = SyncManager(mock_fetcher)
        err = ZeppAuthError("boom")
        report = manager._failure_report("test_stream", err)
        assert report.stream == "test_stream"
        assert report.status == "failed"
        assert report.message == "boom"

    def test_unavailable_report(self, mock_fetcher):
        manager = SyncManager(mock_fetcher)
        err = ZeppAuthError("unavailable")
        report = manager._unavailable_report("test_stream", err)
        assert report.stream == "test_stream"
        assert report.status == "unavailable"

    def test_partial_fetch_batch_is_unverified_and_blocks_success(self, mock_fetcher):
        window = FetchWindow.days_back(2)
        batch = FetchBatch(expected_chunks=2)
        batch.add_success(mock_fetcher.fetch_heart_rate_records(window)[0])
        batch.add_unavailable(FetchWindow(start=window.start, end=window.start + timedelta(days=1)))

        stream = SyncManager(mock_fetcher)._persist_records(
            "heart_rate", batch, None, User(id="partial-user")
        )

        assert stream.status == "unverified"
        assert stream.fetch_status == "partial"
        assert stream.error_kind == "partial_coverage"

    def test_terminal_fetch_failure_persists_completed_chunks(
        self, mock_fetcher, setup_db
    ):
        window = FetchWindow.days_back(2)
        batch = FetchBatch(expected_chunks=2)
        batch.add_success(mock_fetcher.fetch_heart_rate_records(window)[0])

        def fail_heart_rate(_window):
            raise PartialFetchError(
                ZeppAuthError("offline", kind="network"), batch
            )

        mock_fetcher.fetch_heart_rate_records = fail_heart_rate
        user = User(id="partial-network-user")
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = SyncManager(mock_fetcher).sync_report(user, days=2, repo=repo)
            samples = repo.metric_samples(
                user.id,
                "heart_rate",
                datetime(1970, 1, 1, tzinfo=timezone.utc),
                datetime(1970, 1, 2, tzinfo=timezone.utc),
            )

        heart_rate = next(item for item in report.streams if item.stream == "heart_rate")
        assert report.success is False
        assert heart_rate.status == "failed"
        assert heart_rate.records_written == 1
        assert heart_rate.error_kind == "network"
        assert len(samples) == 1

    def test_unavailable_daily_capability_is_saved_as_diagnostic(self, mock_fetcher):
        batch = FetchBatch(expected_chunks=1)
        batch.add_success(mock_fetcher.fetch_daily_statistics_records(
            FetchWindow.days_back(1)
        )[0])
        batch.add_unavailable_capability(
            "daily_summary/readiness_watch_score", "not supported"
        )

        report = SyncManager(mock_fetcher)._persist_records(
            "daily_summary", batch, None, User(id="daily-capability-user")
        )

        assert report.status == "success"
        diagnostic = next(
            item for item in report.diagnostics
            if item.diagnostic_stream == "daily_summary/readiness_watch_score"
        )
        assert diagnostic.status == "unavailable"
        assert diagnostic.error_kind == "not_available"

    def test_all_day_stress_summary_and_timeline_are_persisted(
        self, mock_fetcher, setup_db
    ):
        user = User(id="stress-timeline-user")
        start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="wellness",
            source_key="wellness:all_day_stress:user:2026-09-03:2026-09-03",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{
                "timestamp": int(start.timestamp() * 1000),
                "deviceId": "A1B2C3D4E5F60708",
                "avgStress": 29,
                "minStress": 5,
                "maxStress": 65,
                "relaxProportion": 63,
                "normalProportion": 36,
                "mediumProportion": 1,
                "highProportion": 0,
                "data": json.dumps([
                    {"time": int(start.timestamp() * 1000) - 300_000, "value": 99},
                    {"time": int(start.timestamp() * 1000), "value": 5},
                    {"time": int(start.timestamp() * 1000) + 300_000, "value": 29},
                    {"time": int(start.timestamp() * 1000) + 600_000, "value": 65},
                    {"time": int((start + timedelta(days=1)).timestamp() * 1000), "value": 98},
                ]),
            }, {
                "timestamp": int((start - timedelta(days=1)).timestamp() * 1000),
                "avgStress": 11,
            }, {
                "timestamp": int((start + timedelta(days=1)).timestamp() * 1000),
                "avgStress": 88,
            }]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = SyncManager(mock_fetcher)._persist_records(
                "wellness", [record], repo, user
            )
            daily = repo.daily_metrics(
                user.id, date(2026, 9, 3), date(2026, 9, 3)
            )
            samples = repo.metric_samples(
                user.id,
                "stress",
                start,
                start + timedelta(days=1),
            )

        assert report.status == "success"
        assert report.raw_records == 1
        assert report.records_written == len(daily) + len(samples)
        assert {row.metric for row in daily} == {
            "stress", "stress_min", "stress_max", "stress_relaxed_pct",
            "stress_normal_pct", "stress_medium_pct", "stress_high_pct",
        }
        assert [row.value for row in samples] == [5, 29, 65]
        assert {row.device_id for row in samples} == {"A1B2C3D4E5F60708"}

    @pytest.mark.parametrize("kind", ["service", "network", "auth"])
    def test_optional_stream_failure_blocks_complete_sync(
        self, mock_fetcher, setup_db, kind
    ):
        def fail_wellness(_window):
            raise ZeppAuthError("optional stream failed", kind=kind)

        mock_fetcher.fetch_wellness_records = fail_wellness
        user = User(id=f"optional-{kind}-user")
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = SyncManager(mock_fetcher).sync_report(user, days=1, repo=repo)

        assert report.success is False
        assert report.error_kind == kind
        assert report.needs_reauth is (kind == "auth")
        wellness = next(item for item in report.streams if item.stream == "wellness")
        assert wellness.status == "failed"

    def test_workout_detail_is_decoded_and_persisted(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="workout-detail-user")
        start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            repo.save_workout(Workout(
                user_id=user.id,
                workout_id="detail-run",
                started_at=start,
                ended_at=start + timedelta(seconds=3),
                duration=1,
                type=WorkoutType.RUNNING,
                vendor_source="opaque-source",
            ))
            written = manager._write_stream(FetchedRecord(raw=RawRecord(
                stream="workout_detail",
                source_key="workout_detail:detail-run:opaque-source",
                start_utc=start,
                end_utc=start + timedelta(seconds=3),
                payload={
                    "data": {
                        "trackid": int(start.timestamp()),
                        "time": "1;1;1;",
                        "heart_rate": "1,80;1,2;1,-1;",
                    }
                },
            )), repo, user)

            workout = repo.workout(user.id, "detail-run")
            samples = repo.workout_metric_samples(user.id, "detail-run")

        assert written == 1
        assert workout is not None and workout.detail_synced is True
        assert workout.detail is not None
        assert workout.detail["fetched_at"].endswith("Z")
        assert {
            key: value for key, value in workout.detail.items() if key != "fetched_at"
        } == {
            "schema_version": "4.0",
            "workout_id": str(int(start.timestamp())),
            "metrics_present": ["heart_rate"],
            "metric_sample_counts": {"heart_rate": 4},
            "laps": [],
            "pauses": [],
            "strength_sets": [],
        }
        assert [sample.metric for sample in samples] == ["heart_rate"] * 4
        assert [sample.value for sample in samples] == [80, 80, 82, 81]

    def test_pending_workout_detail_batch_is_capped_at_four(self, mock_fetcher):
        requested_limits = []
        fetched_ids = []

        class Repository:
            @staticmethod
            def pending_workout_details(_user_id, _start, _end, limit):
                requested_limits.append(limit)
                return [
                    SimpleNamespace(
                        workout_id=f"run-{index}",
                        vendor_source="device-source",
                        started_at=datetime.now(timezone.utc),
                    )
                    for index in range(limit)
                ]

        def fetch_detail(workout_id, source, start, end):
            fetched_ids.append(workout_id)
            return FetchedRecord(raw=RawRecord(
                stream="workout_detail",
                source_key=f"workout_detail:{workout_id}:{source}",
                start_utc=start,
                end_utc=end,
                payload={"data": {"trackid": int(start.timestamp())}},
            ))

        mock_fetcher.fetch_workout_detail = fetch_detail
        records = SyncManager(mock_fetcher)._fetch_pending_workout_details(
            FetchWindow.days_back(7), Repository(), User(id="batch-user")
        )

        assert requested_limits == [4]
        assert fetched_ids == ["run-0", "run-1", "run-2", "run-3"]
        assert len(records) == 4

    def test_workout_detail_timeout_reports_incomplete_sync(
        self, mock_fetcher, setup_db
    ):
        mock_fetcher.fetch_workout_detail = lambda *_args: (_ for _ in ()).throw(
            ZeppAuthError(
                "同步超时，已停止后续请求",
                kind="timeout",
            )
        )
        user = User(id="detail-timeout-user")
        started_at = datetime.now(timezone.utc) - timedelta(days=1)
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            repo.save_workout(Workout(
                user_id=user.id,
                workout_id="slow-detail",
                started_at=started_at,
                ended_at=started_at + timedelta(minutes=30),
                duration=30,
                type=WorkoutType.RUNNING,
                vendor_source="device-source",
            ))
            report = SyncManager(mock_fetcher).sync_report(user, days=7, repo=repo)
            states = {row.stream: row for row in repo.sync_stream_states(user.id)}

        assert report.success is False
        assert "时间预算" in report.message
        assert mock_fetcher.calls == [
            "heart_rate", "daily_summary", "workouts", "sleep", "hrv",
            "wellness", "dense_files",
        ]
        detail = next(item for item in report.streams if item.stream == "workout_detail")
        assert detail.status == "failed"
        assert detail.error_kind == "timeout"
        assert states["workout_detail"].fetch_status == "failed"
        assert states["workout_detail"].error_kind == "timeout"
        assert states["heart_rate"].write_status == "success"

    def test_workout_training_record_uses_shanghai_start_day(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="workout-local-day-user")
        start = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            manager._write_stream(FetchedRecord(raw=RawRecord(
                stream="workouts",
                source_key="sport_history:run:test",
                start_utc=start,
                end_utc=start + timedelta(minutes=30),
                payload={"data": {"summary": [{
                    "trackid": int(start.timestamp()),
                    "end_time": int((start + timedelta(minutes=30)).timestamp()),
                    "type": 1,
                    "exercise_load": 40,
                }]}},
            )), repo, user)
            rows = repo.training_range(user.id, date(2026, 8, 28), date(2026, 8, 28))

        assert len(rows) == 1
        assert rows[0]["workout_count"] == 1
        assert rows[0]["total_duration"] == 30

    def test_dense_file_is_decoded_once_and_persisted_idempotently(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="dense-sync-user")
        start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
        archive_calls = []

        def fetch_archive(file_type, file_id):
            archive_calls.append((file_type, file_id))
            return _dense_archive(start, [72, 255, 74])

        mock_fetcher.fetch_dense_file_archive = fetch_archive
        record = FetchedRecord(raw=RawRecord(
            stream="dense_files",
            source_key="file_info:second_heart_rate:2026-08-26:2026-08-27",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{"value": {
                "startTime": int(start.timestamp() * 1000),
                "deviceId": "1,A1B2C3D4E5F60708",
                "samples": [{
                    "s": 0,
                    "e": 2_000,
                    "fileId": "opaque-index-only",
                    "fileType": "SEC_HR",
                    "dateString": "2026-08-26",
                }],
            }}]},
        ))
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            written = manager._write_stream(record, repo, user)
            second_written = manager._write_stream(record, repo, user)
            files = repo.dense_data_files(
                user.id, "second_heart_rate", start.date(), start.date()
            )
            samples = repo.metric_samples(
                user.id, "heart_rate", start, start + timedelta(days=1)
            )

        assert written == 3
        assert second_written == 0
        assert archive_calls == [("SEC_HR", "opaque-index-only")]
        assert len(files) == 1
        assert files[0].parse_status == "decoded"
        assert files[0].sample_count == 2
        assert [sample.value for sample in samples] == [72, 74]

    def test_dense_archive_download_failure_is_a_fetch_failure(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher, dense_archive_budget=1)
        user = User(id="dense-fetch-failure-user")
        start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
        mock_fetcher.fetch_dense_file_archive = lambda *_args: (_ for _ in ()).throw(
            ZeppAuthError("archive offline", kind="network")
        )
        record = FetchedRecord(raw=RawRecord(
            stream="dense_files",
            source_key="file_info:second_heart_rate:2026-08-26:2026-08-27",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{"value": {
                "startTime": int(start.timestamp() * 1000),
                "deviceId": "1,A1B2C3D4E5F60708",
                "samples": [{
                    "s": 0,
                    "e": 2_000,
                    "fileId": "offline-archive",
                    "fileType": "SEC_HR",
                    "dateString": "2026-08-26",
                }],
            }}]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)

        assert report.status == "failed"
        assert report.diagnostic_stream == "heart_rate/dense_archive"
        assert report.fetch_status == "failed"
        assert report.parse_status == "not_run"
        assert report.error_kind == "network"

    def test_unknown_dense_payload_is_unverified(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher, dense_archive_budget=0)
        user = User(id="dense-unknown-user")
        now = datetime.now(timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="dense_files",
            source_key="file_info:second_heart_rate:unknown",
            start_utc=now,
            end_utc=now,
            payload={"items": [{"unexpected": "shape"}]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)

        assert report.status == "unverified"
        assert report.parse_status == "unrecognized"
        assert report.error_kind == "unrecognized_payload"

    def test_standard_sync_indexes_dense_file_without_downloading_archive(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher, dense_archive_budget=0)
        user = User(id="dense-index-only-user")
        start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
        mock_fetcher.fetch_dense_file_archive = lambda *_args: pytest.fail(
            "standard sync must not download a dense archive"
        )
        record = FetchedRecord(raw=RawRecord(
            stream="dense_files",
            source_key="file_info:second_heart_rate:2026-08-26:2026-08-27",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{"value": {
                "startTime": int(start.timestamp() * 1000),
                "deviceId": "1,A1B2C3D4E5F60708",
                "samples": [{
                    "s": 0,
                    "e": 2_000,
                    "fileId": "index-without-download",
                    "fileType": "SEC_HR",
                    "dateString": "2026-08-26",
                }],
            }}]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            written = manager._write_stream(record, repo, user)
            files = repo.dense_data_files(
                user.id, "second_heart_rate", start.date(), start.date()
            )

        assert written == 1
        assert len(files) == 1
        assert files[0].parse_status == "indexed"
        assert files[0].sample_count == 0

    def test_sync_report_serializes_same_user_across_managers(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        class BlockingFetcher(MockDataFetcher):
            def fetch_heart_rate_records(self, window):
                first_entered.set()
                assert release_first.wait(timeout=2)
                return super().fetch_heart_rate_records(window)

        class ObservedFetcher(MockDataFetcher):
            def fetch_heart_rate_records(self, window):
                second_entered.set()
                return super().fetch_heart_rate_records(window)

        first = threading.Thread(
            target=SyncManager(BlockingFetcher()).sync_report,
            args=(User(id="serialized-user"), 1),
        )
        second = threading.Thread(
            target=SyncManager(ObservedFetcher()).sync_report,
            args=(User(id="serialized-user"), 1),
        )
        first.start()
        assert first_entered.wait(timeout=2)
        second.start()
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert second_entered.is_set()

    def test_database_error_aborts_stream_instead_of_poisoning_session(self):
        class LockedRepository:
            def save_metric_samples(self, _samples):
                raise OperationalError("insert", {}, RuntimeError("database is locked"))

        record = FetchedRecord(raw=RawRecord(
            stream="heart_rate",
            source_key="hr:locked",
            start_utc=datetime.now(timezone.utc),
            end_utc=datetime.now(timezone.utc),
            payload={"items": [{"timestamp": 1, "value": 72}]},
        ))

        with pytest.raises(OperationalError, match="database is locked"):
            SyncManager(MockDataFetcher())._persist_record(
                record, LockedRepository(), User(id="locked-user")
            )

    def test_workout_pages_rebuild_one_canonical_local_day(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="canonical-workout-day-user")
        first_start = datetime(2026, 8, 27, 16, 30, tzinfo=timezone.utc)
        second_start = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
        records = [
            FetchedRecord(raw=RawRecord(
                stream="workouts",
                source_key="sport_history:run:first",
                start_utc=first_start,
                end_utc=second_start,
                payload={"data": {"summary": [{
                    "trackid": int(first_start.timestamp()),
                    "end_time": int((first_start + timedelta(minutes=30)).timestamp()),
                    "type": 1,
                    "exercise_load": 40,
                }]}},
            )),
            FetchedRecord(raw=RawRecord(
                stream="workouts",
                source_key="sport_history:strength:second",
                start_utc=first_start,
                end_utc=second_start,
                payload={"data": {"summary": [{
                    "trackid": int(second_start.timestamp()),
                    "end_time": int((second_start + timedelta(minutes=45)).timestamp()),
                    "type": 204,
                    "exercise_load": 30,
                }]}},
            )),
        ]

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            first = manager._persist_records("workouts", records, repo, user)
            second = manager._persist_records("workouts", records, repo, user)
            workouts = repo.workouts(
                user.id, date(2026, 8, 28), date(2026, 8, 28)
            )
            training = repo.training_range(
                user.id, date(2026, 8, 28), date(2026, 8, 28)
            )

        assert first.status == "success"
        assert second.status == "success"
        assert len(workouts) == 2
        assert training == [{
            "user_id": user.id,
            "source": "canonical_workouts",
            "date": "2026-08-28",
            "workout_count": 2,
            "total_duration": 75,
            "total_load": 70,
            "training_status": "moderate",
        }]

    def test_workout_time_correction_rebuilds_old_and_new_days(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher)
        user = User(id="corrected-workout-day-user")
        old_start = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)
        new_start = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)

        def record(start, load):
            return FetchedRecord(raw=RawRecord(
                stream="workouts",
                source_key="sport_history:run:corrected",
                start_utc=start,
                end_utc=start + timedelta(minutes=20),
                payload={"data": {"summary": [{
                    "trackid": "stable-workout-id",
                    "start_time": int(start.timestamp()),
                    "end_time": int((start + timedelta(minutes=20)).timestamp()),
                    "type": 1,
                    "exercise_load": load,
                }]}},
            ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            manager._persist_record(record(old_start, 10), repo, user)
            manager._persist_record(record(new_start, 20), repo, user)
            old_rows = repo.training_range(
                user.id, date(2026, 8, 27), date(2026, 8, 27)
            )
            new_rows = repo.training_range(
                user.id, date(2026, 8, 28), date(2026, 8, 28)
            )

        assert old_rows == []
        assert new_rows[0]["workout_count"] == 1
        assert new_rows[0]["total_load"] == 20

    def test_empty_record_batch_has_consistent_successful_empty_stages(self):
        report = SyncManager(MockDataFetcher())._persist_records(
            "workouts", [], None, User(id="empty-workouts-user")
        )

        assert report.status == "success"
        assert report.fetch_status == "success"
        assert report.parse_status == "empty"
        assert report.write_status == "not_run"
        assert report.error_kind is None

    def test_all_day_stress_padding_only_is_successful_empty(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="stress-padding-only-user")
        start = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
        end = datetime(2026, 9, 5, 16, tzinfo=timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="wellness",
            source_key="wellness:all_day_stress:user:2026-09-05:2026-09-05",
            start_utc=start,
            end_utc=end,
            payload={"items": [
                {"timestamp": int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp() * 1000), "avgStress": 11},
                {"timestamp": int(datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp() * 1000), "avgStress": 88},
            ]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)
            daily = repo.daily_metrics(user.id, date(2026, 9, 5), date(2026, 9, 5))

        assert report.status == "success"
        assert report.parse_status == "empty"
        assert report.write_status == "not_run"
        assert report.error_kind is None
        assert daily == []

    def test_all_day_stress_nonempty_malformed_payload_remains_unrecognized(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher)
        user = User(id="stress-malformed-user")
        start = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="wellness",
            source_key="wellness:all_day_stress:user:2026-09-05:2026-09-05",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{
                "timestamp": int(start.timestamp() * 1000),
                "data": "not-json",
            }]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)

        assert report.status == "unverified"
        assert report.parse_status == "unrecognized"
        assert report.write_status == "not_run"
        assert report.error_kind == "unrecognized_payload"

    def test_all_day_stress_keeps_logical_window_start_sample(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher)
        user = User(id="stress-midnight-boundary-user")
        start = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="wellness",
            source_key="wellness:all_day_stress:user:2026-09-05:2026-09-05",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [{
                "timestamp": int(start.timestamp() * 1000),
                "data": [{"time": int(start.timestamp() * 1000), "value": 42}],
            }]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)
            samples = repo.metric_samples(
                user.id, "stress", start, start + timedelta(days=1)
            )

        assert report.status == "success"
        assert report.parse_status == "success"
        assert report.write_status == "success"
        assert [
            (sample.timestamp.replace(tzinfo=timezone.utc), sample.value)
            for sample in samples
        ] == [(start, 42)]

    def test_all_day_stress_mixed_padding_and_malformed_item_is_unrecognized(
        self, mock_fetcher, setup_db
    ):
        manager = SyncManager(mock_fetcher)
        user = User(id="stress-mixed-malformed-user")
        start = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
        record = FetchedRecord(raw=RawRecord(
            stream="wellness",
            source_key="wellness:all_day_stress:user:2026-09-05:2026-09-05",
            start_utc=start,
            end_utc=start + timedelta(days=1),
            payload={"items": [
                {
                    "timestamp": int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp() * 1000),
                    "avgStress": 11,
                },
                {
                    "timestamp": int(start.timestamp() * 1000),
                    "avgStress": None,
                    "data": "not-json",
                },
            ]},
        ))

        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user.id)
            report = manager._persist_record(record, repo, user)

        assert report.status == "unverified"
        assert report.parse_status == "unrecognized"
        assert report.write_status == "not_run"
        assert report.error_kind == "unrecognized_payload"
