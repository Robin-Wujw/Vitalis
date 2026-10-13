"""Shadow-only daily computations that run in the off-peak batch.

Nothing here reaches a report.  The batch stores each result so a later
comparison can decide, with the user, whether it replaces current logic.
Each function documents its promotion condition.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from math import log
from statistics import fmean, median

from .baseline import BaselineEngine
from .contracts import Availability, BaselineStats
from .events import detect_body_signal_warning
from .open_health.readiness import compute_composite_readiness
from .profile import RawDailyProfile, SeriesPoint

READINESS_VERSION = "readiness-shadow-1"
BODY_SIGNAL_VERSION = "body-signal-shadow-1"
# Robust SD multiplier for the 60-day HRV normal range (smallest worthwhile
# change convention: half a standard deviation either side).
HRV_RANGE_SD = 0.5
HRV_RANGE_DAYS = 60
HRV_MEAN_DAYS = 7
# Metrics whose zero or negative values are real observations.
_SIGNED_METRICS = {"skin_temp_delta", "training_load", "sleep_bedtime_offset_minutes"}


def _canonical_daily(
    raw: RawDailyProfile, metrics: tuple[str, ...], start: date, end: date,
) -> tuple[str | None, dict[date, float]]:
    """Daily medians of the best-covered stream of the first metric with data."""
    for metric in metrics:
        streams: dict[tuple, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
        for point in raw.series.get(metric, []):
            if start <= point.day <= end and (point.value > 0 or metric in _SIGNED_METRICS):
                key = (point.source, point.source_scope, point.device_id, point.unit)
                streams[key][point.day].append(point.value)
        if not streams:
            continue
        best = max(streams.items(), key=lambda item: (end in item[1], len(item[1]), str(item[0])))
        return metric, {day: float(median(values)) for day, values in best[1].items()}
    return None, {}


def _robust(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 7:
        return None
    center = median(values)
    spread = 1.4826 * median(abs(value - center) for value in values)
    return center, spread


def _clamp(value: float) -> float:
    return round(min(100.0, max(0.0, value)), 1)


def _hrv_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    metric, daily = _canonical_daily(
        raw, ("sleep_hrv", "hrv_rmssd"), day - timedelta(days=HRV_RANGE_DAYS + HRV_MEAN_DAYS), day,
    )
    ln_daily = {item: log(value) for item, value in daily.items() if value > 0}

    def mean7(end: date) -> float | None:
        values = [ln_daily[end - timedelta(days=offset)] for offset in range(HRV_MEAN_DAYS)
                  if end - timedelta(days=offset) in ln_daily]
        return fmean(values) if len(values) >= 4 else None

    reference = [value for item, value in ln_daily.items()
                 if day - timedelta(days=HRV_RANGE_DAYS) <= item < day]
    stats = _robust(reference)
    current = mean7(day)
    if stats is None or current is None or stats[1] == 0:
        return {"metric": metric, "score": None, "value": current, "range": None, "streak": 0}
    center, spread = stats
    low, high = center - HRV_RANGE_SD * spread, center + HRV_RANGE_SD * spread
    streak = 0
    for offset in range(0, 14):
        value = mean7(day - timedelta(days=offset))
        if value is None or low <= value <= high:
            break
        streak += 1
    z = (current - center) / spread
    sentence = (
        "HRV 7 天均值高于你 60 天的常态范围" if current > high
        else "HRV 7 天均值低于你 60 天的常态范围" if current < low
        else "HRV 7 天均值在你 60 天的常态范围内"
    )
    return {
        "metric": metric, "score": _clamp(50 + 20 * z), "value": round(current, 4),
        "range": (round(low, 4), round(high, 4)), "streak": streak, "z": round(z, 2),
        "sentence": sentence,
    }


def _sleep_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, duration = _canonical_daily(raw, ("sleep_duration",), day - timedelta(days=28), day)
    if day not in duration:
        return {"score": None}
    history = [value for item, value in duration.items() if item < day]
    stats = _robust(history)
    if stats is None:
        return {"score": None}
    need, spread = stats
    z = (duration[day] - need) / spread if spread else 0.0
    debt_hours = sum(
        max(0.0, need - duration[item]) for item in duration if day - timedelta(days=6) <= item <= day
    ) / 60
    _, bedtimes = _canonical_daily(raw, ("sleep_bedtime_offset_minutes",), day - timedelta(days=6), day)
    bedtime_values = list(bedtimes.values())
    bedtime_spread = (
        1.4826 * median(abs(value - median(bedtime_values)) for value in bedtime_values)
        if len(bedtime_values) >= 4 else None
    )
    score = 50 + 15 * z - 6 * debt_hours - max(0.0, (bedtime_spread or 0) - 30) / 2
    if debt_hours >= 2:
        sentence = f"近 7 天累计少睡约 {debt_hours:.1f} 小时"
    elif z <= -0.75:
        sentence = "昨晚睡得比你平时少"
    elif bedtime_spread is not None and bedtime_spread > 45:
        sentence = "近一周入睡时间不太规律"
    else:
        sentence = "睡眠时长接近你平时的水平"
    return {
        "score": _clamp(score), "z": round(z, 2), "debt_hours": round(debt_hours, 1),
        "bedtime_spread_minutes": round(bedtime_spread, 1) if bedtime_spread is not None else None,
        "need_minutes": round(need, 1), "sentence": sentence,
    }


def _rhr_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, daily = _canonical_daily(raw, ("sleep_rhr", "resting_hr"), day - timedelta(days=28), day)
    stats = _robust([value for item, value in daily.items() if item < day])
    if day not in daily or stats is None or stats[1] == 0:
        return {"score": None}
    z = (daily[day] - stats[0]) / stats[1]
    sentence = (
        "静息心率高于你平时" if z > 0.75 else "静息心率低于你平时" if z < -0.75
        else "静息心率接近你平时"
    )
    return {"score": _clamp(50 - 20 * z), "z": round(z, 2), "sentence": sentence}


def _load_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, load = _canonical_daily(raw, ("training_load",), day - timedelta(days=27), day)
    if not load:
        return {"score": None}
    acute = sum(value for item, value in load.items() if item > day - timedelta(days=7)) / 7
    chronic = sum(load.values()) / 28
    if chronic <= 0:
        return {"score": None}
    ratio = acute / chronic
    sentence = (
        "近 7 天负荷明显高于近 4 周平均" if ratio > 1.3
        else "近 7 天负荷低于近 4 周平均" if ratio < 0.8
        else "近 7 天负荷与近 4 周平均相当"
    )
    return {"score": _clamp(85 - 150 * max(0.0, ratio - 1.0)), "acwr": round(ratio, 2), "sentence": sentence}


def readiness_shadow(raw: RawDailyProfile) -> dict:
    """0–100 readiness from HRV status, sleep, resting HR and recent load.

    Promotion: two weeks of shadow results compared with the vote-based
    recovery state and the vendor readiness, shown to and confirmed by the
    user, before it replaces the morning training basis.
    """
    components = {
        "hrv": _hrv_component(raw),
        "sleep": _sleep_component(raw),
        "rhr": _rhr_component(raw),
        "load": _load_component(raw),
    }
    hrv = components["hrv"]
    insight = compute_composite_readiness(
        {name: item.get("score") for name, item in components.items()},
        target_date=raw.day,
        hrv_value=hrv.get("value"),
        hrv_normal_range=hrv.get("range"),
        hrv_out_of_range_streak=hrv.get("streak", 0),
        profile_revision_used=raw.user_profile.revision,
    )
    present = {name: item for name, item in components.items() if item.get("score") is not None}
    factors = [
        item["sentence"]
        for _, item in sorted(present.items(), key=lambda pair: abs(pair[1]["score"] - 50), reverse=True)[:3]
    ]
    return {
        "version": READINESS_VERSION,
        "insight": insight.model_dump(mode="json"),
        "score": insight.payload.composite_score,
        "hrv_state": insight.payload.state,
        "factors": factors,
        "components": components,
    }


BODY_SIGNALS = {
    # signal name: (metrics in priority order, direction that signals strain)
    "resting_hr": (("sleep_rhr", "resting_hr"), "above"),
    "hrv": (("sleep_hrv", "hrv_rmssd"), "below"),
    "respiratory_rate": (("respiratory_rate",), "above"),
    "skin_temperature_delta": (("skin_temp_delta",), "above"),
    "spo2": (("spo2",), "below"),
}


def body_signal_inputs(raw: RawDailyProfile) -> dict[str, dict]:
    """Two-night deviations from each signal's own 28-night range.

    A signal counts only when both of the last two nights deviate the same way.
    """
    day = raw.day
    output = {}
    for name, (metrics, _) in BODY_SIGNALS.items():
        metric, daily = _canonical_daily(raw, metrics, day - timedelta(days=30), day)
        nights = [day - timedelta(days=1), day]
        reference = [
            value for item, value in daily.items()
            if nights[0] - timedelta(days=28) <= item < nights[0]
        ]
        if metric is None or any(night not in daily for night in nights) or len(reference) < 14:
            output[name] = {"status": "INSUFFICIENT", "metric": metric}
            continue
        center = median(reference)
        baseline = BaselineStats(
            status=Availability.AVAILABLE, metric=metric, source="", source_scope="",
            device_id=None, unit="", window_days=28, sample_count=len(reference), distinct_days=len(reference),
            minimum_days=14, coverage_ratio=round(len(reference) / 28, 4),
            median=center, mad=median(abs(value - center) for value in reference),
            reference_value=center,
        )
        deviations = [BaselineEngine.deviation(daily[night], baseline) for night in nights]
        directions = {item.direction for item in deviations}
        percents = [item.percent for item in deviations if item.percent is not None]
        zs = [item.robust_z for item in deviations if item.robust_z is not None]
        output[name] = {
            "status": "AVAILABLE",
            "metric": metric,
            "direction": directions.pop() if len(directions) == 1 else "near",
            "percent": round(fmean(percents), 2) if len(percents) == 2 else None,
            "robust_z": round(fmean(zs), 2) if len(zs) == 2 else None,
            "values": [round(daily[night], 3) for night in nights],
            "baseline": round(center, 3),
        }
    return output


def body_signal_shadow(raw: RawDailyProfile) -> dict:
    """Warn when two or more body signals leave their personal range together.

    Promotion: the shadow false-positive count over the observation period,
    shown to and confirmed by the user, before the warning enters the morning
    report.  It never names a disease.
    """
    inputs = body_signal_inputs(raw)
    event = detect_body_signal_warning(raw.user_id, raw.day, inputs)
    return {
        "version": BODY_SIGNAL_VERSION,
        "warning": event is not None,
        "evidence": [item.model_dump(mode="json") for item in event.evidence] if event else [],
        "inputs": inputs,
    }


def latest_value(points: list[SeriesPoint], day: date) -> float | None:
    """The vendor value for ``day`` (latest observation), for comparison only."""
    same_day = [point for point in points if point.day == day]
    if not same_day:
        return None
    latest = max(same_day, key=lambda point: str(point.observed_at))
    return latest.last_value if latest.last_value is not None else latest.value
