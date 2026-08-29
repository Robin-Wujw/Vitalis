"""测试 Zepp 同步管理器（SyncManager）。"""

from datetime import date, datetime, timedelta, timezone
import threading

import pytest
from sqlalchemy.exc import OperationalError

from vitalis.connectors.zepp.client import ZeppAuthError
from vitalis.connectors.zepp.fetcher import FetchWindow, FetchedRecord, RawRecord
from vitalis.connectors.zepp.sync_manager import SyncManager
from vitalis.models import User, Workout, WorkoutType
from vitalis.storage import HealthRepository, init_db, session_scope


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
        assert workout.detail == {
            "schema_version": "2.0",
            "workout_id": str(int(start.timestamp())),
            "metrics_present": ["heart_rate"],
            "metric_sample_counts": {"heart_rate": 4},
            "laps": [],
            "pauses": [],
            "strength_sets": [],
        }
        assert [sample.metric for sample in samples] == ["heart_rate"] * 4
        assert [sample.value for sample in samples] == [80, 80, 82, 81]

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

    def test_dense_file_index_is_persisted_without_fake_samples(self, mock_fetcher, setup_db):
        manager = SyncManager(mock_fetcher)
        user = User(id="dense-sync-user")
        start = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
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
                    "e": 60_000,
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
            files = repo.dense_data_files(
                user.id, "second_heart_rate", start.date(), start.date()
            )
            samples = repo.metric_samples(
                user.id, "heart_rate", start, start + timedelta(days=1)
            )

        assert written == 1
        assert len(files) == 1
        assert files[0].parse_status == "indexed"
        assert files[0].sample_count == 0
        assert samples == []

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
