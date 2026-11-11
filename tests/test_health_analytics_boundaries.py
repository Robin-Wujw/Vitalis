"""Timezone coverage and qualification partitions on synthetic stored reads."""
from datetime import UTC, date, datetime
from types import SimpleNamespace

from vitalis.application.health_analytics import HealthAnalytics
from vitalis.application.ports import HealthDailyMetricRead, HealthMetricRead


def test_body_coverage_counts_local_observation_days():
    rows = [HealthMetricRead(timestamp=stamp, value=70, unit="kg", source="zepp",
        source_scope="scale", device_id="synthetic", source_record_id=str(i), sample_ordinal=0)
        for i, stamp in enumerate((datetime(2026, 9, 1, 23, tzinfo=UTC), datetime(2026, 9, 2, 1, tzinfo=UTC)))]
    reader = SimpleNamespace(metric_samples=lambda _, metric, *args, **kwargs: rows if metric == "weight" else [])
    result = HealthAnalytics(reader, timezone_name="Asia/Singapore").body_composition(
        "synthetic", date(2026, 9, 2), date(2026, 9, 2))
    assert result["sample_count"] == 2
    assert result["coverage"]["days_with_data"] == 1


def test_threshold_current_separates_reference_and_unverified_measurements():
    rows = [HealthDailyMetricRead(date=date(2026, 9, i + 1), metric="lactate_threshold_hr", value=160 + i,
        unit="bpm", source="zepp", source_scope="device", device_id="synthetic",
        source_field="lactateThresholdHr", qualification=qualification)
        for i, qualification in enumerate(("vendor_reference", "vendor_unit_unverified"))]
    reader = SimpleNamespace(daily_metrics=lambda _, start, end, metric: rows if metric == "lactate_threshold_hr" else [],
        workout_statistics_workouts=lambda *args, **kwargs: [])
    result = HealthAnalytics(reader).thresholds("synthetic", date(2026, 9, 1), date(2026, 9, 3))
    assert len(result["current"]["lactate_threshold_hr"]) == 2


def test_period_summary_isolates_failed_sections_and_propagates_partial(monkeypatch):
    from vitalis.application.workout_statistics import WorkoutStatistics

    service = HealthAnalytics(SimpleNamespace())
    service.daily_summary = lambda *args: {"state": "ready", "sample_count": 2}
    service.sleep = lambda *args: (_ for _ in ()).throw(OSError("synthetic private error"))
    service.metrics = lambda *args: {"state": "no_data"}
    monkeypatch.setattr(WorkoutStatistics, "statistics", lambda *args, **kwargs: {"state": "partial", "coverage": {"truncated": True}})
    result = service.period_summary("synthetic", date(2026, 9, 1), date(2026, 9, 2))
    assert result["state"] == "partial"
    assert result["activity"]["sample_count"] == 2
    assert result["sleep"]["state"] == "failed"
    assert result["sleep"]["failure_code"] == "section_unavailable"
    assert result["sleep"]["retryable"] is True
    assert result["training"]["coverage"]["truncated"] is True
    assert "synthetic private error" not in str(result)
    assert result["section_counts"] == {"ready": 1, "partial": 1, "failed": 1, "no_data": 5}


def test_period_summary_all_failures_use_non_200_http_error(client, issue_token, monkeypatch):
    from vitalis.application.workout_statistics import WorkoutStatistics
    from vitalis.entrypoints.api.routes import health_analytics

    def fail(*args, **kwargs):
        raise OSError("synthetic private failure")

    service = HealthAnalytics(SimpleNamespace())
    service.daily_summary = service.sleep = service.metrics = fail
    monkeypatch.setattr(WorkoutStatistics, "statistics", fail)
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: service)
    headers = {"Authorization": "Bearer " + issue_token("all-sections-fail", scopes=["read"])}
    result = client.get("/api/health/period-summary?from=2026-09-01&to=2026-09-02", headers=headers)
    assert result.status_code == 503
    assert result.json()["state"] == "failed"
    assert result.json()["failure_code"] == "service_unavailable"


def test_goal_summary_uses_unique_qualified_pairs_and_counts_missing_days():
    from vitalis.intelligence.observation_statistics import goal_comparisons

    base = {"date": date(2026, 9, 1), "source": "zepp", "source_scope": "user_summary", "device_id": None,
            "unit": "steps", "qualification": "vendor_recorded", "source_field": "totalSteps"}
    observed = {**base, "metric": "steps", "value": 8000}
    target = {**base, "metric": "step_goal", "value": 7000, "qualification": "vendor_user_target", "source_field": "stepGoal"}
    result = goal_comparisons([observed, target], start=date(2026, 9, 1), end=date(2026, 9, 2))
    assert result["state"] == "partial"
    count = next(row for row in result["summary"] if row["metric"] == "steps")
    assert (count["compared_days"], count["achieved_days"], count["missing_days"]) == (1, 1, 1)
    ambiguous = goal_comparisons([observed, target, {**observed, "value": 100, "source_field": "otherSource"}],
        start=date(2026, 9, 1), end=date(2026, 9, 1))
    assert ambiguous["comparisons"] == []
    count = next(row for row in ambiguous["summary"] if row["metric"] == "steps")
    assert count["compared_days"] == 0 and count["ambiguous_days"] == 1
