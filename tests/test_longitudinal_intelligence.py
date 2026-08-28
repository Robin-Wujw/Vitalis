from datetime import date, timedelta

from vitalis.intelligence.association import PersonalAssociationEngine
from vitalis.intelligence.contracts import Availability, ConfidenceBand
from vitalis.intelligence.monthly import MonthlyProfileEngine
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.trend import TrendEngine


TARGET = date(2026, 8, 28)


def _point(metric, day, value, unit, *, device=None):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=day,
        source="zepp",
        source_scope="device" if device else "normalized_daily_record",
        device_id=device,
    )


def _raw_history(days=120):
    raw = RawDailyProfile(user_id="longitudinal-user", day=TARGET)
    raw.series = {
        "sleep_duration": [],
        "hrv_rmssd": [],
        "resting_hr": [],
        "training_load": [],
        "steps": [],
    }
    raw.sleep_by_day = {}
    raw.activity_by_day = {}
    raw.training_by_day = {}
    raw.workouts = []
    sleep_by_day = {}
    for offset in range(days - 1, -1, -1):
        day = TARGET - timedelta(days=offset)
        cycle = offset % 11
        sleep = 390 + cycle * 8 + (20 if offset < 28 else 0)
        sleep_by_day[day] = sleep
        load = 15 + (offset % 7) * 6
        steps = 5000 + cycle * 350
        raw.sleep_by_day[day] = {
            "date": day,
            "sleep_duration": sleep,
            "bedtime": f"23:{cycle * 3:02d}",
        }
        raw.activity_by_day[day] = {
            "date": day,
            "steps": steps,
            "active_minutes": 35,
        }
        raw.training_by_day[day] = {
            "date": day,
            "total_load": load,
            "total_duration": 30 if offset % 3 == 0 else 0,
            "workout_count": 1 if offset < 28 and offset % 4 == 0 else 0,
        }
        raw.series["sleep_duration"].append(_point("sleep_duration", day, sleep, "min"))
        raw.series["training_load"].append(_point("training_load", day, load, "load"))
        raw.series["steps"].append(_point("steps", day, steps, "steps"))
        raw.series["resting_hr"].append(_point("resting_hr", day, 70 - cycle, "bpm"))
        previous = day - timedelta(days=1)
        if previous in sleep_by_day:
            previous_sleep = sleep_by_day[previous]
            raw.series["hrv_rmssd"].append(
                _point("hrv_rmssd", day, previous_sleep / 8, "ms", device="helio")
            )
            raw.series["hrv_rmssd"].append(
                _point("hrv_rmssd", day, 95 - previous_sleep / 10, "ms", device="balance2")
            )
    for offset in range(0, 28, 4):
        day = TARGET - timedelta(days=offset)
        family = "strength" if offset % 8 == 0 else "aerobic"
        raw.workouts.append({
            "workout_id": f"workout-{offset}",
            "local_day": day,
            "data": {
                "sport_mode_label": "力量训练" if family == "strength" else "户外跑",
                "training_family": family,
                "duration": 45,
            },
        })
    return raw


def test_association_engine_uses_lagged_pairs_and_never_merges_devices():
    profile = PersonalAssociationEngine().build("association-run", _raw_history())
    candidates = [
        item for item in profile.associations
        if item.predictor_metric == "sleep_duration"
        and item.outcome_metric == "hrv_rmssd"
        and item.window_days == 90
    ]

    assert {item.outcome_device_id for item in candidates} == {"helio", "balance2"}
    helio = next(item for item in candidates if item.outcome_device_id == "helio")
    balance = next(item for item in candidates if item.outcome_device_id == "balance2")
    assert helio.status == Availability.AVAILABLE
    assert helio.lag_days == 1
    assert helio.paired_days == 89
    assert helio.coefficient == 1
    assert helio.direction == "POSITIVE"
    assert balance.coefficient == -1
    assert balance.direction == "NEGATIVE"
    assert all(item.association_only for item in candidates)


def test_association_engine_explicitly_reports_insufficient_variation():
    raw = RawDailyProfile(user_id="constant-user", day=TARGET)
    raw.series = {
        "sleep_duration": [
            _point("sleep_duration", TARGET - timedelta(days=offset), 420, "min")
            for offset in range(90)
        ],
        "hrv_rmssd": [
            _point("hrv_rmssd", TARGET - timedelta(days=offset), 50, "ms", device="helio")
            for offset in range(90)
        ],
    }

    profile = PersonalAssociationEngine().build("constant-run", raw)
    candidate = next(
        item for item in profile.associations
        if item.predictor_metric == "sleep_duration"
        and item.outcome_metric == "hrv_rmssd"
        and item.window_days == 90
    )
    assert candidate.status == Availability.INSUFFICIENT_DATA
    assert candidate.coefficient is None
    assert candidate.confidence == ConfidenceBand.NONE
    assert any("有效变化不足" in item for item in candidate.limitations)


def test_monthly_profile_recomputes_28_day_facts_from_normalized_history():
    raw = _raw_history()
    associations = PersonalAssociationEngine().build("monthly-run", raw).associations
    profile = MonthlyProfileEngine().build(
        "monthly-run",
        raw,
        TrendEngine().calculate(raw),
        [],
        associations,
    )

    assert profile.period_start == TARGET - timedelta(days=27)
    assert profile.period_end == TARGET
    assert profile.facts.sleep.available_days == 28
    assert profile.facts.sleep.change_percent is not None
    assert profile.facts.training.workout_count == 7
    assert profile.facts.training.sport_mode_counts == {"力量训练": 4, "户外跑": 3}
    assert profile.facts.activity.available_days == 28
    assert {item.device_id for item in profile.facts.recovery.streams if item.metric == "hrv_rmssd"} == {
        "helio", "balance2"
    }
    assert profile.data_quality.status.value == "SUFFICIENT"
    assert profile.inferences.personal_associations


def test_monthly_profile_does_not_turn_missing_training_records_into_zero():
    raw = RawDailyProfile(user_id="missing-month-user", day=TARGET)
    profile = MonthlyProfileEngine().build("missing-run", raw, [], [], [])

    assert profile.facts.training.record_days == 0
    assert profile.facts.training.workout_count is None
    assert profile.facts.training.duration_minutes is None
    assert profile.facts.training.aerobic_minutes is None
    assert profile.actions.recommendations[0].code == "MONTHLY_INSUFFICIENT_DATA"
