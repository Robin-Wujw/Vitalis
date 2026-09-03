"""测试 Zepp 数据获取器（fetcher）的工具函数与 FetchWindow。"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from vitalis.connectors.zepp import ZeppConnector, client as client_module
from vitalis.connectors.zepp.client import ZeppAPIClient, ZeppAuthError
from vitalis.connectors.zepp.fetcher import (
    DataFetcher,
    FetchWindow,
    PartialFetchError,
    _heart_rate_cursor,
    _heart_rate_items,
    _payload_items,
)
from vitalis.connectors.zepp.sync_manager import SyncReport
from vitalis.models import User


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

    def test_generated_time_nested_in_value(self):
        assert _heart_rate_cursor([
            {"value": {"generatedTime": 1_700_000_000_000, "bpm": 72}}
        ]) == 1_700_000_001_000

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
    assert not [
        call for call in connector.events
        if call[:2] in {("Charge", "stress_data"), ("Charge", "insight_data")}
    ]
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


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, "auth"),
        (404, "not_available"),
        (503, "service"),
        (418, "vendor_response"),
    ],
)
def test_client_classifies_http_failures(monkeypatch, status_code, expected_kind):
    class Response:
        text = "upstream failure"

        def __init__(self, code):
            self.status_code = code

    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    monkeypatch.setattr(
        client._client,
        "get",
        lambda *args, **kwargs: Response(status_code),
    )
    monkeypatch.setattr(client_module.time_mod, "sleep", lambda _seconds: None)

    with pytest.raises(ZeppAuthError) as raised:
        client.fetch_devices()

    assert raised.value.kind == expected_kind
    assert raised.value.needs_reauth is (expected_kind == "auth")


def test_client_classifies_transport_failure(monkeypatch):
    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    monkeypatch.setattr(
        client._client,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("offline")
        ),
    )
    monkeypatch.setattr(client_module.time_mod, "sleep", lambda _seconds: None)

    with pytest.raises(ZeppAuthError) as raised:
        client.fetch_devices()

    assert raised.value.kind == "network"


def test_client_classifies_timeout_separately(monkeypatch):
    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    monkeypatch.setattr(
        client._client,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.ReadTimeout("slow")
        ),
    )
    monkeypatch.setattr(client_module.time_mod, "sleep", lambda _seconds: None)

    with pytest.raises(ZeppAuthError) as raised:
        client.fetch_devices()

    assert raised.value.kind == "timeout"


def test_client_constructor_enforces_region_host_allowlist():
    with pytest.raises(ZeppAuthError) as raised:
        ZeppAPIClient("token", "user-123", "https://127.0.0.1")

    assert raised.value.kind == "invalid_request"


@pytest.mark.parametrize(
    "host",
    [
        "http://api-mifitcn.zepp.com",
        "https://api-mifitcn.zepp.com:443",
        "https://api-mifitcn.zepp.com/path",
        "https://api-mifitcn.zepp.com?region=cn",
        "https://api-mifitcn.zepp.com#fragment",
        "https://user:pass@api-mifitcn.zepp.com",
    ],
)
def test_region_host_rejects_non_origin_components(host):
    with pytest.raises(ZeppAuthError) as raised:
        ZeppAPIClient("token", "user-123", host)

    assert raised.value.kind == "invalid_request"


def test_region_host_accepts_and_normalizes_https_root_origin():
    client = ZeppAPIClient(
        "token", "user-123", "https://API-MIFITCN.ZEPP.COM/"
    )

    assert client.region_host == "api-mifitcn.zepp.com"


def test_fetch_hrv_uses_configured_local_day_bounds(monkeypatch):
    client = ZeppAPIClient("token", "user-123", "api-mifitcn.zepp.com")
    captured = {}

    def fetch_events(event_type, sub_type, from_ms, to_ms, limit, reverse):
        captured.update(
            event_type=event_type,
            sub_type=sub_type,
            from_ms=from_ms,
            to_ms=to_ms,
        )
        return {"items": []}

    monkeypatch.setattr(client, "fetch_events", fetch_events)
    client.fetch_hrv("2026-08-28", "2026-08-29")

    assert captured["from_ms"] == int(
        datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert captured["to_ms"] == int(
        datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc).timestamp() * 1000
    ) - 1


class OptionalFailureConnector:
    def __init__(self, kind):
        self.kind = kind

    def _raise(self):
        raise ZeppAuthError(f"optional {self.kind}", kind=self.kind)

    def fetch_user_events(self, *args, **kwargs):
        self._raise()

    def fetch_events(self, *args, **kwargs):
        self._raise()

    def fetch_user_events_date_string(self, *args, **kwargs):
        self._raise()

    def fetch_file_info_events(self, *args, **kwargs):
        self._raise()


def test_optional_fetchers_only_swallow_not_available():
    window = FetchWindow.days_back(1)
    unavailable = DataFetcher(OptionalFailureConnector("not_available"))
    assert unavailable.fetch_wellness_records(window) == []
    assert unavailable.fetch_dense_file_records(window) == []

    for kind in ("auth", "network", "service"):
        fetcher = DataFetcher(OptionalFailureConnector(kind))
        with pytest.raises(ZeppAuthError) as wellness:
            fetcher.fetch_wellness_records(window)
        assert wellness.value.kind == kind
        with pytest.raises(ZeppAuthError) as dense:
            fetcher.fetch_dense_file_records(window)
        assert dense.value.kind == kind


def test_all_workout_endpoints_unavailable_is_not_a_successful_empty_batch():
    class Connector:
        def fetch_sport_history(self, *args, **kwargs):
            raise ZeppAuthError("missing", kind="not_available")

    with pytest.raises(ZeppAuthError) as raised:
        DataFetcher(Connector()).fetch_workout_records(FetchWindow.days_back(1))

    assert raised.value.kind == "not_available"


def test_successful_empty_workout_endpoints_keep_fetch_evidence():
    class Connector:
        def fetch_sport_history(self, *args, **kwargs):
            return {"data": {"summary": [], "next": -1}}

    records = DataFetcher(Connector()).fetch_workout_records(FetchWindow.days_back(1))

    assert records
    assert all(record.raw.payload["data"]["summary"] == [] for record in records)


def test_local_date_window_uses_configured_timezone_bounds():
    window = FetchWindow.local_dates(date(2026, 8, 28), date(2026, 8, 29))

    assert window.start == datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    assert window.end == datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)
    assert window.start_day() == "2026-08-28"
    assert window.end_day() == "2026-08-29"


def test_import_token_rejects_rebinding_local_user_to_other_vendor_account():
    class Repository:
        @staticmethod
        def get_token(user_id, source):
            assert (user_id, source) == ("local-user", "zepp")
            return SimpleNamespace(source_user_id="vendor-a")

    with pytest.raises(ZeppAuthError) as raised:
        ZeppConnector(mock=False).import_token(
            Repository(),
            "local-user",
            "new-token",
            vendor_user_id="vendor-b",
            region_host="api-mifitcn.zepp.com",
        )

    assert raised.value.kind == "invalid_request"
    assert "其他 Zepp 账号" in str(raised.value)


def test_real_connector_fetch_synchronizes_requested_local_dates(monkeypatch):
    connector = ZeppConnector(mock=False)
    captured = {}

    def sync_with_report(*_args, **kwargs):
        captured["window"] = kwargs["window"]
        return SyncReport(success=True)

    monkeypatch.setattr(connector, "sync_with_report", sync_with_report)
    monkeypatch.setattr(connector, "_rebuild_dailies", lambda *_args: [])

    connector.fetch(
        User(id="window-user"),
        date(2026, 8, 28),
        date(2026, 8, 29),
        repo=object(),
    )

    assert captured["window"].start == datetime(
        2026, 8, 27, 16, 0, tzinfo=timezone.utc
    )
    assert captured["window"].end == datetime(
        2026, 8, 29, 16, 0, tzinfo=timezone.utc
    )


def test_mixed_chunk_availability_is_preserved_as_partial_coverage():
    class Connector:
        calls = 0

        def fetch_heart_rate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"items": [{"timestamp": 1_700_000_000, "value": 72}]}
            raise ZeppAuthError("missing chunk", kind="not_available")

    end = datetime(2026, 8, 28, tzinfo=timezone.utc)
    records = DataFetcher(Connector()).fetch_heart_rate_records(
        FetchWindow(start=end - timedelta(days=15), end=end)
    )

    assert len(records) == 1
    assert records.expected_chunks == 3
    assert records.successful_chunks == 1
    assert records.unavailable_chunks == 2
    assert records.partial is True


def test_successful_empty_heart_rate_chunk_is_not_refetched():
    class Connector:
        calls = 0

        def fetch_heart_rate(self, *args, **kwargs):
            self.calls += 1
            return {"items": []}

    connector = Connector()
    records = DataFetcher(connector).fetch_heart_rate_records(
        FetchWindow.days_back(1)
    )

    assert connector.calls == 1
    assert records[0].raw.payload == {"items": []}


def test_terminal_chunk_failure_carries_completed_records():
    class Connector:
        calls = 0

        def fetch_heart_rate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"items": [{"generatedTime": 1_700_000_000_000, "value": 72}]}
            raise ZeppAuthError("offline", kind="network")

    end = datetime(2026, 8, 28, tzinfo=timezone.utc)
    with pytest.raises(PartialFetchError) as raised:
        DataFetcher(Connector()).fetch_heart_rate_records(
            FetchWindow(start=end - timedelta(days=8), end=end)
        )

    assert raised.value.kind == "network"
    assert len(raised.value.records) == 1


def test_mixed_workout_endpoint_availability_is_partial():
    class Connector:
        def fetch_sport_history(self, sport, *args, **kwargs):
            if sport == "run":
                return {"data": {"summary": [], "next": -1}}
            raise ZeppAuthError("missing sport", kind="not_available")

    records = DataFetcher(Connector()).fetch_workout_records(
        FetchWindow.days_back(1)
    )

    assert records.partial is True
    assert records.successful_chunks == 1
    assert records.unavailable_chunks > 0


def test_wholly_unavailable_wellness_capabilities_do_not_create_partial():
    class Connector:
        @staticmethod
        def fetch_user_events(event_type, *args, **kwargs):
            if event_type == "all_day_stress":
                return {"items": []}
            raise ZeppAuthError("capability unavailable", kind="not_available")

        @staticmethod
        def fetch_events(*args, **kwargs):
            raise ZeppAuthError("capability unavailable", kind="not_available")

        @staticmethod
        def fetch_user_events_date_string(*args, **kwargs):
            raise ZeppAuthError("capability unavailable", kind="not_available")

    records = DataFetcher(Connector()).fetch_wellness_records(
        FetchWindow.days_back(1)
    )

    assert len(records) == 1
    assert records.partial is False
