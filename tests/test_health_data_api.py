"""API coverage for timestamped metrics, daily metrics and workouts."""

from datetime import date, datetime, timezone

from vitalis.models import DailyMetric, MetricSample, Workout, WorkoutType
from vitalis.storage import HealthRepository, session_scope


def test_metric_series_and_daily_metrics(client):
    user_id = "metrics-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_metric_samples([
            MetricSample(user_id=user_id, metric="heart_rate", timestamp=datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc), value=60, unit="bpm"),
            MetricSample(user_id=user_id, metric="heart_rate", timestamp=datetime(2026, 8, 25, 8, 35, tzinfo=timezone.utc), value=80, unit="bpm"),
        ])
        repo.save_daily_metrics([
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="readiness", value=84, unit="score")
        ])

    series = client.get(
        "/api/v1/health/metrics/heart_rate?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z&resolution=1h",
        headers={"X-User-Id": user_id},
    )
    assert series.status_code == 200
    point = series.json()["points"][0]
    assert point["value"] == 70
    assert point["min"] == 60
    assert point["max"] == 80

    daily = client.get(
        "/api/v1/health/daily-metrics?from=2026-08-25&to=2026-08-25&metric=readiness",
        headers={"X-User-Id": user_id},
    )
    assert daily.status_code == 200
    assert daily.json()["metrics"][0]["value"] == 84


def test_metric_writes_deduplicate_same_batch(client):
    user_id = "duplicate-metrics-user"
    timestamp = datetime(2026, 8, 25, 8, 5, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        written_samples = repo.save_metric_samples([
            MetricSample(user_id=user_id, metric="hrv_rmssd", timestamp=timestamp, value=52, unit="ms"),
            MetricSample(user_id=user_id, metric="hrv_rmssd", timestamp=timestamp, value=59, unit="ms"),
        ])
        written_daily = repo.save_daily_metrics([
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="hrv_readiness", value=70),
            DailyMetric(user_id=user_id, date=date(2026, 8, 25), metric="hrv_readiness", value=75),
        ])

    assert written_samples == 1
    assert written_daily == 1

    series = client.get(
        "/api/v1/health/metrics/hrv_rmssd?from=2026-08-25T00:00:00Z&to=2026-08-26T00:00:00Z",
        headers={"X-User-Id": user_id},
    )
    assert series.status_code == 200
    assert [point["value"] for point in series.json()["points"]] == [59]

    daily = client.get(
        "/api/v1/health/daily-metrics?from=2026-08-25&to=2026-08-25&metric=hrv_readiness",
        headers={"X-User-Id": user_id},
    )
    assert daily.status_code == 200
    assert [metric["value"] for metric in daily.json()["metrics"]] == [75]


def test_workout_list_and_detail(client):
    user_id = "workout-user"
    workout = Workout(
        user_id=user_id,
        workout_id="run-1",
        started_at=datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc),
        type=WorkoutType.RUNNING,
        duration=42,
        heart_rate_avg=145,
        vendor_source="run.gps",
    )
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.save_workout(workout)
        assert repo.save_workout_detail(user_id, "run-1", {"samples": [{"heart_rate": 145}]})

    listing = client.get(
        "/api/v1/health/workouts?from=2026-08-01&to=2026-08-31",
        headers={"X-User-Id": user_id},
    )
    assert listing.status_code == 200
    assert listing.json()["workouts"][0]["detail_available"] is True

    detail = client.get(
        "/api/v1/health/workouts/run-1",
        headers={"X-User-Id": user_id},
    )
    assert detail.status_code == 200
    assert detail.json()["detail"]["samples"][0]["heart_rate"] == 145
