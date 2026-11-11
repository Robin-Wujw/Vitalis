"""Value-level and missing-data contracts for the MCP-equivalent health queries."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from contextlib import contextmanager

from vitalis.application.ports import HealthMetricRead

DAY = date(2026, 1, 3)


def reader():
    source = SimpleNamespace(daily_metrics=lambda *a: (), metric_samples=lambda *a, **k: (),
        activity_records=lambda *a, **k: (), sleep_records=lambda *a, **k: (),
        nap_records=lambda *a, **k: (), source_snapshot=lambda *a: None,
        workout_statistics_workouts=lambda *a, **k: ())
    @contextmanager
    def metric_rows(*args):
        yield iter(source.metric_samples(*args))
    source.metric_sample_rows = metric_rows
    return source


def test_health_summary_isolates_units_devices_and_qualifications():
    from vitalis.application.health_analytics import HealthAnalytics
    source = reader()
    source.metric_samples = lambda user, metric, *a, **k: tuple(
        HealthMetricRead(datetime(2026, 1, 3, hour, tzinfo=timezone.utc), value,
            unit, "zepp", "device", device, None, None, qualification=qualification)
        for hour, value, unit, device, qualification in [
            (1, 12, "ms", "a", "recorded"), (2, 18, "ms", "a", "recorded"),
            (3, 100, "ms", "b", "recorded"), (4, 9, "unknown", "a", "unverified")]
    ) if metric == "hrv_rmssd" else ()
    result = HealthAnalytics(source, timezone_name="UTC").metrics("u", "hrv", DAY, DAY, include_samples=True)
    groups = result["signals"]["hrv_rmssd"]["intraday"]
    assert len(groups) == 3
    valid = next(g for g in groups if g["device_id"] == "a" and g["unit"] == "ms")
    assert valid["summary"]["mean"] == 15
    assert valid["summary"]["sample_count"] == 2
    assert valid["coverage"]["days_with_data"] == 1
    assert valid["coverage"]["within_day_coverage"] is None
    assert result["state"] == "ready"


def test_no_data_does_not_create_zero_health_facts():
    from vitalis.application.health_analytics import HealthAnalytics
    service = HealthAnalytics(reader(), timezone_name="UTC")
    for category in service.CATEGORIES:
        result = service.metrics("u", category, DAY, DAY)
        assert result["state"] == "no_data"
        assert all(not signal["daily"] and not signal["intraday"] for signal in result["signals"].values())
    assert service.body_composition("u", DAY, DAY)["records"] == []
    assert service.profile("u")["state"] == "no_data"


def test_empty_saved_vendor_snapshot_is_not_ready():
    from vitalis.application.health_analytics import HealthAnalytics
    source = reader()
    source.source_snapshot = lambda _, kind: dict(data={} if kind == "profile" else {"devices": []},
        fetched_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    result = HealthAnalytics(source).profile("u")
    assert result["state"] == result["devices"]["state"] == "no_data"
    assert result["devices"]["items"] == [] and result["devices"]["fetched_at"]


def test_sleep_enrichment_keeps_daily_readiness_and_odi_separate_from_sleep_window():
    from vitalis.application.health_analytics import HealthAnalytics
    from vitalis.application.ports import HealthDailyMetricRead
    source = reader()
    source.sleep_records = lambda *a, **k: ({"date": DAY, "data": {
        "sleep_duration": 420, "source": "zepp", "source_scope": "device", "device_id": "watch-one",
        "sleep_started_at": "2026-01-02T23:00:00+00:00", "sleep_ended_at": "2026-01-03T06:00:00+00:00"}},)
    source.daily_metrics = lambda *a: tuple(HealthDailyMetricRead(
        date=day, metric=metric, value=value, unit=unit, source="zepp", source_scope="device", device_id=device,
        source_field=field, qualification="vendor_reported", observed_at=datetime(2026, 1, 3, 8, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 1, 3, 9, tzinfo=timezone.utc))
        for day, metric, value, unit, field, device in [
            (DAY, "readiness", 70, "score", "rdnsScore", "watch-one"),
            (DAY, "readiness", 80, "score", "rdnsScore", "watch-two"),
            (DAY, "spo2_odi", 3, "events/hour", "odi", "watch-one"),
            (date(2026, 1, 2), "readiness", 90, "score", "rdnsScore", "watch-one")])
    result = HealthAnalytics(source, timezone_name="UTC").sleep("u", DAY, DAY)
    night = result["nights"][0]
    daily = night["same_day_observations"]
    assert daily["date"] == DAY.isoformat()
    assert daily["qualification"] == "same_report_date_not_sleep_window"
    groups = daily["readiness"]["readiness"]
    assert {g["device_id"]: g["summary"]["mean"] for g in groups} == {"watch-one": 70, "watch-two": 80}
    assert daily["overnight_spo2"]["spo2_odi"][0]["samples"][0]["value"] == 3
    assert "spo2_odi" not in night["sleep_window_observations"]
    assert groups[0]["coverage"]["days_requested"] == 1


def test_body_records_preserve_timestamp_and_qualification():
    from vitalis.application.health_analytics import HealthAnalytics
    source = reader()
    stamp = datetime(2026, 1, 3, tzinfo=timezone.utc)
    source.metric_samples = lambda user, metric, *a, **k: (
        HealthMetricRead(stamp, 30, "unknown", "zepp", "user_summary", None, None, None,
            source_field="summary.muscleRate", qualification="community_schema"),
    ) if metric == "muscle_rate" else ()
    result = HealthAnalytics(source, timezone_name="UTC").body_composition("u", DAY, DAY)
    assert len(result["records"]) == 1
    assert result["records"][0]["measurements"]["muscle_rate"]["value"] == 30
    assert result["records"][0]["measurements"]["muscle_rate"]["unit"] == "unknown"


def test_api_all_health_capabilities_exist_for_empty_account(client, issue_token):
    headers = {"Authorization": "Bearer " + issue_token("health-parity-empty", scopes=["read"])}
    for path in ("/health/daily-summary", "/health/heart-rate", "/health/sleep",
                 "/health/body-composition", "/health/training-thresholds", "/health/period-summary",
                 "/health/summary/stress", "/sources/zepp/profile", "/sources/zepp/schema"):
        response = client.get(path.replace("/health", "/api/health").replace("/sources", "/api/sources") +
            "?from=2026-01-03&to=2026-01-03", headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert "state" in response.json() or "tools" in response.json()


def test_full_synthetic_sync_to_health_api_preserves_values_and_minutes(client, issue_token):
    import base64
    import json
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord, DataFetcher
    from vitalis.adapters.zepp.sync_manager import SyncManager
    from vitalis.adapters.zepp.client import MockZeppClient
    from vitalis.domain import User

    owner = "health-parity-full"
    headers = {"Authorization": "Bearer " + issue_token(owner, scopes=["read"])}
    stamp = datetime(2026, 1, 3, tzinfo=timezone.utc)
    connector = MockZeppClient()
    connector.source_mode = "mock"
    manager = SyncManager(DataFetcher(connector, "UTC"))
    user = User(id=owner)
    summary = {"tz": 0, "goal": 1000, "stp": {"ttl": 1440, "cal": 100, "dis": 900,
        "stage": [{"start": 0, "stop": 59, "step": 60, "dis": 40, "mode": 3}]},
        "slp": {"st": stamp.timestamp(), "ed": stamp.timestamp() + 3600,
            "dp": 20, "lt": 30, "dt": 10, "wk": 0, "rhr": 50, "ss": 80,
            "stage": [{"start": 0, "stop": 19, "mode": 5}, {"start": 20, "stop": 49, "mode": 4},
                      {"start": 50, "stop": 59, "mode": 8}]}}
    payloads = [
        ("sleep", "band:synthetic", {"data": [{"date_time": "2026-01-03",
            "summary": base64.b64encode(json.dumps(summary).encode()).decode(),
            "data": base64.b64encode(bytes([0, 0, 1]) * 1440).decode(),
            "data_hr": base64.b64encode(bytes([0, 60, 70, 255]) + bytes(1436)).decode()}]}),
        ("wellness", "wellness:weight:weight:synthetic", {"items": [{"memberId": -1,
            "generatedTime": stamp.timestamp(), "summary": {"weight": 72, "height": 180,
                "bmi": 22.22, "fatRate": 21, "muscleRate": 30}}]}),
        ("wellness", "wellness:single_stress:user:synthetic", {"items": [{"timestamp": stamp.timestamp() * 1000, "value": 61}]}),
        ("wellness", "wellness:respiratory_rate:v2:synthetic", {"items": [{"timestamp": stamp.timestamp() * 1000,
            "value": {"measurements": base64.b64encode(bytes([0, 12, 255, 18]) + bytes(1436)).decode()}}]}),
        ("wellness", "wellness:pai:user:synthetic", {"items": [{"time": stamp.timestamp() * 1000,
            "dailyPai": 15, "totalPai": 85, "lowZoneMinutes": 30, "lowZoneLowerLimit": 110}]}),
        ("daily_summary", "events:DailyHealth:summary:synthetic", {"items": [{"eventType": "DailyHealth",
            "value": {"samples": [{"dateString": "2026-01-03", "steps": 1440, "stepGoal": 1000,
                "totalBurningDuration": 40, "burningDurationGoal": 30}]}}]}),
        ("wellness", "wellness:lactate_threshold:v2:synthetic", {"items": [{"value": {"samples": [
            {"dateString": "2026-01-03", "lactateThresholdHr": 170, "lactateThresholdPace": 280}]}}]}),
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        for stream, key, payload in payloads:
            report = manager._persist_record(FetchedRecord(RawRecord(stream, key, stamp,
                datetime(2026, 1, 4, tzinfo=timezone.utc), payload)), repo, user)
            assert report.records_written > 0, (stream, key, report)
    def query(path):
        response = client.get("/api/" + path, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()
    scope = "?from=2026-01-03&to=2026-01-03"
    result = query("health/daily-summary" + scope + "&include_hourly=true&include_minutes=true")
    assert result["days"][0]["steps"] == 1440
    assert result["days"][0]["band_activity"]["hourly_steps"] == [60] * 24
    goals = result["goals"]["goal_comparisons"]["comparisons"]
    burning = next(goal for goal in goals if goal["metric"] == "vendor_burning_minutes")
    assert burning["target"] == 30 and burning["achieved"] is True
    result = query("health/heart-rate?day=2026-01-03&include_series=true")
    assert result["band_minutes"][0]["minutes_covered"] == 2
    assert sum(value is not None for value in result["band_minutes"][0]["per_minute"]) == 2
    result = query("health/sleep" + scope)
    assert result["nights"][0]["rem_sleep"] == 10
    result = query("health/body-composition" + scope)
    assert result["records"][0]["measurements"]["body_fat_percent"]["value"] == 21
    result = query("health/summary/stress" + scope + "&include_samples=true")
    assert result["signals"]["single_stress"]["intraday"][0]["summary"]["mean"] == 61
    result = query("health/summary/respiratory_rate" + scope + "&include_samples=true")
    assert result["signals"]["respiratory_rate"]["intraday"][0]["summary"]["mean"] == 15
    result = query("health/summary/pai" + scope)
    assert result["signals"]["pai_total"]["daily"][0]["summary"]["mean"] == 85
    result = query("health/training-thresholds" + scope)
    assert result["current"]["lactate_threshold_hr"][0]["value"] == 170
    assert query("health/period-summary" + scope)["state"] == "ready"
    assert len(query("sources/zepp/schema")["tools"]) == 14
