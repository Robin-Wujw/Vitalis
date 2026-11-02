"""Synthetic device/account adversaries for deterministic analysis source selection."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from vitalis.intelligence.analyzers import HrvAnalyzer, RecoveryAnalyzer
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import (
    Availability, HrvFeatures, SleepFeatures, SleepState, TrainingFeatures,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.readiness import (
    apply_readiness, body_signal_inputs, readiness_score, wake_charge_daily,
    wake_charge_summary,
)

DAY = date(2026, 10, 13)


def _points(metric, values, *, scope="device", device="A", unit="score", timed=False):
    return [
        SeriesPoint(
            metric=metric, value=float(value), unit=unit,
            day=DAY - timedelta(days=offset),
            observed_at=(
                datetime.combine(DAY - timedelta(days=offset), time(7), timezone.utc)
                if timed else DAY - timedelta(days=offset)
            ),
            source="zepp", source_scope=scope, device_id=device,
        )
        for offset, value in enumerate(values)
    ]


def _raw(**series):
    return RawDailyProfile(user_id="source-isolation-synthetic", day=DAY, timezone_name="UTC", series=series)


def _hrv(raw):
    return HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))


def _recovery(raw):
    return RecoveryAnalyzer().analyze(
        raw, SleepFeatures(status=Availability.INSUFFICIENT_DATA), SleepState.INSUFFICIENT_DATA,
        HrvFeatures(status=Availability.INSUFFICIENT_DATA),
        TrainingFeatures(status=Availability.INSUFFICIENT_DATA),
    )


@pytest.mark.parametrize("scope", ["user_summary", "user_fused"])
def test_explicit_account_hrv_is_third_value_not_device_average_or_proven_fusion(scope):
    raw = _raw(sleep_hrv=(
        _points("sleep_hrv", [30] + [50] * 21, unit="ms")
        + _points("sleep_hrv", [90] + [60] * 21, device="B", unit="ms")
        + _points("sleep_hrv", [47] + [55] * 21, scope=scope, device=None, unit="ms")
    ))
    result = _hrv(raw)
    assert result.value_ms == 47
    assert result.preferred_source == "zepp" and result.preferred_source_scope == scope
    assert result.preferred_device_id is None
    assert result.fusion_method == "vendor_account_summary_with_device_audit"
    assert result.corroboration_status == "conflicting" and not result.corroboration_affects_decision
    assert {(stream.source_scope, stream.device_id, stream.value_ms) for stream in result.streams} == {
        ("device", "A", 30), ("device", "B", 90), (scope, None, 47),
    }
    assert result.sleep_hrv_daily_trend.today_value_ms == 47


def test_unknown_without_device_does_not_get_account_priority_or_account_label():
    raw = _raw(sleep_hrv=(
        _points("sleep_hrv", [80] + [50] * 21, unit="ms")
        + _points("sleep_hrv", [35] + [55] * 28, scope="unknown", device=None, unit="ms")
    ))
    result = _hrv(raw)
    assert result.preferred_device_id == "A" and result.value_ms == 80
    unknown = next(stream for stream in result.streams if stream.device_id is None)
    assert unknown.device_label != "Zepp 账户级汇总"
    assert result.fusion_method == "canonical_device_with_corroboration"
    assert result.sleep_hrv_daily_trend.device_id == "A"


def test_hrv_device_opposites_stay_separate_and_different_metric_is_not_corroboration():
    raw = _raw(
        sleep_hrv=(
            _points("sleep_hrv", [30] + [50] * 28, unit="ms")
            + _points("sleep_hrv", [90] + [60] * 21, device="B", unit="ms")
        ),
        hrv_rmssd=_points("hrv_rmssd", [100] + [50] * 28, scope="user_summary", device=None, unit="ms"),
    )
    result = _hrv(raw)
    assert result.value_ms == 30 and result.preferred_device_id == "A"
    assert {stream.value_ms for stream in result.streams} == {30, 90}
    assert result.corroborating_stream_count == 1
    assert result.corroboration_affects_decision and result.fusion_direction == "unknown"


@pytest.mark.parametrize("scope", ["user_summary", "user_fused"])
def test_recovery_keeps_account_values_and_per_metric_provenance(scope):
    raw = _raw(**{
        metric: (
            _points(metric, [20, 21, 22])
            + _points(metric, [80, 81], device="B")
            + _points(metric, [73], scope=scope, device=None)
        )
        for metric in ("readiness", "hybrid_charge", "physical_readiness", "mental_charge")
    })
    result = _recovery(raw)
    assert result.vendor_readiness == 73 and result.vendor_charge == 73
    assert result.vendor_readiness_components == {"身体": 73}
    assert result.vendor_charge_components == {"心理": 73}
    for metric in raw.series:
        provenance = result.vendor_provenance[metric]
        assert provenance["source_scope"] == scope and provenance["device_id"] is None
        assert provenance["selection_reason"] == "explicit_account_summary"
        assert provenance["candidate_count"] == 3


def test_recovery_uses_best_covered_device_not_cross_device_median():
    raw = _raw(readiness=(
        _points("readiness", [20, 21, 22], device="A")
        + _points("readiness", [80, 81], device="Z")
        + _points("readiness", [99] * 28, device=None, scope="unknown")
    ))
    result = _recovery(raw)
    assert result.vendor_readiness == 20
    assert result.vendor_provenance["readiness"]["device_id"] == "A"


def test_wake_charge_selects_one_stream_for_entire_baseline_not_daily_winners():
    a = _points("hybrid_charge", [40] + [30, 70] * 15, timed=True)
    b = _points("hybrid_charge", [99] + [95] * 20, device="B", timed=True)
    # A's most recent historic day is a preaggregated day, not a raw sample.
    a[1] = replace(a[1], value=10, maximum=30, observed_minutes=800, sample_count=800)
    raw = _raw(hybrid_charge=a + b)
    daily = wake_charge_daily(raw)
    assert daily[DAY] == 40 and daily[DAY - timedelta(days=1)] == 30
    result = wake_charge_summary(raw)
    assert result["wake_charge_reference"] == 50
    assert result["wake_charge_low"] == 30 and result["wake_charge_high"] == 70
    assert result["wake_charge_provenance"]["device_id"] == "A"
    component = readiness_score(raw)["components"]["charge"]
    assert component["value"] == 40 and component["provenance"]["device_id"] == "A"


def test_wake_charge_does_not_borrow_other_devices_history_for_new_device():
    old = _points("hybrid_charge", [80] * 31, device="old", timed=True)[1:]
    new = _points("hybrid_charge", [30], device="new", timed=True)
    result = wake_charge_summary(_raw(hybrid_charge=old + new))
    assert result["wake_charge"] == 30 and result["wake_charge_reference"] is None
    assert result["wake_charge_provenance"]["device_id"] == "new"


def test_wake_charge_account_daily_summary_is_not_a_wake_measurement():
    raw = _raw(hybrid_charge=(
        _points("hybrid_charge", [45] + [50, 60] * 15, timed=True)
        + _points("hybrid_charge", [99] * 31, scope="user_summary", device=None)
    ))
    result = wake_charge_summary(raw)
    assert result["wake_charge"] == 45
    assert result["wake_charge_provenance"]["source_scope"] == "device"


def test_readiness_prefers_explicit_account_same_metric_and_retains_provenance():
    account = _points("sleep_hrv", [40] * 7 + [48, 52] * 30,
                      scope="user_summary", device=None, unit="ms")
    devices = (
        _points("sleep_hrv", [80] * 7 + [48, 52] * 30, device="A", unit="ms")
        + _points("sleep_hrv", [90] * 7 + [48, 52] * 30, device="Z", unit="ms")
    )
    expected = readiness_score(_raw(sleep_hrv=account))
    combined = readiness_score(_raw(sleep_hrv=devices + account))
    assert combined["score"] == expected["score"]
    assert combined["components"]["hrv"]["provenance"]["source_scope"] == "user_summary"
    recovery = apply_readiness(_recovery(_raw()), combined)
    assert recovery.readiness_component_provenance["hrv"]["source_scope"] == "user_summary"


def test_readiness_never_selects_by_device_id_on_equal_coverage():
    a = _points("sleep_rhr", [42] + [50, 54] * 14, device="A", unit="bpm")
    z = _points("sleep_rhr", [72] + [50, 54] * 14, device="Z", unit="bpm")
    for points in (a + z, z + a, [replace(point, device_id="Z" if point.device_id == "A" else "A") for point in a + z]):
        component = readiness_score(_raw(sleep_rhr=points))["components"]["rhr"]
        assert component["score"] is None
        assert component["provenance"]["selection_reason"] == "ambiguous_equal_coverage"
        assert component["provenance"]["device_id"] is None


def test_readiness_known_device_beats_long_unknown_history_and_keeps_own_baseline():
    device = _points("sleep_rhr", [60] + [50, 54] * 10, device="A", unit="bpm")
    unknown = _points("sleep_rhr", [40] + [80, 84] * 14, scope="unknown", device=None, unit="bpm")
    expected = readiness_score(_raw(sleep_rhr=device))["components"]["rhr"]
    selected = readiness_score(_raw(sleep_rhr=unknown + device))["components"]["rhr"]
    assert selected["score"] == expected["score"]
    assert selected["provenance"]["device_id"] == "A"
    assert selected["provenance"]["distinct_days"] == 21


def test_body_signals_compare_same_account_stream_not_opposite_device_histories():
    account = _points("sleep_rhr", [65, 65] + [50, 54] * 14, scope="user_summary", device=None, unit="bpm")
    device = _points("sleep_rhr", [40, 40] + [70, 74] * 14, device="Z", unit="bpm")
    result = body_signal_inputs(_raw(sleep_rhr=account + device))["resting_hr"]
    assert result["direction"] == "above" and result["baseline"] == 52
    assert result["provenance"]["source_scope"] == "user_summary"


@pytest.mark.parametrize("scope", ["user_summary", "user_fused"])
def test_account_scope_with_contributing_device_metadata_remains_account(scope):
    raw = _raw(sleep_hrv=(
        _points("sleep_hrv", [30] + [50] * 21, unit="ms")
        + _points("sleep_hrv", [47] + [55] * 21, scope=scope, device="B", unit="ms")
    ))
    result = _hrv(raw)
    assert result.value_ms == 47 and result.preferred_source_scope == scope
    assert result.preferred_device_id == "B"  # Original metadata is not erased.
    assert result.preferred_device_label == "Zepp 账户级汇总"
    assert result.fusion_method == "vendor_account_summary_with_device_audit"


def test_wake_selection_ignores_outside_window_and_future_coverage():
    a = _points("hybrid_charge", [45] + [50, 60] * 20, timed=True)
    b = _points("hybrid_charge", [99] * 100, device="B", timed=True)
    b = [point for point in b if point.day == DAY or point.day < DAY - timedelta(days=60)]
    b += [replace(b[0], day=DAY + timedelta(days=offset)) for offset in range(1, 20)]
    result = wake_charge_summary(_raw(hybrid_charge=a + b))
    assert result["wake_charge"] == 45
    assert result["wake_charge_provenance"]["distinct_days"] == 41


def test_wake_account_daily_summary_with_timestamp_still_is_not_a_reading():
    a = _points("hybrid_charge", [45, 50], timed=True)
    summary = [replace(point, is_daily_summary=True) for point in
               _points("hybrid_charge", [99] * 30, scope="user_summary", device=None, timed=True)]
    assert wake_charge_summary(_raw(hybrid_charge=a + summary))["wake_charge"] == 45


def test_equal_coverage_abstains_for_recovery_and_wake_without_averaging():
    for points in (
        _points("hybrid_charge", [30, 40], timed=True) + _points("hybrid_charge", [80, 90], device="Z", timed=True),
        _points("hybrid_charge", [80, 90], device="Z", timed=True) + _points("hybrid_charge", [30, 40], timed=True),
    ):
        result = _recovery(_raw(hybrid_charge=points))
        assert result.vendor_charge is None and result.wake_charge is None
        assert result.vendor_provenance["hybrid_charge"]["selection_reason"] == "ambiguous_equal_coverage"
        assert result.wake_charge_provenance["selection_reason"] == "ambiguous_equal_coverage"


def test_hrv_rhr_unknown_stream_never_beats_identified_stream_with_same_metric():
    raw = _raw(
        sleep_hrv=_points("sleep_hrv", [47] + [50] * 21, scope="user_summary", device=None, unit="ms"),
        sleep_rhr=(
            _points("sleep_rhr", [90] + [50] * 28, scope="unknown", device=None, unit="bpm")
            + _points("sleep_rhr", [52] + [50] * 21, device="A", unit="bpm")
        ),
    )
    result = _hrv(raw)
    assert result.rhr_device_id == "A" and result.rhr_bpm == 52
