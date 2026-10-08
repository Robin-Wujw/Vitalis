from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.trend import TrendEngine
from vitalis.intelligence.weekly import WeeklyProfileEngine


TARGET = date(2026, 8, 28)


def _series(metric, values, unit, *, device=None, scope=None):
    return [
        SeriesPoint(
            metric=metric,
            value=value,
            unit=unit,
            day=TARGET - timedelta(days=len(values) - index - 1),
            observed_at=TARGET - timedelta(days=len(values) - index - 1),
            source="zepp",
            source_scope=scope or ("device" if device else "normalized_daily_record"),
            device_id=device,
        )
        for index, value in enumerate(values)
    ]


def _raw_week():
    raw = RawDailyProfile(user_id="weekly-user", day=TARGET)
    raw.series = {
        "sleep_duration": _series("sleep_duration", [390] * 7 + [450] * 7, "min"),
        "hrv_rmssd": _series("hrv_rmssd", [50] * 7 + [55] * 7, "ms", device="helio"),
        "sleep_hrv": _series("sleep_hrv", list(range(50, 64)), "ms", device="balance"),
        "resting_hr": _series("resting_hr", [58] * 7 + [55] * 7, "bpm"),
        "training_load": _series("training_load", [20] * 7 + [30] * 7, "load"),
        "steps": _series("steps", [6000] * 7 + [8000] * 7, "steps"),
    }
    raw.sleep_by_day = {}
    raw.activity_by_day = {}
    raw.training_by_day = {}
    raw.training_history_coverage = {
        "status": "COMPLETE",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat() for offset in range(7)
        ],
    }
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
    profile = WeeklyProfileEngine().build(
        "weekly-run", raw, TrendEngine().calculate(raw), [], period_mode="rolling"
    )

    assert profile.period_start == TARGET - timedelta(days=6)
    assert profile.facts.sleep.average_minutes == 450
    assert profile.facts.sleep.change_percent == 15.4
    assert profile.facts.training.workout_count == 2
    assert profile.facts.training.sport_mode_counts == {"力量训练": 1, "户外跑": 1}
    assert profile.facts.activity.average_steps == 8000
    assert profile.facts.recovery.sleep_hrv_device_id == "balance"
    assert len(profile.facts.recovery.sleep_hrv_daily) == 7
    assert profile.facts.recovery.sleep_hrv_daily[-1].value_ms == 63
    assert any("睡眠时长" in item for item in profile.inferences.key_changes)
    codes = {item.code for item in profile.actions.recommendations}
    assert "MAINTAIN_PLAN" in codes
    assert "ADD_AEROBIC_VOLUME" not in codes
    assert "ADD_STRENGTH_SESSIONS" not in codes


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
        period_mode="rolling",
    )

    recommendation = profile.actions.recommendations[0]
    assert recommendation.code == "PRIORITIZE_RECOVERY"
    assert "完整休息日" in recommendation.action


def test_weekly_recovery_prefers_sleep_hrv_over_all_day_rmssd():
    raw = _raw_week()
    raw.series["sleep_hrv"] = _series(
        "sleep_hrv", [62] * 7 + [68] * 7, "ms", device="helio"
    )

    profile = WeeklyProfileEngine().build(
        "weekly-run", raw, TrendEngine().calculate(raw), [], period_mode="rolling"
    )

    assert profile.facts.recovery.hrv_metric == "sleep_hrv"
    assert profile.facts.recovery.hrv_median_ms == 68


def test_weekly_recovery_prefers_zepp_fused_sleep_hrv_over_device_stream():
    raw = _raw_week()
    raw.series["sleep_hrv"] += _series(
        "sleep_hrv", [64] * 7 + [66] * 7, "ms", scope="user_fused"
    )

    profile = WeeklyProfileEngine().build(
        "weekly-run", raw, TrendEngine().calculate(raw), [], period_mode="rolling"
    )

    assert profile.facts.recovery.hrv_device_id is None
    assert profile.facts.recovery.hrv_median_ms == 66
    assert profile.facts.recovery.sleep_hrv_device_id is None
    assert profile.facts.recovery.sleep_hrv_daily[-1].value_ms == 66


