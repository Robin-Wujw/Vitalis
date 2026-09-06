from datetime import date, timedelta

from vitalis.intelligence.monthly import MonthlyProfileEngine
from vitalis.intelligence.period_activity import period_training_details
from vitalis.intelligence.profile import RawDailyProfile


TARGET = date(2026, 8, 28)


def _coverage(days):
    return {
        "status": "PARTIAL" if days < 28 else "COMPLETE",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat()
            for offset in range(days)
        ],
    }


def test_monthly_unknown_days_are_not_rest_days_or_quota_deficits():
    raw = RawDailyProfile(user_id="monthly-unknown", day=TARGET)
    for offset in range(20):
        day = TARGET - timedelta(days=offset)
        raw.training_by_day[day] = {
            "date": day,
            "workout_count": 0,
            "total_duration": 0,
            "total_load": 0,
        }
    raw.training_history_coverage = _coverage(20)

    profile = MonthlyProfileEngine().build(
        "monthly-unknown-run", raw, [], [], []
    )
    training = profile.facts.training
    codes = {item.code for item in profile.actions.recommendations}

    assert training.record_days == 20
    assert training.unknown_days == 8
    assert training.coverage_status == "PARTIAL"
    assert training.rest_days == 20
    assert "MONTHLY_AEROBIC_BALANCE" not in codes
    assert "MONTHLY_STRENGTH_BALANCE" not in codes


def test_period_training_details_preserve_observed_zero_calories():
    raw = RawDailyProfile(user_id="zero-calories", day=TARGET)
    raw.workouts = [{
        "workout_id": "zero-cal",
        "local_day": TARGET,
        "data": {
            "type": "running",
            "training_family": "aerobic",
            "duration": 20,
            "distance_km": 0,
            "calories": 0,
            "observed_fields": ["distance_km", "calories"],
        },
    }]

    details = period_training_details(raw, TARGET - timedelta(days=6), TARGET)

    assert details["running_distance_km"] == 0
    assert details["workout_calories_kcal"] == 0
    assert details["workout_calories_sessions"] == 1


def test_monthly_training_details_keep_running_and_strength_evidence():
    raw = RawDailyProfile(user_id="monthly-details", day=TARGET)
    for offset in range(28):
        day = TARGET - timedelta(days=offset)
        raw.training_by_day[day] = {
            "date": day,
            "workout_count": 1 if offset in {1, 3} else 0,
            "total_duration": 40 if offset in {1, 3} else 0,
            "total_load": 10 if offset in {1, 3} else 0,
        }
    raw.training_history_coverage = _coverage(28)
    raw.workouts = [
        {
            "workout_id": "run-1",
            "local_day": TARGET - timedelta(days=1),
            "data": {
                "type": "running",
                "training_family": "aerobic",
                "duration": 40,
                "distance_km": 5.0,
                "calories": 300,
                "observed_fields": ["distance_km", "calories"],
                "sport_mode_label": "户外跑",
            },
        },
        {
            "workout_id": "strength-1",
            "local_day": TARGET - timedelta(days=3),
            "data": {
                "type": "strength",
                "training_family": "strength",
                "duration": 40,
                "calories": 200,
                "observed_fields": ["calories"],
                "sport_mode_label": "力量训练",
            },
            "detail": {
                "strength_sets": [
                    {
                        "exercise_id": "squat",
                        "exercise_name": "深蹲",
                        "repetitions": 8,
                    },
                    {
                        "exercise_id": "squat",
                        "exercise_name": "深蹲",
                        "repetitions": 8,
                    },
                ]
            },
            "detail_available": True,
        },
    ]

    profile = MonthlyProfileEngine().build(
        "monthly-details-run", raw, [], [], []
    )
    training = profile.facts.training

    assert training.coverage_status == "COMPLETE"
    assert training.running_sessions == 1
    assert training.running_distance_km == 5
    assert training.running_duration_minutes == 40
    assert training.running_classification_counts
    assert training.strength_duration_minutes == 40
    assert training.strength_explicit_sessions == 1
    assert training.strength_sets == 2
    assert training.workout_calories_kcal == 500
    assert training.workout_calories_sessions == 2
