"""测试 Zepp 数据获取器（fetcher）的工具函数与 FetchWindow。"""

from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.connectors.zepp.fetcher import (
    FetchWindow,
    _heart_rate_cursor,
    _heart_rate_items,
    _payload_items,
)


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
