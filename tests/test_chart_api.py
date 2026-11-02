"""User-scoped chart reads and immutable report chart projections."""
from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.domain import MetricSample


def test_chart_api_is_read_only_user_scoped_and_preserves_gaps(client):
    day = date(2026, 10, 7)
    with session_scope() as db:
        repo = HealthRepository(db)
        for user, value in (("chart-api-user", 60), ("chart-api-other", 180)):
            repo.upsert_user(user)
            db.flush()
            repo.save_metric_samples([MetricSample(user_id=user, metric="heart_rate", timestamp=datetime(2026,10,7,0,minute,tzinfo=timezone.utc),
                value=value+minute, unit="bpm", source="zepp", source_scope="device", device_id="synthetic-device") for minute in (0, 20)])
    result = client.get(f"/api/charts?from={day}&to={day}&mode=intraday", headers={"X-User-Id":"chart-api-user"})
    assert result.status_code == 200
    payload = result.json()
    assert payload["user_id"] == "chart-api-user"
    chart = next(c for c in payload["charts"] if c["key"] == "heart_rate")
    assert [p["value"] for p in chart["series"][0]["points"] if p["value"] is not None] == [60,80]
    assert any(p["value"] is None for p in chart["series"][0]["points"])
    denied = client.get(f"/api/charts?from={day}&to={day}", authenticate=False)
    assert denied.status_code == 401


def test_chart_api_rejects_wide_intraday_window(client):
    result = client.get("/api/charts?from=2026-10-01&to=2026-10-31&mode=intraday", headers={"X-User-Id":"chart-api-budget"})
    assert result.status_code == 422
    assert result.json()["state"] == "failed"


def test_missing_report_chart_does_not_build_analysis(client):
    result = client.get("/api/reports/evening/charts?day=2026-10-07", headers={"X-User-Id":"chart-api-missing"})
    assert result.status_code == 404


def test_report_charts_are_stored_with_daily_and_calendar_reports():
    from vitalis import bootstrap
    from vitalis.domain import ActivityRecord, NormalizedDaily, SleepRecord
    user = "chart-report-synthetic"
    target = date(2026, 10, 7)
    cutoff = datetime(2026,10,7,14,tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.bind_source_mode(user, "mock")
        for offset in range(65):
            day = target-timedelta(days=offset)
            repo.save_daily(NormalizedDaily(user_id=user, date=day,
                sleep=SleepRecord(user_id=user,date=day,sleep_duration=420+offset%30),
                activity=ActivityRecord(user_id=user,date=day,steps=6000+offset,observed_fields=["steps"])))
        repo.save_metric_samples([MetricSample(user_id=user,metric="heart_rate", timestamp=cutoff-timedelta(minutes=1),value=65,unit="bpm")])
    result = bootstrap.get_intelligence_command(today_factory=lambda:target, now_factory=lambda:cutoff).analyze(user,target)
    assert any(c.key == "heart_rate" for c in result.daily.charts)
    assert any(c.key == "daily_sleep_duration" for c in result.weekly.charts)
    assert any(c.key == "daily_sleep_duration" for c in result.monthly.charts)
    saved = bootstrap.get_intelligence_query().daily(user,target)
    assert saved.charts == result.daily.charts
