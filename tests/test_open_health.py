from datetime import date, timedelta
from math import isclose

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    OpenHealthBundle,
    OpenHealthInsights,
    OpenHealthStatus,
    ReadinessInsight,
    SleepInsight,
    UserProfile,
)
from vitalis.intelligence.open_health.projection import coverage_summary
from vitalis.intelligence.open_health import (
    EWMAObservation,
    OpenHealthEngine,
    WinsorizedEWMA,
    compute_anomaly,
    compute_ewma,
    compute_readiness,
    compute_sleep,
)


def d(day: int) -> date:
    return date(2026, 1, day)


def test_ewma_cold_provisional_trusted_and_golden_alpha_update():
    values = [EWMAObservation(d(day), 100.0) for day in range(1, 15)]
    results = compute_ewma(values, "rmssd")
    assert results[2].status == "COLD"
    assert results[3].status == "PROVISIONAL"
    assert results[13].status == "TRUSTED"
    alpha = 1 - 2.718281828459045 ** (-0.6931471805599453 / 14)
    target = WinsorizedEWMA("rmssd")
    for item in values[:4]:
        result = target.update(item.value, observation_date=item.date)
    assert isclose(result.center_after or 0, 100.0, abs_tol=1e-12)
    assert isclose(alpha, 0.048304, rel_tol=1e-4)


def test_ewma_winsor_hard_reject_and_stale():
    stream = WinsorizedEWMA("rmssd")
    for day in range(1, 9):
        stream.update(100.0, observation_date=d(day))
    winsor = stream.update(120.0, observation_date=d(9))
    assert winsor.winsorized is True
    assert isclose(winsor.value_used or 0, 115.0, abs_tol=1e-9)

    for day in range(10, 15):
        stream.update(100.0, observation_date=d(day))
    rejected = stream.update(140.0, observation_date=d(15))
    assert rejected.hard_rejected is True
    assert rejected.accepted is False
    stale = compute_ewma([EWMAObservation(d(day), 100.0) for day in range(1, 15)], "rmssd", as_of=d(30))[-1]
    assert stale.status == "STALE"
    assert stale.stale is True


def test_readiness_excludes_target_and_missing_rr_is_only_note():
    rows = [
        {"date": d(1), "rmssd_ms": 98, "rr_available": True},
        {"date": d(2), "rmssd_ms": 100, "rr_available": True},
        {"date": d(3), "rmssd_ms": 102, "rr_available": True},
        {"date": d(4), "rmssd_ms": 50, "rr_available": False},
    ]
    result = compute_readiness(rows, target_date=d(4))
    assert result.status == OpenHealthStatus.AVAILABLE
    assert isinstance(result.payload, ReadinessInsight)
    assert result.payload.state == "suppressed"
    expected_baseline = sum(__import__("math").log(value) for value in (98, 100, 102)) / 3
    assert isclose(result.payload.baseline_ln_rmssd or 0, expected_baseline, abs_tol=1e-12)
    assert "缺少 RR" in (result.note or "")

    low = compute_readiness(rows[:3], target_date=d(3))
    assert low.status == OpenHealthStatus.REFUSED
    assert low.refusal_reason.code == "INSUFFICIENT_READINESS_HISTORY"


def test_readiness_refuses_zero_dispersion_baseline():
    rows = [
        {"date": d(day), "rmssd_ms": 100}
        for day in range(1, 5)
    ]
    rows[-1]["rmssd_ms"] = 99.9

    result = compute_readiness(rows, target_date=d(4))

    assert result.status == OpenHealthStatus.REFUSED
    assert result.refusal_reason.code == "ZERO_READINESS_DISPERSION"


def anomaly_rows(current_values=((20, 70, 20), (20, 70, 20))):
    rows = []
    for day in range(1, 13):
        rows.append({
            "date": d(day),
            "rmssd_ms": 96 + (day % 5) * 2,
            "rhr_bpm": 48 + (day % 4),
            "respiratory_rate": 13 + (day % 3) * 0.4,
        })
    for offset, (rmssd, rhr, resp) in enumerate(current_values, start=13):
        rows.append({"date": d(offset), "rmssd_ms": rmssd, "rhr_bpm": rhr, "respiratory_rate": resp})
    return rows


def test_anomaly_minimum_current_and_two_day_golden_rule():
    one = compute_anomaly(anomaly_rows()[:11] + anomaly_rows()[-2:-1], target_date=d(13))
    assert one.status == OpenHealthStatus.REFUSED
    assert one.refusal_reason.code == "INSUFFICIENT_ANOMALY_PERSISTENCE"
    two = compute_anomaly(anomaly_rows(), target_date=d(14))
    assert two.status == OpenHealthStatus.AVAILABLE
    assert two.payload.flagged is True
    assert two.payload.streak_days == 2
    assert two.payload.diagnostic is False
    assert two.payload.threshold > 16.27


