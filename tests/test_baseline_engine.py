from datetime import date, timedelta

from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import Availability
from vitalis.intelligence.profile import SeriesPoint


def _point(day, value, metric="hrv_rmssd", device="helio"):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit="ms",
        day=day,
        observed_at=day,
        source="zepp",
        source_scope="device",
        device_id=device,
    )


def test_baselines_are_metric_and_device_specific():
    target = date(2026, 8, 28)
    points = []
    for offset in range(1, 22):
        day = target - timedelta(days=offset)
        points.extend([
            _point(day, 50 + offset % 3, device="helio"),
            _point(day, 80 + offset % 3, device="balance"),
        ])
    baselines = BaselineEngine().build({"hrv_rmssd": points}, target)["hrv_rmssd"]
    helio = next(item for item in baselines if item.device_id == "helio" and item.window_days == 28)
    balance = next(item for item in baselines if item.device_id == "balance" and item.window_days == 28)
    assert helio.status == Availability.AVAILABLE
    assert balance.status == Availability.AVAILABLE
    assert helio.transform == "natural_log"
    assert helio.reference_value < balance.reference_value


def test_high_frequency_samples_count_once_per_day_for_eligibility():
    target = date(2026, 8, 28)
    points = [_point(target - timedelta(days=1), 45 + index) for index in range(100)]
    baseline = BaselineEngine().build({"hrv_rmssd": points}, target)["hrv_rmssd"][0]
    assert baseline.sample_count == 100
    assert baseline.distinct_days == 1
    assert baseline.status == Availability.INSUFFICIENT_DATA


def test_deviation_uses_log_rmssd_and_robust_statistics():
    target = date(2026, 8, 28)
    values = [50, 51, 49, 52, 48, 50, 51, 49, 52, 48, 50, 51, 49, 52]
    points = [_point(target - timedelta(days=index + 1), value) for index, value in enumerate(values)]
    baseline = next(
        item for item in BaselineEngine().build({"hrv_rmssd": points}, target)["hrv_rmssd"]
        if item.window_days == 28
    )
    deviation = BaselineEngine.deviation(40, baseline)
    assert deviation.direction == "below"
    assert deviation.percent < -15
    assert deviation.robust_z is not None
