from datetime import date, timedelta

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.trend import TrendEngine
from vitalis.intelligence.weekly import WeeklyProfileEngine


TARGET = date(2026, 8, 28)


def _series(metric, values, unit, *, device=None):
    return [
        SeriesPoint(
            metric=metric,
            value=value,
            unit=unit,
            day=TARGET - timedelta(days=len(values) - index - 1),
            observed_at=TARGET - timedelta(days=len(values) - index - 1),
            source="zepp",
            source_scope="device" if device else "normalized_daily_record",
            device_id=device,
        )
        for index, value in enumerate(values)
    ]


def _raw_week():
    raw = RawDailyProfile(user_id="weekly-user", day=TARGET)
    raw.series = {
        "sleep_duration": _series("sleep_duration", [390] * 7 + [450] * 7, "min"),
        "hrv_rmssd": _series("hrv_rmssd", [50] * 7 + [55] * 7, "ms", device="helio"),
        "resting_hr": _series("resting_hr", [58] * 7 + [55] * 7, "bpm"),
        "training_load": _series("training_load", [20] * 7 + [30] * 7, "load"),
        "steps": _series("steps", [6000] * 7 + [8000] * 7, "steps"),
    }
    raw.sleep_by_day = {}
    raw.activity_by_day = {}
    raw.training_by_day = {}
    for offset in range(14):
        day = TARGET - timedelta(days=offset)
        current = offset < 7
        raw.sleep_by_day[day] = {
            "date": day,
            "sleep_duration": 450 if current else 390,
            "bedtime": "23:00" if offset % 2 else "23:10",
        }
        raw.activity_by_day[day] = {
            "date": day,
            "steps": 8000 if current else 6000,
            "active_minutes": 40,
        }
        raw.training_by_day[day] = {
            "date": day,
            "total_load": 30 if current else 20,
            "total_duration": 30 if current and offset in {1, 3} else 0,
            "workout_count": 1 if current and offset in {1, 3} else 0,
        }
    raw.workouts = [
        {
            "local_day": TARGET - timedelta(days=1),
            "data": {
                "sport_mode_label": "户外跑",
                "training_family": "aerobic",
                "duration": 40,
            },
        },
        {
            "local_day": TARGET - timedelta(days=3),
            "data": {
                "sport_mode_label": "力量训练",
                "training_family": "strength",
                "duration": 45,
            },
        },
    ]
    return raw


def test_weekly_profile_separates_facts_inferences_and_actions():
    raw = _raw_week()
    profile = WeeklyProfileEngine().build("weekly-run", raw, TrendEngine().calculate(raw), [])

    assert profile.period_start == TARGET - timedelta(days=6)
    assert profile.facts.sleep.average_minutes == 450
    assert profile.facts.sleep.change_percent == 15.4
    assert profile.facts.training.workout_count == 2
    assert profile.facts.training.sport_mode_counts == {"力量训练": 1, "户外跑": 1}
    assert profile.facts.activity.average_steps == 8000
    assert any("睡眠时长" in item for item in profile.inferences.key_changes)
    codes = {item.code for item in profile.actions.recommendations}
    assert {"ADD_AEROBIC_VOLUME", "ADD_STRENGTH_SESSIONS"} <= codes


def test_weekly_recovery_event_takes_action_priority():
    raw = _raw_week()
    event = HealthEvent(
        id="recovery-event",
        type="HRV_DROP",
        type_label="心率变异性持续下降",
        severity=EventSeverity.MODERATE,
        severity_label="中等",
        metric="hrv_rmssd",
        metric_label="心率变异性 RMSSD",
        start_date=TARGET - timedelta(days=2),
        end_date=TARGET,
        duration_days=3,
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        summary="连续下降。",
    )

    profile = WeeklyProfileEngine().build(
        "weekly-run",
        raw,
        TrendEngine().calculate(raw),
        [event],
    )

    recommendation = profile.actions.recommendations[0]
    assert recommendation.code == "PRIORITIZE_RECOVERY"
    assert "完整休息日" in recommendation.action
