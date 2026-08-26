"""测试 Zepp 同步管理器（SyncManager）。"""

import pytest

from vitalis.connectors.zepp.client import ZeppAuthError
from vitalis.connectors.zepp.fetcher import FetchWindow, FetchedRecord, RawRecord
from vitalis.connectors.zepp.sync_manager import SyncManager
from vitalis.models import User
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
        assert report.records_written >= 0

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
