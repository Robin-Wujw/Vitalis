from datetime import date, datetime, time, timedelta, timezone

from vitalis.intelligence.activity import (
    ActivityAnalyzer,
    workout_calories_kcal,
    workout_distance_km,
)
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import Availability
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


TARGET = date(2026, 8, 28)


def _point(metric, value, day, *, unit, scope="user_fused", device=None):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=day,
        source="zepp",
        source_scope=scope,
        device_id=device,
    )


def test_workout_helpers_require_evidence_for_zero_but_keep_positive_legacy_values():
    assert workout_calories_kcal({"calories": 0}) is None
    assert workout_calories_kcal({"calories": 0, "observed_fields": ["calories"]}) == 0
    assert workout_calories_kcal({"calories": 123}) == 123
    assert workout_distance_km({"distance_km": 0}) is None
    assert workout_distance_km({"distance_km": 0, "observed_fields": ["distance_km"]}) == 0
    assert workout_distance_km({"distance_km": 4.25}) == 4.25


def test_activity_uses_one_canonical_daily_energy_entry_and_keeps_workout_separate():
    raw = RawDailyProfile(user_id="u", day=TARGET, as_of=datetime(2026, 8, 29, tzinfo=timezone.utc))
    raw.series = {
        "calories": [
            _point("calories", 500, TARGET, unit="kcal", scope="user_fused"),
            _point("calories", 500, TARGET, unit="kcal", scope="normalized_daily_record"),
        ]
    }
    raw.workouts = [{
        "local_day": TARGET,
        "source": "zepp",
        "started_at": datetime(2026, 8, 28, 12, tzinfo=timezone.utc),
        "data": {"calories": 100, "observed_fields": ["calories"]},
    }]

    features = ActivityAnalyzer().analyze(raw, BaselineEngine().build(raw.series, TARGET))

    assert features.status == Availability.AVAILABLE
    assert [(item.role, item.value, item.source_field) for item in features.energy] == [
        ("unspecified", 500, "series.calories"),
        ("workout", 100, "workout.calories"),
    ]
    assert sum(item.value for item in features.energy if item.role == "unspecified") == 500


def test_activity_distance_baseline_is_same_stream_and_known_km_only():
    raw = RawDailyProfile(user_id="u", day=TARGET, as_of=datetime(2026, 8, 29, tzinfo=timezone.utc))
    raw.series = {
        "distance_km": [
            _point("distance_km", 2, TARGET - timedelta(days=offset), unit="km")
            for offset in range(1, 15)
        ] + [
            _point("distance_km", 4, TARGET, unit="km"),
            _point("distance_km", 4000, TARGET, unit="m"),
        ]
    }

    features = ActivityAnalyzer().analyze(raw, BaselineEngine().build(raw.series, TARGET))

    assert features.distance_km is not None
    assert features.distance_km.value == 4
    assert features.distance_km.unit == "km"
    assert features.distance_km.provenance.source_scope == "user_fused"
    assert features.distance_km.baseline_reference == 2
    assert features.distance_km.deviation is not None


def test_activity_does_not_compare_unfinished_cumulative_day_to_complete_days():
    raw = RawDailyProfile(
        user_id="u",
        day=TARGET,
        as_of=datetime.combine(TARGET, time(12, 0), tzinfo=timezone.utc),
    )
    raw.series = {
        "steps": [
            _point("steps", 10_000, TARGET - timedelta(days=offset), unit="steps")
            for offset in range(1, 15)
        ] + [_point("steps", 2_000, TARGET, unit="steps")]
    }

    features = ActivityAnalyzer().analyze(raw, BaselineEngine().build(raw.series, TARGET))

    assert features.steps is not None
    assert features.steps.value == 2_000
    assert features.steps.deviation is None
    assert features.steps.baseline_reference is None
    assert "target_day_incomplete_baseline_comparison_skipped" in features.limitations
