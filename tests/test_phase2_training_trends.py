"""Synthetic period and workout trend checks; no personal data or network."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.domain import WorkoutMetricSample
from vitalis.intelligence.contracts import Availability, ConfidenceBand, TrendDirection
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.running import RunningAnalyzer
from vitalis.intelligence.strength import StrengthAnalyzer
from vitalis.intelligence.trend import TrendEngine


TARGET = date(2026, 9, 30)
AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _points(metric, values, unit, *, source="zepp", device=None):
    return [
        SeriesPoint(
            metric=metric, value=value, unit=unit,
            day=TARGET - timedelta(days=len(values) - index - 1),
            observed_at=TARGET - timedelta(days=len(values) - index - 1),
            source=source, source_scope="device" if device else "daily_metric",
            device_id=device,
        )
        for index, value in enumerate(values)
    ]


def _raw(workouts=()):
    return RawDailyProfile(
        user_id="synthetic-training-trend", day=TARGET, as_of=AS_OF, workouts=list(workouts),
    )


def _run(index, *, current=True, source="zepp", device="synthetic-watch"):
    day = TARGET - timedelta(days=index)
    started = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6)
    workout_id = f"synthetic-run-{index}"
    speed = 3.3 if current else 3.0
    heart_rate = 140 if current else 130
    samples = []
    for second in range(0, 1800, 30):
        samples.extend([
            WorkoutMetricSample(
                source=source, workout_id=workout_id, timestamp=started + timedelta(seconds=second),
                metric="speed", value=speed, unit="m/s", device_id=device,
            ),
            WorkoutMetricSample(
                source=source, workout_id=workout_id, timestamp=started + timedelta(seconds=second),
                metric="heart_rate", value=heart_rate * (1.1 if current and second >= 900 else 1),
                unit="bpm", device_id=device,
            ),
        ])
    return {
        "source": source, "workout_id": workout_id, "local_day": day,
        "started_at": started, "device_id": device, "samples": samples,
        "data": {
            "type": "running", "training_family": "aerobic", "duration": 30,
            "distance_km": speed * 1800 / 1000,
            "heart_rate_zone_boundaries_bpm": [100, 140, 160, 180, 200, 240],
        },
    }


def _strength(index, *, repetitions=(12, 10, 8), weight=10, unit="kg", basis="per_hand", source="strength_sets"):
    day = TARGET - timedelta(days=index)
    return {
        "source": "zepp", "workout_id": f"synthetic-strength-{index}", "local_day": day,
        "started_at": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=18),
        "confirmed_exercises": [], "samples": [],
        "detail": {"strength_sets": [
            {
                "source": source, "order": order, "exercise_id": "curl", "exercise_name": "弯举",
                "repetitions": reps, "weight_value": weight, "weight_unit": unit,
                "weight_basis": basis,
            }
            for order, reps in enumerate(repetitions, start=1)
        ]},
        "data": {"type": "strength", "training_family": "strength", "duration": 30},
    }


def test_default_trends_include_7_vs_previous_7_and_28_60_90_windows():
    raw = _raw()
    raw.series = {"resting_hr": _points("resting_hr", [60] * 90 + [54] * 90, "bpm")}

    trends = TrendEngine().calculate(raw)

    assert {item.window_days for item in trends} == {7, 28, 60, 90}
    long = next(item for item in trends if item.window_days == 90)
    assert long.period_start == TARGET - timedelta(days=89)
    assert long.period_end == TARGET
    assert long.reference_period_start == TARGET - timedelta(days=179)
    assert long.reference_period_end == TARGET - timedelta(days=90)
    assert long.expected_days == long.previous_expected_days == 90
    assert long.previous_coverage_ratio == 1
    assert long.comparison_available is True
    assert long.comparison_basis == "preceding_window"
    assert long.change_percent == -10
    assert long.as_of == AS_OF
    assert long.observed_at == TARGET


def test_previous_window_gate_does_not_replace_7_vs_7_with_an_internal_slope():
    raw = _raw()
    raw.series = {"resting_hr": _points("resting_hr", [60, 61, 62, 63, 64, 65, 66], "bpm")}

    trend = TrendEngine().calculate(raw, windows=(7,))[0]

    assert trend.current_median == 63
    assert trend.previous_distinct_days == 0
    assert trend.previous_median is trend.change_percent is None
    assert trend.comparison_available is False
    assert trend.direction == TrendDirection.INSUFFICIENT_DATA
    assert trend.confidence == ConfidenceBand.NONE
    assert trend.comparison_basis == "unavailable"


def test_parser_fitness_pai_and_threshold_series_stay_device_and_unit_isolated():
    raw = _raw()
    raw.series = {
        "vo2max": _points("vo2max", [40] * 90 + [44] * 90, "ml/kg/min"),
        "pai_daily": _points("pai_daily", [5] * 90 + [10] * 90, "pai"),
        "pai_low_zone": _points("pai_low_zone", [2] * 180, "pai"),
        "pai_medium_zone": _points("pai_medium_zone", [1] * 180, "pai"),
        "pai_high_zone": _points("pai_high_zone", [3] * 180, "pai"),
        "lactate_threshold_hr": _points("lactate_threshold_hr", [160] * 90 + [165] * 90, "bpm"),
        "lactate_threshold_pace": _points("lactate_threshold_pace", [360] * 90 + [330] * 90, "s/km"),
    }
    raw.series["vo2max"] += _points("vo2max", [70] * 180, "ml/kg/min", device="other-watch")
    raw.series["vo2max"] += _points("vo2max", [999] * 180, "unknown")

    trends = TrendEngine().calculate(raw, windows=(90,))
    by_metric = {item.metric for item in trends}

    assert by_metric >= set(raw.series)
    vo2 = [item for item in trends if item.metric == "vo2max"]
    assert len(vo2) == 2
    assert {item.current_median for item in vo2} == {44, 70}
    threshold = next(item for item in trends if item.metric == "lactate_threshold_pace")
    assert threshold.change_percent == -8.3
    assert threshold.direction == TrendDirection.FALLING
    assert threshold.unit == "s/km"
    assert all(item.source == "zepp" for item in trends)


def test_sparse_threshold_is_an_observation_without_a_supported_period_comparison():
    raw = _raw()
    raw.series = {"lactate_threshold_hr": _points("lactate_threshold_hr", [160], "bpm")}

    trend = TrendEngine().calculate(raw, windows=(28,))[0]

    assert trend.status == Availability.INSUFFICIENT_DATA
    assert trend.current_value == 160
    assert trend.current_distinct_days == 1
    assert trend.expected_days == 28
    assert trend.comparison_available is False
    assert trend.change_percent is None
    assert trend.direction == TrendDirection.INSUFFICIENT_DATA


def test_defaults_in_training_records_do_not_become_observed_vendor_status():
    raw = _raw()
    raw.training_by_day = {
        TARGET - timedelta(days=offset): {"training_status": "moderate"}
        for offset in range(180)
    }

    assert TrendEngine().calculate(raw) == []


def test_run_trends_report_pace_hr_and_drift_with_session_denominators():
    raw = _raw([_run(index, current=index < 7) for index in range(14)])

    trends = TrendEngine().calculate(raw, windows=(7,))
    by_metric = {item.metric: item for item in trends}

    assert {"running_pace", "running_heart_rate", "running_cardiac_drift"} <= by_metric.keys()
    pace = by_metric["running_pace"]
    assert pace.current_median == pytest.approx(303.03, abs=0.01)
    assert pace.previous_median == pytest.approx(333.33, abs=0.01)
    assert pace.change_percent == -9.1
    assert pace.current_sample_count == pace.current_expected_samples == 7
    assert pace.previous_sample_count == pace.previous_expected_samples == 7
    assert pace.sample_coverage_ratio == pace.previous_sample_coverage_ratio == 1
    assert pace.sample_unit == "session"
    assert pace.calendar_semantics == "activity_day"
    assert by_metric["running_heart_rate"].current_median == 147
    drift = by_metric["running_cardiac_drift"]
    assert drift.current_median == 10
    assert drift.previous_median == 0
    assert drift.change_percent is None
    assert drift.change_absolute == 10
    assert drift.direction == TrendDirection.RISING


def test_run_trends_do_not_pool_incompatible_sources_or_hide_missing_hr_details():
    workouts = [_run(index, current=index < 7) for index in range(14)]
    for workout in workouts[:6]:
        workout["samples"] = []
    raw = _raw(workouts)

    trends = TrendEngine().calculate(raw, windows=(7,))
    drift = next(item for item in trends if item.metric == "running_cardiac_drift")

    assert drift.current_expected_samples == 7
    assert drift.current_sample_count == 1
    assert drift.sample_coverage_ratio == pytest.approx(1 / 7, abs=0.0001)
    assert drift.status == Availability.INSUFFICIENT_DATA
    assert drift.change_percent is None


def test_running_heart_rate_trend_uses_qualified_summary_when_detail_is_absent():
    workouts = [_run(index, current=index < 7) for index in range(14)]
    for workout in workouts:
        workout["samples"] = []
        workout["data"]["heart_rate_avg"] = 142 if workout["local_day"] >= TARGET - timedelta(days=6) else 132
    trends = TrendEngine().calculate(_raw(workouts), windows=(7,))
    heart_rate = next(item for item in trends if item.metric == "running_heart_rate")

    assert heart_rate.current_median == 142
    assert heart_rate.previous_median == 132
    assert heart_rate.current_sample_count == heart_rate.current_expected_samples == 7
    assert heart_rate.status == Availability.AVAILABLE


def test_late_fetched_workouts_are_excluded_from_as_of_bounded_training_analyses():
    late = _run(0)
    late["data"]["fetched_at"] = (AS_OF + timedelta(seconds=1)).isoformat()
    raw = _raw([late])

    assert RunningAnalyzer().analyze(raw).status == Availability.INSUFFICIENT_DATA
    assert StrengthAnalyzer().analyze(raw).status == Availability.INSUFFICIENT_DATA


def test_cardiac_drift_requires_actual_common_timestamps_and_a_continuous_segment():
    workout = _run(0)
    for sample in workout["samples"]:
        if sample.metric == "heart_rate":
            sample.timestamp += timedelta(minutes=31)
    analysis = RunningAnalyzer().analyze(_raw([workout]))

    assert analysis.recent_sessions[0].cardiac_drift_percent is None
    assert any("重叠" in item for item in analysis.recent_sessions[0].limitations)

    sparse = _run(0)
    for sample in sparse["samples"]:
        elapsed = (sample.timestamp - sparse["started_at"]).total_seconds()
        sample.timestamp = sparse["started_at"] + timedelta(seconds=elapsed * 2)
    sparse_analysis = RunningAnalyzer().analyze(_raw([sparse]))
    assert sparse_analysis.recent_sessions[0].cardiac_drift_percent is None


def test_running_comparable_baseline_rejects_a_different_workout_source():
    current = _run(0)
    history = [_run(index, source="other") for index in (1, 2, 3)]

    session = RunningAnalyzer().analyze(_raw([*history, current])).recent_sessions[0]

    assert session.comparable_baseline is None


def test_running_threshold_does_not_cross_source_boundaries():
    workout = _run(0)
    raw = _raw([workout])
    raw.series["lactate_threshold_hr"] = [
        SeriesPoint(
            metric="lactate_threshold_hr", value=170, unit="bpm", day=TARGET,
            observed_at=TARGET, source="other", source_scope="daily_metric",
        )
    ]

    analysis = RunningAnalyzer().analyze(raw)

    assert analysis.lactate_threshold_bpm is None
    assert analysis.zone_method == "device_workout"
    assert analysis.recent_sessions[0].heart_rate_zone_source == "device_workout"


def test_running_samples_with_wrong_units_or_mixed_devices_do_not_produce_drift():
    workout = _run(0)
    for sample in workout["samples"]:
        if sample.metric == "speed":
            sample.unit = "km/h"
    assert RunningAnalyzer().analyze(_raw([workout])).recent_sessions[0].cardiac_drift_percent is None

    workout = _run(0)
    workout["samples"][1].device_id = "a-different-device"
    assert RunningAnalyzer().analyze(_raw([workout])).recent_sessions[0].cardiac_drift_percent is None


def test_strength_volume_keeps_each_set_dose_and_known_weight_basis():
    workouts = [
        _strength(index, repetitions=(13, 11, 9) if index < 7 else (12, 10, 8))
        for index in range(14)
    ]
    raw = _raw(workouts)

    trends = TrendEngine().calculate(raw, windows=(7,))
    by_metric = {item.metric: item for item in trends}

    assert {"strength_sets", "strength_repetitions", "strength_volume"} <= by_metric.keys()
    volume = by_metric["strength_volume"]
    assert volume.current_median == 330
    assert volume.previous_median == 300
    assert volume.unit == "kg*reps"
    assert volume.weight_basis == "per_hand"
    assert volume.exercise_id == "curl"
    assert volume.current_sample_count == volume.current_expected_samples == 7
    assert volume.change_percent == 10
    session = StrengthAnalyzer().analyze(raw).recent_sessions[0]
    assert session.comparisons[0].current_repetitions == [13, 11, 9]
    assert session.comparisons[0].previous_repetitions == [13, 11, 9]


@pytest.mark.parametrize("unit,basis,weight", [(None, "per_hand", 10), ("kg", None, 10), ("kg", "per_hand", None)])
def test_unknown_weight_qualification_abstains_from_capacity_comparisons(unit, basis, weight):
    workouts = [_strength(index, unit=unit, basis=basis, weight=weight) for index in range(14)]

    trends = TrendEngine().calculate(_raw(workouts), windows=(7,))
    volume = next(item for item in trends if item.metric == "strength_volume")

    assert volume.current_sample_count == 0
    assert volume.current_expected_samples == 7
    assert volume.current_median is volume.change_percent is None
    assert volume.status == Availability.INSUFFICIENT_DATA
    session = StrengthAnalyzer().analyze(_raw(workouts)).recent_sessions[0]
    assert session.comparisons[0].comparable is False


def test_strength_lap_records_preserve_display_semantics_and_do_not_become_known_doses():
    raw = _raw([_strength(index, source="lap_62") for index in range(14)])

    trends = TrendEngine().calculate(raw, windows=(7,))
    session = StrengthAnalyzer().analyze(raw).recent_sessions[0]

    assert not any(item.metric.startswith("strength_") for item in trends)
    assert session.explicit_exercises == []
    assert [item.repetitions for item in session.observed_sets] == [12, 10, 8]
    assert session.comparisons == []


def test_strength_capacity_does_not_compare_kg_lb_or_different_weight_bases():
    workouts = [
        _strength(index, unit="kg" if index < 7 else "lb", basis="per_hand")
        for index in range(14)
    ]
    volumes = [
        item for item in TrendEngine().calculate(_raw(workouts), windows=(7,))
        if item.metric == "strength_volume"
    ]

    assert {item.unit for item in volumes} == {"kg*reps", "lb*reps"}
    assert all(item.comparison_available is False for item in volumes)
    assert all(item.change_percent is None for item in volumes)