def test_anomaly_supports_two_dimensions_when_third_is_missing():
    rows = [
        {"date": d(day), "rmssd_ms": 96 + (day % 5) * 2, "rhr_bpm": 48 + (day % 4)}
        for day in range(1, 13)
    ]
    rows.extend([
        {"date": d(13), "rmssd_ms": 20, "rhr_bpm": 70},
        {"date": d(14), "rmssd_ms": 20, "rhr_bpm": 70},
    ])

    result = compute_anomaly(rows, target_date=d(14))

    assert result.status == OpenHealthStatus.AVAILABLE
    assert result.payload.dimensions == ["ln_rmssd", "rhr"]
    assert result.payload.flagged is True
    assert result.payload.threshold > 13.82


def test_anomaly_gap_resets_continuity_state():
    rows = anomaly_rows()[:-1]
    rows.pop(12)
    rows.extend([
        {"date": d(14), "rmssd_ms": 20, "rhr_bpm": 70, "respiratory_rate": 20},
        {"date": d(15), "rmssd_ms": 20, "rhr_bpm": 70, "respiratory_rate": 20},
    ])
    result = compute_anomaly(rows, target_date=d(15))
    assert result.payload.gap_reset is True
    assert result.payload.flagged is True


def test_period_coverage_uses_anomaly_baseline_nights_and_sleep_nights():
    anomaly = compute_anomaly(anomaly_rows(), target_date=d(14))
    sleep = compute_sleep(sleep_rows(14), target_date=d(14))
    bundle = OpenHealthBundle(
        target_date=d(14),
        anomaly=anomaly,
        sleep=sleep,
    )

    coverage = coverage_summary(bundle, period_days=28)

    assert coverage["anomaly"].observed_days >= 10
    assert coverage["anomaly"].required_days == 10
    assert coverage["sleep"].observed_days == 14
    assert coverage["sleep"].required_days == 14


def sleep_rows(count=7):
    return [
        {"date": d(day), "bedtime": "23:00", "wake_time": "07:00", "sleep_minutes": 450, "nap_minutes": 0}
        for day in range(1, count + 1)
    ]


def test_sleep_cross_midnight_no_target_no_nap_and_regularity():
    result = compute_sleep(sleep_rows(), target_date=d(7))
    assert result.status == OpenHealthStatus.AVAILABLE
    assert isinstance(result.payload, SleepInsight)
    assert result.payload.time_in_bed_minutes == 480
    assert result.payload.efficiency == 450 / 480
    assert result.payload.midpoint == "03:00"
    assert result.payload.regularity[0].status == "AVAILABLE"
    assert result.payload.regularity[0].available_nights == 7
    assert result.payload.regularity[1].status == "REFUSED"
    assert result.payload.target_status == "REFUSED"

    no_nap = compute_sleep([{k: v for k, v in row.items() if k != "nap_minutes"} for row in sleep_rows()], target_date=d(7))
    assert no_nap.status == OpenHealthStatus.PARTIAL
    assert "nap" in (no_nap.note or "")

    profile = {"user_id": "u", "revision": 3, "sleep_target_minutes": 480}
    with_target = compute_sleep(sleep_rows(), profile, target_date=d(7))
    assert with_target.payload.target_status == "AVAILABLE"
    assert with_target.payload.target_met is False
    assert with_target.payload.target_gap_minutes == 30
    assert with_target.profile_revision_used == 3


def test_sleep_rejects_implausible_window_and_duration():
    short_window = compute_sleep([
        {"date": d(1), "bedtime": "23:00", "wake_time": "00:00", "sleep_minutes": 50}
    ])
    assert short_window.status == OpenHealthStatus.REFUSED
    assert short_window.refusal_reason.code == "INVALID_TIME_IN_BED"

    exceeds_window = compute_sleep([
        {"date": d(1), "bedtime": "23:00", "wake_time": "07:00", "sleep_minutes": 500}
    ])
    assert exceeds_window.status == OpenHealthStatus.REFUSED
    assert exceeds_window.refusal_reason.code == "INVALID_SLEEP_DURATION"


def test_open_health_engine_is_explicit_and_shadow_only():
    bundle = OpenHealthEngine().analyze(sleep_rows(), {"user_id": "u", "revision": 2}, target_date=d(7))
    assert bundle.profile_revision_used == 2
    assert bundle.readiness.shadow_only is True
    assert bundle.anomaly.shadow_only is True
    assert bundle.sleep.shadow_only is True
    assert all(item.schema_version == "1.0" for item in (bundle.readiness, bundle.anomaly, bundle.sleep))
