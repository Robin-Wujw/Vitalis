"""Additive activity totals preserve source partitions and missing-day coverage."""
from datetime import date

from vitalis.application.health_analytics import HealthAnalytics
from vitalis.intelligence.observation_statistics import grouped_observations

START, END = date(2026, 9, 1), date(2026, 9, 3)


def observation(day, value, device="watch-one"):
    return {"date": day, "value": value, "source": "zepp", "source_scope": "device",
            "device_id": device, "unit": "steps", "qualification": "vendor_recorded"}


def test_totals_sum_observed_days_without_mixing_devices_or_filling_missing_days():
    rows = [observation(START, 1000), observation(END, 2000), observation(START, 8000, "watch-two")]
    groups = grouped_observations(rows, start=START, end=END, timezone_name="UTC", include_daily_totals=True)
    by_device = {row["device_id"]: row for row in groups}
    one = by_device["watch-one"]
    assert one["summary"]["total"] == 3000
    assert one["summary"]["mean"] == 1500
    assert one["summary"]["total_qualification"] == "observed_days_only"
    assert one["coverage"]["days_with_data"] == 2
    assert one["coverage"]["days_requested"] == 3
    assert by_device["watch-two"]["summary"]["total"] == 8000


def test_duplicate_daily_candidates_do_not_create_an_inflated_total():
    rows = [observation(START, 1000), observation(START, 1200), observation(END, 2000)]
    group = grouped_observations(rows, start=START, end=END, timezone_name="UTC", include_daily_totals=True)[0]
    assert group["summary"]["total"] is None
    assert group["summary"]["total_qualification"] == "ambiguous_daily_records"
    assert group["summary"]["ambiguous_days"] == 1


def test_non_additive_observations_have_no_total_and_recorded_zero_is_preserved():
    rows = [observation(START, 0)]
    plain = grouped_observations(rows, start=START, end=END, timezone_name="UTC")[0]
    assert "total" not in plain["summary"]
    total = grouped_observations(rows, start=START, end=END, timezone_name="UTC", include_daily_totals=True)[0]
    assert total["summary"]["total"] == 0
    assert total["coverage"]["days_with_data"] == 1
    assert grouped_observations([], start=START, end=END, timezone_name="UTC", include_daily_totals=True) == []


def test_daily_and_period_api_expose_the_same_qualified_totals(client, issue_token, monkeypatch):
    from vitalis.entrypoints.api.routes import health_analytics
    from tests.test_health_analytics_parity import reader

    source = reader()
    source.activity_records = lambda *a, **k: tuple({"date": day, "data": {
        "steps": steps, "distance_km": distance, "calories": calories,
        "source": "zepp", "source_scope": "device", "device_id": "synthetic-watch"}}
        for day, steps, distance, calories in [(START, 1000, .8, 50), (END, 2000, 1.5, 100)])
    service = HealthAnalytics(source, timezone_name="UTC")
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: service)
    headers = {"Authorization": "Bearer " + issue_token("activity-totals", scopes=["read"])}
    query = "?from=2026-09-01&to=2026-09-03"
    daily = client.get("/api/health/daily-summary" + query, headers=headers)
    period = client.get("/api/health/period-summary" + query, headers=headers)
    assert daily.status_code == period.status_code == 200
    totals = daily.json()["summary"]
    assert totals == period.json()["activity"]["summary"]
    assert totals["steps"][0]["summary"]["total"] == 3000
    assert totals["distance_km"][0]["summary"]["total"] == 2.3
    assert totals["calories"][0]["summary"]["total"] == 150
    assert totals["active_minutes"] == []