def test_weekly_empty_training_history_keeps_nullable_totals_missing():
    raw = RawDailyProfile(user_id="empty-week", day=TARGET)

    profile = WeeklyProfileEngine().build("empty-run", raw, [], [], period_mode="rolling")
    training = profile.facts.training

    assert training.record_days == 0
    assert training.unknown_days == 7
    assert training.coverage_status == "UNKNOWN"
    assert training.workout_count == 0
    assert training.training_days == 0
    assert training.rest_days is None
    assert training.duration_minutes is None
    assert training.vendor_load is None
    assert training.aerobic_minutes is None
    assert training.strength_sessions is None
    assert profile.actions.recommendations[0].code == "WEEKLY_INSUFFICIENT_DATA"


def test_weekly_complete_seven_record_days_count_untrained_days_as_rest():
    raw = RawDailyProfile(user_id="complete-rest-week", day=TARGET)
    for offset in range(7):
        day = TARGET - timedelta(days=offset)
        raw.training_by_day[day] = {
            "date": day,
            "workout_count": 0,
            "total_duration": 0,
            "total_load": 0,
        }
    raw.training_history_coverage = {
        "status": "COMPLETE",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat() for offset in range(7)
        ],
    }

    profile = WeeklyProfileEngine().build("complete-rest-run", raw, [], [], period_mode="rolling")
    training = profile.facts.training

    assert training.coverage_status == "COMPLETE"
    assert training.record_days == 7
    assert training.unknown_days == 0
    assert training.training_days == 0
    assert training.rest_days == 7
    assert training.duration_minutes == 0
    assert training.vendor_load == 0
    assert training.aerobic_minutes == 0
    assert training.strength_sessions == 0


def test_weekly_partial_history_never_generates_catch_up_or_maintain_quota():
    raw = RawDailyProfile(user_id="partial-week", day=TARGET)
    for offset in range(3):
        day = TARGET - timedelta(days=offset)
        raw.training_by_day[day] = {
            "date": day,
            "workout_count": 0,
            "total_duration": 0,
            "total_load": 0,
        }
    raw.training_history_coverage = {
        "status": "PARTIAL",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat() for offset in range(3)
        ],
    }

    profile = WeeklyProfileEngine().build("partial-run", raw, [], [], period_mode="rolling")
    codes = {item.code for item in profile.actions.recommendations}

    assert profile.facts.training.record_days == 3
    assert profile.facts.training.unknown_days == 4
    assert "ADD_AEROBIC_VOLUME" not in codes
    assert "ADD_STRENGTH_SESSIONS" not in codes
    assert "MAINTAIN_PLAN" not in codes
    assert "WEEKLY_INSUFFICIENT_DATA" in codes


def test_weekly_slices_29_day_coverage_and_keeps_stored_workouts_as_lower_bound():
    raw = RawDailyProfile(user_id="slice-week", day=TARGET)
    workout_day = TARGET - timedelta(days=1)
    raw.training_by_day[workout_day] = {
        "date": workout_day,
        "workout_count": 1,
        "total_duration": None,
        "total_load": None,
    }
    raw.workouts = [{
        "local_day": workout_day,
        "data": {
            "sport_mode_label": "户外跑",
            "training_family": "aerobic",
            "duration": 35,
        },
    }]
    raw.training_history_coverage = {
        "status": "PARTIAL",
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat() for offset in range(7)
        ] + [(TARGET - timedelta(days=7 + offset)).isoformat() for offset in range(2)],
    }

    profile = WeeklyProfileEngine().build("slice-run", raw, [], [], period_mode="rolling")
    training = profile.facts.training

    assert training.coverage_status == "COMPLETE"
    assert training.record_days == 7
    assert training.training_days == 1
    assert training.workout_count == 1
    assert training.duration_minutes is None
    assert training.vendor_load is None
    assert training.aerobic_minutes == 35
    assert training.rest_days == 6


