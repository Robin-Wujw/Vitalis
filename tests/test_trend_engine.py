from datetime import date, timedelta

from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import Availability, ConfidenceBand, TrendDirection
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.trend import TrendEngine


TARGET = date(2026, 8, 28)


def _points(metric, values, *, device=None, unit="ms"):
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


def test_trend_engine_compares_periods_and_reports_variability():
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.series = {
        "sleep_duration": _points(
            "sleep_duration",
            [400] * 7 + [450, 455, 460, 450, 455, 460, 465],
            unit="min",
        )
    }

    trend = next(
        item for item in TrendEngine().calculate(raw, windows=(7,))
        if item.metric == "sleep_duration"
    )

    assert trend.status == Availability.AVAILABLE
    assert trend.current_median == 455
    assert trend.previous_median == 400
    assert trend.change_percent == 13.8
    assert trend.direction == TrendDirection.RISING
    assert trend.variability_mad == 5
    assert trend.confidence == ConfidenceBand.HIGH


def test_trend_engine_keeps_hrv_devices_separate():
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.series = {
        "hrv_rmssd": (
            _points("hrv_rmssd", list(range(50, 64)), device="helio")
            + _points("hrv_rmssd", list(range(70, 84)), device="balance")
        )
    }

    trends = [
        item for item in TrendEngine().calculate(raw, windows=(7,))
        if item.metric == "hrv_rmssd"
    ]

    assert {item.device_id for item in trends} == {"helio", "balance"}
    assert {item.current_median for item in trends} == {60, 80}


def test_trend_engine_abstains_when_current_coverage_is_too_low():
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.series = {"resting_hr": _points("resting_hr", [55, 56, 57], unit="bpm")}

    trend = TrendEngine().calculate(raw, windows=(7,))[0]

    assert trend.status == Availability.INSUFFICIENT_DATA
    assert trend.direction == TrendDirection.INSUFFICIENT_DATA
    assert trend.confidence == ConfidenceBand.NONE


def test_ninety_day_trend_compares_against_preceding_ninety_days():
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.series = {
        "resting_hr": _points("resting_hr", [60] * 90 + [54] * 90, unit="bpm")
    }

    trend = TrendEngine().calculate(raw, windows=(90,))[0]

    assert trend.status == Availability.AVAILABLE
    assert trend.current_median == 54
    assert trend.previous_median == 60
    assert trend.change_percent == -10
    assert trend.direction == TrendDirection.FALLING


def test_incomplete_target_day_is_excluded_from_cumulative_activity_trends():
    raw = RawDailyProfile(user_id="incomplete-activity-trend", day=TARGET)
    raw.report_context = {"target_day_complete": False}
    raw.series = {
        "steps": _points("steps", [100] * 6 + [1000], unit="steps"),
        "active_minutes": _points("active_minutes", [10] * 6 + [100], unit="min"),
        "sleep_duration": _points("sleep_duration", [400] * 6 + [800], unit="min"),
    }

    trends = TrendEngine().calculate(raw, windows=(7,))
    by_metric = {item.metric: item for item in trends}

    assert by_metric["steps"].current_distinct_days == 6
    assert by_metric["active_minutes"].current_distinct_days == 6
    assert by_metric["sleep_duration"].current_distinct_days == 7


def test_zero_reference_with_nonzero_mad_keeps_percent_missing():
    raw = RawDailyProfile(user_id="zero-baseline", day=TARGET)
    raw.series = {
        "skin_temperature_delta": _points(
            "skin_temperature_delta", [-0.2, 0.0, 0.2, 0.3], unit="C"
        )
    }
    baseline = BaselineEngine().build(raw.series, TARGET)["skin_temperature_delta"][0]

    deviation = BaselineEngine.deviation(0.3, baseline)

    assert baseline.reference_value == 0
    assert baseline.mad > 0
    assert deviation.percent is None
    assert deviation.robust_z is not None
