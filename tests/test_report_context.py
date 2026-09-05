from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.analyzers import TrainingAnalyzer
from vitalis.intelligence.contracts import DailyProfile
from vitalis.intelligence.profile import ProfileLoader, RawDailyProfile
from vitalis.models import DailyMetric, MetricSample
from vitalis.storage import HealthRepository, session_scope


def test_daily_contract_version_defaults_match_validation_literals():
    properties = DailyProfile.model_json_schema()["properties"]
    for name in ("schema_version", "intelligence_version", "decision_policy_version"):
        assert properties[name]["const"] == DailyProfile.model_fields[name].default


def test_profile_cutoff_preserves_date_precision_and_excludes_future_samples():
    user_id = "report-cutoff"
    day = date(2026, 9, 4)
    cutoff = datetime(2026, 9, 4, 4, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_daily_metrics([DailyMetric(
            user_id=user_id, date=day, metric="sleep_hrv", value=60,
            unit="ms", source_scope="user_fused",
        )])
        repo.save_metric_samples([
            MetricSample(
                user_id=user_id, metric="hrv_rmssd", timestamp=timestamp,
                value=value, unit="ms", source_scope="device", device_id="strap",
            )
            for timestamp, value in (
                (cutoff - timedelta(hours=1), 50),
                (cutoff + timedelta(hours=1), 80),
            )
        ])
        raw = ProfileLoader(repo).load(user_id, day, as_of=cutoff)

    assert [point.value for point in raw.series["hrv_rmssd"]] == [50]
    assert raw.report_context["as_of"] == cutoff.isoformat()
    assert raw.report_context["target_day_complete"] is False
    assert raw.report_context["latest_observations"]["sleep_hrv"] == day.isoformat()
    assert raw.training_history_coverage["status"] == "UNKNOWN"
    assert raw.training_history_coverage["prior_7d_verified"] is False


def test_same_day_training_order_uses_start_time():
    day = date(2026, 9, 4)
    raw = RawDailyProfile(user_id="training-order", day=day)
    raw.training_by_day = {
        day: {"total_duration": 60, "total_load": 60, "workout_count": 2},
    }
    raw.workouts = [
        {
            "workout_id": str(hour), "local_day": day,
            "started_at": datetime(2026, 9, 4, hour, tzinfo=timezone.utc),
            "data": {
                "type": "strength", "training_family": "strength",
                "sport_mode": "strength_training", "sport_mode_label": "strength",
                "duration": 30, "load": 30,
            },
        }
        for hour in (2, 10)
    ]
    training = TrainingAnalyzer().analyze(raw, {})
    assert [item.started_at.hour for item in training.recent_workouts] == [10, 2]


def test_missing_training_load_and_unverified_today_are_not_zero():
    day = date(2026, 9, 4)
    raw = RawDailyProfile(user_id="training-unknown", day=day)
    raw.training_by_day = {day - timedelta(days=1): {
        "total_duration": None, "total_load": None, "workout_count": 1,
    }}
    raw.training_history_coverage = {
        "status": "UNKNOWN", "verified_days": [], "prior_7d_verified": False,
    }
    training = TrainingAnalyzer().analyze(raw, {})
    assert training.today_workouts is None
    assert training.today_load is None
    assert training.duration_7d is None
    assert training.load_7d is None
    assert training.load_28d is None
    assert training.load_7d_reference is None
    assert training.history_coverage["prior_7d_verified"] is False