def test_weekly_comparison_requires_four_days_in_each_period():
    raw = RawDailyProfile(user_id="comparison-week", day=TARGET)
    for offset in range(7):
        day = TARGET - timedelta(days=offset)
        raw.sleep_by_day[day] = {"sleep_duration": 420}
        raw.activity_by_day[day] = {"steps": 8000}
    for offset in range(3):
        day = TARGET - timedelta(days=7 + offset)
        raw.sleep_by_day[day] = {"sleep_duration": 390}
        raw.activity_by_day[day] = {"steps": 6000}

    profile = WeeklyProfileEngine().build("comparison-run", raw, [], [], period_mode="rolling")

    assert profile.facts.sleep.previous_available_days == 3
    assert profile.facts.sleep.change_percent is None
    assert profile.facts.activity.previous_available_days == 3
    assert profile.facts.activity.steps_change_percent is None


def test_calendar_weekly_steps_use_activity_average_in_report_changes():
    from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

    raw = RawDailyProfile(
        user_id="weekly-step-statistics",
        day=date(2026, 10, 5),
        as_of=datetime(2026, 10, 5, 9, tzinfo=timezone.utc),
        timezone_name="UTC",
    )
    values = [1000] * 6 + [13000] + [2000] * 6 + [8900]
    first_day = date(2026, 9, 21)
    raw.series["steps"] = [
        SeriesPoint(
            metric="steps", value=value, unit="steps",
            day=first_day + timedelta(days=index),
            observed_at=first_day + timedelta(days=index),
            source="zepp", source_scope="normalized_daily_record",
        )
        for index, value in enumerate(values)
    ]

    profile = WeeklyProfileEngine().build("weekly-step-run", raw, [], [])

    assert profile.period_start == date(2026, 9, 28)
    assert profile.period_end == date(2026, 10, 4)
    assert profile.facts.activity.available_days == 7
    assert profile.facts.activity.previous_available_days == 7
    assert profile.facts.activity.steps_change_percent == 10.0
    median_trend = next(item for item in profile.inferences.trends if item.metric == "steps")
    assert median_trend.change_percent == 100.0
    assert not any("步数" in item for item in profile.inferences.key_changes)

    sections = WeeklyBriefingEngine().build(profile).model_dump(mode="json")["sections"]
    recovery = next(item for item in sections if item["key"] == "sleep_recovery")
    activity = next(item for item in sections if item["key"] == "activity_feedback")
    assert not any("步数" in item for item in recovery["interpretation"])
    assert any("日均步数" in item and "10.0%" in item for item in activity["interpretation"])


def test_activity_only_weekly_report_is_partial_not_globally_unavailable():
    raw = RawDailyProfile(user_id="activity-only-week", day=TARGET)
    raw.series["steps"] = _series(
        "steps", [1000] * 14, "steps"
    )

    profile = WeeklyProfileEngine().build(
        "activity-only-run", raw, [], [], period_mode="rolling"
    )

    assert profile.facts.activity.available_days == 7
    assert profile.data_quality.status.value == "PARTIAL"


def test_weekly_sleep_trend_uses_the_same_five_day_gate_as_sleep_facts():
    raw = RawDailyProfile(user_id="sparse-sleep-week", day=TARGET)
    raw.series["sleep_duration"] = [
        SeriesPoint(
            metric="sleep_duration",
            value=440 if offset < 4 else 390,
            unit="min",
            day=TARGET - timedelta(days=offset),
            observed_at=TARGET - timedelta(days=offset),
            source="zepp",
            source_scope="normalized_daily_record",
        )
        for offset in [0, 1, 2, 3, 7, 8, 9, 10, 11, 12, 13]
    ]
    for offset in [*range(4), *range(7, 14)]:
        day = TARGET - timedelta(days=offset)
        raw.sleep_by_day[day] = {
            "date": day,
            "sleep_duration": 440 if offset < 4 else 390,
        }

    profile = WeeklyProfileEngine().build(
        "sparse-sleep-run", raw, [], [], period_mode="rolling"
    )

    assert profile.facts.sleep.available_days == 4
    assert profile.facts.sleep.change_percent is None
    assert not any("睡眠时长" in item for item in profile.inferences.key_changes)
