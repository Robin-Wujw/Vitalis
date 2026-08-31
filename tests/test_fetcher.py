"""测试 Zepp 数据获取器（fetcher）的工具函数与 FetchWindow。"""

from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.connectors.zepp.client import ZeppAPIClient
from vitalis.connectors.zepp.fetcher import (
    DataFetcher,
    FetchWindow,
    _heart_rate_cursor,
    _heart_rate_items,
    _payload_items,
)


def test_watch_statistics_uses_requested_statistic_in_path(monkeypatch):
    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    calls = []
    monkeypatch.setattr(
        client,
        "_get",
        lambda path, params: calls.append((path, params)) or {"items": []},
    )

    client.fetch_watch_statistics("SPORT_LOAD", "2026-08-01", "2026-08-30")
    client.fetch_watch_statistics("VO2_MAX", "2026-08-01", "2026-08-30")

    assert [call[0] for call in calls] == [
        "/v2/watch/users/user-123/WatchSportStatistics/SPORT_LOAD",
        "/v2/watch/users/user-123/WatchSportStatistics/VO2_MAX",
    ]


def test_watch_statistics_rejects_unknown_statistic():
    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    with pytest.raises(ValueError, match="unsupported watch statistic"):
        client.fetch_watch_statistics("UNKNOWN")


class TestFetchWindow:
    def test_days_back_within_bounds(self):
        w = FetchWindow.days_back(30)
        assert w.end > w.start
        assert (w.end - w.start).days == 30

    def test_days_back_rejects_out_of_bounds(self):
        with pytest.raises(Exception):
            FetchWindow.days_back(0)
        with pytest.raises(Exception):
            FetchWindow.days_back(731)

    def test_chunks(self):
        w = FetchWindow.days_back(30)
        chunks = w.chunks(7)
        assert len(chunks) == 5
        assert chunks[0].start == w.start
        assert chunks[-1].end == w.end

    def test_start_end_day(self):
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
        w = FetchWindow(start=start, end=end)
        assert w.start_day() == "2024-01-01"
        assert w.end_day() == "2024-01-10"


class TestHeartRateItems:
    def test_direct_array(self):
        payload = [{"timestamp": 1}, {"timestamp": 2}]
        assert len(_heart_rate_items(payload)) == 2

    def test_nested_data_items(self):
        payload = {"data": {"items": [{"timestamp": 3}]}}
        assert len(_heart_rate_items(payload)) == 1

    def test_object_items_key(self):
        payload = {"items": [{"timestamp": 4}]}
        assert len(_heart_rate_items(payload)) == 1

    def test_empty(self):
        assert _heart_rate_items({}) == []


class TestHeartRateCursor:
    def test_advances_past_max(self):
        items = [
            {"timestamp": 1_700_000_000},
            {"time": "1700003600"},
            {"timeStamp": 1_700_007_200_000},
            {"value": 99},  # malformed: skipped
            "not an object",  # malformed: skipped
        ]
        # max is 1700007200000 ms, cursor advances one second
        assert _heart_rate_cursor(items) == 1_700_007_201_000

    def test_empty(self):
        assert _heart_rate_cursor([]) is None


class TestPayloadItems:
    def test_items_direct(self):
        assert len(_payload_items({"items": [1, 2]})) == 2

    def test_data_array(self):
        assert len(_payload_items({"data": [1, 2]})) == 2

    def test_data_object_items(self):
        assert len(_payload_items({"data": {"items": [1, 2]}})) == 2

    def test_real_workout_summary(self):
        assert _payload_items({"data": {"summary": [{"trackid": 1}]}}) == [
            {"trackid": 1}
        ]

    def test_empty(self):
        assert _payload_items({}) == []


def test_daily_and_wellness_requests_are_chunked_for_long_history():
    class RecordingConnector:
        def __init__(self):
            self.events = []
            self.user_events = []
            self.date_events = []

        def fetch_events(self, event_type, sub_type, from_ms, to_ms, limit, reverse):
            self.events.append((event_type, sub_type, from_ms, to_ms))
            return {"items": []}

        def fetch_user_events(self, event_type, sub_type, from_ms, to_ms, limit, reverse):
            self.user_events.append((event_type, sub_type, from_ms, to_ms))
            return {"items": []}

        def fetch_user_events_date_string(self, event_type, sub_type, from_iso, to_iso):
            self.date_events.append((event_type, sub_type, from_iso, to_iso))
            return {"items": []}

        def fetch_watch_statistics(self, *args, **kwargs):
            return {"items": []}

    connector = RecordingConnector()
    fetcher = DataFetcher(connector)
    end = datetime(2026, 8, 28, tzinfo=timezone.utc)
    window = FetchWindow(start=end - timedelta(days=15), end=end)

    fetcher.fetch_daily_statistics_records(window)
    fetcher.fetch_wellness_records(window)

    daily_health_calls = [x for x in connector.events if x[:2] == ("DailyHealth", "summary")]
    readiness_calls = [x for x in connector.events if x[:2] == ("readiness", "watch_score")]
    rmssd_calls = [x for x in connector.events if x[:2] == ("HRVRMSSD", "real_data")]
    assert len(daily_health_calls) == 3
    assert len(readiness_calls) == 3
    assert len(rmssd_calls) == 3
    assert len(connector.date_events) == 6


def test_dense_file_index_requests_are_chunked():
    class RecordingConnector:
        def __init__(self):
            self.calls = []

        def fetch_file_info_events(self, event_type, sub_type, from_ms, to_ms, limit):
            self.calls.append((event_type, sub_type, from_ms, to_ms, limit))
            return {"items": []}

    connector = RecordingConnector()
    end = datetime(2026, 8, 28, tzinfo=timezone.utc)
    records = DataFetcher(connector).fetch_dense_file_records(
        FetchWindow(start=end - timedelta(days=15), end=end)
    )

    assert len(records) == 3
    assert len(connector.calls) == 3
    assert {call[:2] for call in connector.calls} == {
        ("second_heart_rate", "real_data")
    }
