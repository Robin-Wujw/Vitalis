"""Synthetic regression fixtures for provenance-qualified signal facts."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from math import log
from types import SimpleNamespace

import pytest

from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.profile import ProfileLoader, RawDailyProfile, SeriesPoint
from vitalis.intelligence.signals import (
    SIGNAL_REGISTRY,
    PeriodSignalState,
    SignalState,
    SignalStatus,
    build_period_signals,
    build_signal_states,
)


DAY = date(2026, 10, 8)
AS_OF = datetime(2026, 10, 9, 4, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def _db():
    # These detached-profile tests do not need the repository-wide database.
    yield


def _raw(day=DAY, *, as_of=AS_OF, zone="Asia/Shanghai", series=None):
    return RawDailyProfile(
        user_id="synthetic-signals",
        day=day,
        as_of=as_of,
        timezone_name=zone,
        series=series or {},
    )


def _point(day, value, metric="steps", unit="steps", *, source="zepp",
           scope="device", device="watch-a", observed_at=None, source_field=None):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=observed_at or day,
        source=source,
        source_scope=scope,
        device_id=device,
        source_field=source_field or f"daily.{metric}",
    )


def _states(raw):
    return build_signal_states(raw, BaselineEngine().build(raw.series, raw.day))


def test_registry_covers_existing_parser_metrics_and_declares_policy():
    required = {
        "sleep_duration", "deep_sleep", "rem_sleep", "light_sleep", "awake",
        "sleep_score", "sleep_wake_count", "hrv_rmssd", "hrv_sdnn", "sleep_hrv",
        "resting_hr", "sleep_rhr", "respiratory_rate", "respiratory_rate_min",
        "respiratory_rate_max", "skin_temp_delta", "skin_temp_baseline_delta",
        "spo2", "spo2_apnea_low", "spo2_odi", "spo2_odi_events",
        "spo2_night_score", "spo2_measured_minutes", "steps", "distance_km",
        "active_minutes", "calories", "running_distance", "cycling_distance",
        "heart_rate", "stress", "stress_min", "stress_max", "stress_high_pct",
        "stress_medium_pct", "stress_normal_pct", "stress_relaxed_pct",
        "training_load", "training_duration", "vo2max", "pai_daily",
        "pai_low_zone", "pai_medium_zone", "pai_high_zone",
        "lactate_threshold_hr", "lactate_threshold_pace", "readiness",
        "physical_readiness", "mental_readiness", "hrv_readiness",
        "rhr_readiness", "skin_temp_readiness", "ahi_readiness", "afib_readiness",
        "bio_charge", "hybrid_charge", "physical_charge", "mental_charge",
        "hrv_baseline", "rhr_baseline", "device_max_hr", "device_resting_hr",
    }
    assert required <= set(SIGNAL_REGISTRY)
    for metric, entry in SIGNAL_REGISTRY.items():
        assert entry.normalized_metric == metric
        assert entry.source_fields and entry.unit
        assert entry.calendar_semantics in {"sleep_day", "activity_day", "calendar_day"}
        assert entry.qualification
        assert entry.coverage_rule.baseline_window_days == 28
        assert entry.coverage_rule.baseline_minimum_days == 14
        assert entry.visible_channels
        assert entry.decision_role in {"yes", "no", "shadow"}
        assert entry.missing_late_policy
    for metric in ("readiness", "bio_charge", "hybrid_charge"):
        assert SIGNAL_REGISTRY[metric].decision_role == "no"
    assert "training_status" not in SIGNAL_REGISTRY


def test_registry_includes_all_normalized_workout_detail_metrics():
    # Parser output is split between daily/profile streams and workout detail;
    # both must have an explicit qualification policy before public exposure.
    required = {
        "speed", "equivalent_pace", "cadence", "stride_length", "distance",
        "altitude", "running_power", "ground_contact_time",
        "vertical_oscillation", "vertical_stride_ratio",
    }
    assert required <= set(SIGNAL_REGISTRY)
    for metric in required:
        definition = SIGNAL_REGISTRY[metric]
        assert definition.source_fields
        assert definition.unit
        assert "evening" in definition.visible_channels
        assert definition.decision_role in {"no", "shadow"}


def test_every_registered_signal_has_an_unknown_state_without_invented_values():
    raw = _raw()
    states = _states(raw)
    assert set(states) == set(SIGNAL_REGISTRY)
    for metric, items in states.items():
        assert len(items) == 1
        state = items[0]
        assert isinstance(state, SignalState)
        assert state.metric == metric
        assert state.status == SignalStatus.UNKNOWN
        assert state.value is None and state.baseline is None
        assert state.observed_at is None and state.fetched_at is None
        assert state.source is None and state.device_id is None
        assert state.coverage_ratio == 0 and state.distinct_days == 0
        assert state.target_day_coverage.coverage_ratio == 0
        assert state.target_day_coverage.status == SignalStatus.UNKNOWN
        assert state.as_of == AS_OF


def test_current_fact_survives_baseline_warmup_and_serializes_date_precision():
    raw = _raw(series={"steps": [_point(DAY, 0)]})
    state = _states(raw)["steps"][0]
    assert state.status == SignalStatus.AVAILABLE
    assert state.value == 0
    assert state.baseline is None and state.deviation is None
    assert state.comparison_status == SignalStatus.INSUFFICIENT
    assert state.sample_count == 1 and state.distinct_days == 1
    assert state.expected_days == 28 and state.coverage_ratio == pytest.approx(1 / 28)
    assert state.target_day_coverage.coverage_ratio == 1
    assert state.source == "zepp" and state.device_id == "watch-a"
    assert state.source_fields == ["daily.steps"]
    assert state.fetched_at is None
    encoded = state.model_dump(mode="json")
    assert encoded["observed_at"] == DAY.isoformat()
    assert encoded["fetched_at"] is None
    assert json.loads(state.model_dump_json())["value"] == 0
    assert SignalState.model_validate(encoded).observed_at == DAY


def test_daily_baseline_isolated_by_source_scope_device_and_unit():
    points = []
    for offset in range(29):
        day = DAY - timedelta(days=offset)
        points.extend([
            _point(day, 100 + offset, source="zepp", scope="device", device="watch-a"),
            _point(day, 500 + offset, source="other", scope="device", device="watch-a"),
            _point(day, 1000 + offset, source="zepp", scope="device", device="watch-b"),
            _point(day, 2000 + offset, source="zepp", scope="user_fused", device=None),
            _point(day, 99, unit="km", source="zepp", scope="device", device="watch-a"),
        ])
    states = _states(_raw(series={"steps": points}))["steps"]
    assert len(states) == 5
    valid = [state for state in states if state.unit == "steps"]
    assert {state.baseline for state in valid} == {114.5, 514.5, 1014.5, 2014.5}
    invalid = next(state for state in states if state.unit == "km")
    assert invalid.status == SignalStatus.INSUFFICIENT
    assert invalid.value is None and invalid.baseline is None
    for state in valid:
        assert state.distinct_days == 28
        assert state.baseline_stats.distinct_days == 28
        assert state.baseline_stats.source == state.source
        assert state.baseline_stats.source_scope == state.source_scope
        assert state.baseline_stats.device_id == state.device_id
        assert state.baseline_stats.unit == state.unit


def test_rmssd_baseline_retains_log_transform_and_robust_spread_unit():
    values = [45, 46, 47, 48, 49, 50, 51] * 4
    points = [_point(DAY, 40, "hrv_rmssd", "ms")]
    points += [
        _point(DAY - timedelta(days=offset), value, "hrv_rmssd", "ms")
        for offset, value in enumerate(values, 1)
    ]
    state = _states(_raw(series={"hrv_rmssd": points}))["hrv_rmssd"][0]
    assert state.baseline == pytest.approx(48, abs=0.001)
    assert state.baseline_stats.transform == "natural_log"
    assert state.robust_spread > 0 and state.robust_spread_unit == "ln(ms)"
    assert state.baseline_stats.median == pytest.approx(log(48), abs=1e-6)
    assert state.deviation < 0 and state.deviation_percent < 0
    assert state.robust_z < 0 and state.direction == "below"


def test_repeated_samples_do_not_satisfy_fourteen_distinct_days():
    raw = _raw(series={"hrv_rmssd": [
        _point(DAY - timedelta(days=1), 50 + index % 3, "hrv_rmssd", "ms")
        for index in range(500)
    ] + [_point(DAY, 40, "hrv_rmssd", "ms")]})
    state = _states(raw)["hrv_rmssd"][0]
    assert state.status == SignalStatus.AVAILABLE
    assert state.value == 40 and state.baseline is None
    assert state.baseline_stats.sample_count == 500
    assert state.baseline_stats.distinct_days == 1
    assert state.comparison_status == SignalStatus.INSUFFICIENT


def test_cutoff_requalifies_baseline_and_rejects_future_and_mismatched_dates():
    cutoff = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
    points = [_point(DAY, 12, observed_at=cutoff)]
    points.extend([
        _point(DAY, 1000, observed_at=cutoff + timedelta(seconds=1)),
        _point(DAY + timedelta(days=1), 2000),
        _point(DAY, 3000, observed_at=cutoff - timedelta(days=2)),
    ])
    points += [
        _point(DAY - timedelta(days=offset), 100, observed_at=cutoff + timedelta(days=1))
        for offset in range(1, 20)
    ]
    raw = _raw(as_of=cutoff, series={"steps": points})
    state = _states(raw)["steps"][0]
    assert state.value == 12 and state.observed_at == cutoff
    assert state.status == SignalStatus.PARTIAL
    assert state.baseline is None
    assert state.distinct_days == 1
    assert state.target_day_coverage.sample_count == 1


def test_timezone_cutoff_uses_local_day_and_real_day_length_at_dst():
    target = date(2026, 11, 1)
    cutoff = datetime(2026, 11, 2, 4, tzinfo=timezone.utc)
    raw = _raw(target, as_of=cutoff, zone="America/New_York", series={
        "steps": [_point(target, 1500)],
    })
    state = _states(raw)["steps"][0]
    assert state.status == SignalStatus.PARTIAL
    assert state.target_day_coverage.expected_minutes == 25 * 60
    later = deepcopy(raw)
    later.as_of = cutoff + timedelta(hours=1)
    assert _states(later)["steps"][0].status == SignalStatus.AVAILABLE


def test_stale_does_not_forward_fill_and_bad_current_data_is_insufficient():
    old = _point(DAY - timedelta(days=2), 83, "readiness", "score")
    raw = _raw(series={"readiness": [old]})
    state = _states(raw)["readiness"][0]
    assert state.status == SignalStatus.STALE
    assert state.value is None and state.last_observed_value == 83
    assert state.last_observed_day == DAY - timedelta(days=2)
    assert state.target_day_coverage.coverage_ratio == 0
    raw.series["readiness"].append(_point(DAY, 101, "readiness", "score"))
    assert _states(raw)["readiness"][0].status == SignalStatus.INSUFFICIENT


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, -1])
def test_invalid_values_are_never_facts_or_comparisons(value):
    state = _states(_raw(series={"steps": [_point(DAY, value)]}))["steps"][0]
    assert state.status == SignalStatus.INSUFFICIENT
    assert state.value is None and state.baseline is None
    assert state.sample_count == 0 and state.coverage_ratio == 0
    json.dumps(state.model_dump(mode="json"), allow_nan=False)


def test_zero_temperature_and_negative_delta_are_valid_measurements():
    raw = _raw(series={"skin_temp_delta": [
        _point(DAY, 0, "skin_temp_delta", "C"),
        _point(DAY - timedelta(days=1), -0.2, "skin_temp_delta", "C"),
    ]})
    state = _states(raw)["skin_temp_delta"][0]
    assert state.value == 0 and state.distinct_days == 2
    assert state.status == SignalStatus.AVAILABLE


def test_unknown_source_and_unattributed_device_are_not_qualified():
    raw = _raw(series={"steps": [
        _point(DAY, 1200, source=""),
        _point(DAY, 2300, device=None),
    ]})
    assert all(state.value is None for state in _states(raw)["steps"])


def test_unknown_training_day_is_not_a_zero_or_default_readiness():
    raw = _raw(series={"training_duration": [
        _point(DAY, 0, "training_duration", "min", source="canonical_workouts",
               scope="normalized_daily_record", device=None,
               source_field="training.total_duration"),
    ]})
    raw.training_by_day[DAY] = {"workout_count": 0, "total_duration": 0,
                               "training_status": "moderate"}
    state = _states(raw)["training_duration"][0]
    assert state.value is None and state.status == SignalStatus.INSUFFICIENT
    assert "training_status" not in _states(raw)
    raw.training_history_coverage = {"verified_days": [DAY.isoformat()], "truncated": False}
    assert _states(raw)["training_duration"][0].value == 0
    raw.training_history_coverage["truncated"] = True
    assert _states(raw)["training_duration"][0].value is None


def test_profile_loader_imports_nullable_sleep_stages_and_explicit_zero():
    raw = _raw()
    raw.sleep_by_day[DAY] = {
        "sleep_duration": 450, "deep_sleep": 90, "rem_sleep": 0,
        "light_sleep": 330, "awake": None, "wake_count": 0,
        "source": "synthetic-vendor", "source_scope": "device", "device_id": "watch-a",
    }
    ProfileLoader(None)._add_record_series(raw)
    assert raw.series["deep_sleep"][0].value == 90
    assert raw.series["rem_sleep"][0].value == 0
    assert raw.series["light_sleep"][0].value == 330
    assert "awake" not in raw.series
    point = raw.series["deep_sleep"][0]
    assert (point.source, point.source_scope, point.device_id, point.source_field) == (
        "synthetic-vendor", "device", "watch-a", "sleep.deep_sleep",
    )
    assert raw.series["sleep_wake_count"][0].value == 0


def test_profile_loader_preserves_existing_timestamped_readiness_and_charge_inputs():
    queried = []
    timestamp = datetime(2026, 10, 8, 0, tzinfo=timezone.utc)

    def samples(_user, metric, _start, _end, *, limit):
        queried.append(metric)
        if metric in {"readiness", "hybrid_charge", "skin_temp_delta", "stress"}:
            return [SimpleNamespace(metric=metric, value=0, unit="C" if metric == "skin_temp_delta" else "score",
                                    timestamp=timestamp, source="zepp", source_scope="device", device_id="watch-a")]
        return []

    raw = _raw()
    loader = ProfileLoader(SimpleNamespace(metric_samples=samples))
    loader._add_sample_metrics(raw, DAY)
    assert {"readiness", "hybrid_charge", "skin_temp_delta", "stress"} <= set(queried)
    for metric in ("readiness", "hybrid_charge", "skin_temp_delta", "stress"):
        point = raw.series[metric][0]
        assert point.observed_at == timestamp and point.value == 0
        assert point.source_field == f"sample.{metric}"


def test_period_reduces_daily_values_then_uses_equal_length_reference_window():
    start, end = DAY - timedelta(days=6), DAY
    points = [
        _point(start + timedelta(days=offset), 100 * (offset + 1))
        for offset in range(7)
    ]
    points += [_point(start, 100)] * 300
    points += [_point(start - timedelta(days=offset), 200) for offset in range(1, 8)]
    points += [_point(end + timedelta(days=1), 1_000_000)]
    raw = _raw(series={"steps": points})
    raw.report_context["open_health_period_summary"] = {"steps": 99_000_000}
    period = build_period_signals(raw, start, end)["steps"][0]
    assert isinstance(period, PeriodSignalState)
    assert period.expected_days == period.reference.expected_days == 7
    assert period.effective_days == period.distinct_days == 7
    assert period.coverage_ratio == 1 and period.status == SignalStatus.AVAILABLE
    assert period.median == 400 and period.average == 400 and period.value == 400
    assert period.aggregation == "mean_per_effective_day"
    assert period.reference.start == start - timedelta(days=7)
    assert period.reference.end == start - timedelta(days=1)
    assert period.reference.value == 200
    assert period.deviation == 200 and period.deviation_percent == 100
    assert period.reference.source == period.source == "zepp"
    assert len(period.daily_values) == 7
    assert period.fetched_at is None
    encoded = period.model_dump(mode="json")
    assert encoded["start"] == start.isoformat()
    assert PeriodSignalState.model_validate(encoded).end == end


def test_period_current_facts_remain_visible_without_comparable_reference():
    start = DAY - timedelta(days=6)
    raw = _raw(series={"vo2max": [_point(DAY, 48, "vo2max", "ml/kg/min")]})
    period = build_period_signals(raw, start, DAY)["vo2max"][0]
    assert period.status == SignalStatus.PARTIAL and period.value == 48
    assert period.effective_days == 1 and period.expected_days == 7
    assert period.comparison_status == SignalStatus.INSUFFICIENT
    assert period.reference.value is None
    assert period.deviation is None and period.direction == "unknown"


def test_period_never_merges_device_history_or_reference_from_other_source():
    start = DAY - timedelta(days=6)
    points = [_point(DAY - timedelta(days=i), 50, "sleep_hrv", "ms", device="watch-a")
              for i in range(7)]
    points += [_point(start - timedelta(days=i), 100, "sleep_hrv", "ms", device="watch-b")
               for i in range(1, 8)]
    raw = _raw(series={"sleep_hrv": points})
    states = build_period_signals(raw, start, DAY)["sleep_hrv"]
    a = next(state for state in states if state.device_id == "watch-a")
    b = next(state for state in states if state.device_id == "watch-b")
    assert a.value == 50 and a.reference.value is None and a.deviation is None
    assert b.value is None and b.status == SignalStatus.STALE
    assert b.reference.value == 100


def test_unfinished_activity_day_is_not_a_complete_period_day_or_comparison():
    cutoff = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    start = DAY - timedelta(days=6)
    points = [_point(DAY - timedelta(days=i), 7000) for i in range(14)]
    raw = _raw(as_of=cutoff, series={"steps": points})
    period = build_period_signals(raw, start, DAY)["steps"][0]
    assert period.distinct_days == 7 and period.effective_days == 6
    assert period.coverage_ratio == pytest.approx(6 / 7)
    assert period.status == SignalStatus.PARTIAL
    assert period.daily_values[-1].complete is False
    assert period.target_day_coverage.status == SignalStatus.PARTIAL
    assert period.value == 7000


def test_calendar_period_uses_explicit_unequal_reference_month():
    current_start, current_end = date(2026, 9, 1), date(2026, 9, 30)
    reference_start, reference_end = date(2026, 8, 1), date(2026, 8, 31)
    points = [
        _point(current_start + timedelta(days=index), 5000)
        for index in range((current_end - current_start).days + 1)
    ]
    points.extend(
        _point(reference_start + timedelta(days=index), 4500)
        for index in range((reference_end - reference_start).days + 1)
    )
    raw = _raw(day=date(2026, 10, 1), series={"steps": points})
    raw.report_context["reference_period"] = {
        "start": reference_start.isoformat(), "end": reference_end.isoformat(),
    }
    period = build_period_signals(raw, current_start, current_end)["steps"][0]
    assert period.expected_days == 30
    assert period.reference.expected_days == 31
    assert period.reference.start == reference_start
    assert period.reference.end == reference_end
    assert period.effective_days == 30 and period.reference.effective_days == 31
    assert period.comparison_status == SignalStatus.AVAILABLE


@pytest.mark.parametrize("days", [7, 28, 30, 31, 60, 90])
def test_period_denominators_and_reference_are_calendar_length_aware(days):
    start = DAY - timedelta(days=days - 1)
    points = [_point(DAY - timedelta(days=i), 5000) for i in range(days * 2)]
    period = build_period_signals(_raw(series={"steps": points}), start, DAY)["steps"][0]
    assert period.expected_days == period.reference.expected_days == days
    assert period.effective_days == period.reference.effective_days == days
    assert period.deviation == 0 and period.deviation_percent == 0


def test_period_rejects_inverted_dates_and_does_not_mutate_raw_or_baselines():
    raw = _raw(series={"steps": [_point(DAY, 4000)]})
    baselines = BaselineEngine().build(raw.series, raw.day)
    before_raw, before_baselines = deepcopy(raw), deepcopy(baselines)
    build_signal_states(raw, baselines)
    build_period_signals(raw, DAY - timedelta(days=6), DAY)
    assert raw == before_raw and baselines == before_baselines
    with pytest.raises(ValueError):
        build_period_signals(raw, DAY, DAY - timedelta(days=1))
