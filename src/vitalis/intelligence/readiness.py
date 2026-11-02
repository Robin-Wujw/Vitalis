"""Daily readiness score and body-signal warning.

Both ran as shadow modules until the user reviewed 60 days of replayed
results (2026-10-14) and confirmed them for the morning report.  The score
and its factors lead the key records, and its band sets the recovery state
the training plan follows (the user asked for this on 2026-10-14 after
seeing v2 on real mornings); the signal vote is kept for comparison.  A
warning lowers the day's intensity.  The off-peak batch still
records both for tracking.

Components score 50 at the user's own norm.  The displayed score reads like
a grade, as the user asked (2026-10-14): the personal norm is 70, 60 is a
pass, 75 "fair" and 85 "good"; the transform is linear, so it keeps the
order of days.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import log
from statistics import fmean, median

from .baseline import BaselineEngine
from .contracts import Availability, BaselineStats
from .events import detect_body_signal_warning
from .open_health.readiness import compute_composite_readiness
from .profile import RawDailyProfile, SeriesPoint

READINESS_VERSION = "readiness-2"
BODY_SIGNAL_VERSION = "body-signal-1"
# Displayed score: 70 + 0.9 x (composite - 50), so the personal norm is 70.
DISPLAY_NORM = 70
DISPLAY_SCALE = 0.9
PASS_FROM = 60
FAIR_FROM = 75
GOOD_FROM = 85
# Robust SD multiplier for the 60-day HRV normal range (smallest worthwhile
# change convention: half a standard deviation either side).
HRV_RANGE_SD = 0.5
HRV_RANGE_DAYS = 60
HRV_MEAN_DAYS = 7
# Metrics whose zero or negative values are real observations.
_SIGNED_METRICS = {"skin_temp_delta", "training_load", "sleep_bedtime_offset_minutes"}


def is_vendor_account_summary(source: str, scope: str, device_id: str | None) -> bool:
    """An explicit Zepp account source, not evidence of a fusion algorithm."""
    # An account-level record may retain a contributing device as metadata.
    # That identifier neither changes the scope nor verifies how Zepp combined it.
    return source == "zepp" and scope in {"user_summary", "user_fused"}


def _source_priority(source: str, scope: str, device_id: str | None) -> int:
    if is_vendor_account_summary(source, scope, device_id):
        return 3
    if device_id is not None and scope not in {"", "unknown"}:
        return 2
    return int(scope not in {"", "unknown"})


def select_analysis_stream(
    points: list[SeriesPoint], start: date, end: date, *, metric: str,
) -> tuple[list[SeriesPoint], dict]:
    """Choose one comparable stream for the entire window without ID tie-breaks.

    A current reading is required before considering history-only alternatives.
    Within that group, explicit account summaries precede identified devices,
    then other known scopes and finally unknown sources. Coverage breaks ties,
    never measured values, device names or input order. Equal evidence abstains.
    """
    streams: dict[tuple, list[SeriesPoint]] = defaultdict(list)
    for point in points:
        if start <= point.day <= end:
            streams[(point.source, point.source_scope, point.device_id, point.unit)].append(point)
    provenance = {
        "metric": metric, "source": None, "source_scope": None, "device_id": None, "unit": None,
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "distinct_days": 0, "sample_count": 0, "candidate_count": len(streams),
        "selection_reason": "no_data", "fusion_verified": False, "candidates": [],
    }
    if not streams:
        return [], provenance

    candidates = []
    for stream, samples in streams.items():
        source, scope, device_id, unit = stream
        days = {point.day for point in samples}
        sample_count = sum(point.sample_count for point in samples)
        coverage: dict[date, int] = defaultdict(int)
        observed_minutes: dict[date, set[str]] = defaultdict(set)
        for point in samples:
            if point.observed_minutes is not None:
                coverage[point.day] += point.observed_minutes
            elif isinstance(point.observed_at, datetime):
                observed_minutes[point.day].add(point.observed_at.replace(second=0, microsecond=0).isoformat())
        covered_minutes = sum(min(1440, coverage[day] + len(observed_minutes[day])) for day in days)
        priority = _source_priority(source, scope, device_id)
        reason = (
            "explicit_account_summary" if priority == 3
            else "best_covered_device_stream" if priority == 2
            else "known_scope_stream" if priority == 1
            else "unknown_source_only"
        )
        details = {
            "source": source, "source_scope": scope, "device_id": device_id, "unit": unit,
            "distinct_days": len(days), "sample_count": sample_count,
            "covered_minutes": covered_minutes, "target_day_present": end in days,
            "selection_reason": reason, "selected": False,
            "observed_start": min(point.observed_at.isoformat() for point in samples),
            "observed_end": max(point.observed_at.isoformat() for point in samples),
            "source_fields": sorted({point.source_field for point in samples if point.source_field}),
            "qualifications": sorted({point.qualification for point in samples if point.qualification}),
        }
        fetched = [point.fetched_at.isoformat() for point in samples if point.fetched_at]
        details["fetched_at"] = max(fetched) if fetched else None
        key = (end in days, priority, len(days), covered_minutes, sample_count)
        candidates.append((key, samples, details))
    best_key = max(item[0] for item in candidates)
    best = [item for item in candidates if item[0] == best_key]
    # Sorting is for stable audit output only, never to choose a device.
    provenance["candidates"] = sorted(
        (item[2] for item in candidates),
        key=lambda item: (item["source"], item["source_scope"], item["device_id"] or "", item["unit"]),
    )
    if len(best) != 1:
        provenance["selection_reason"] = "ambiguous_equal_coverage"
        for _, _, details in best:
            details["selection_reason"] = "ambiguous_equal_coverage"
        return [], provenance
    _, selected, details = best[0]
    details["selected"] = True
    provenance.update(details)
    return selected, provenance


def _canonical_daily(
    raw: RawDailyProfile, metrics: tuple[str, ...], start: date, end: date,
) -> tuple[str | None, dict[date, float], dict]:
    """Daily medians of one source for the first metric with eligible data."""
    for metric in metrics:
        points = [
            point for point in raw.series.get(metric, [])
            if start <= point.day <= end and (point.value > 0 or metric in _SIGNED_METRICS)
        ]
        if not points:
            continue
        selected, provenance = select_analysis_stream(points, start, end, metric=metric)
        daily: dict[date, list[float]] = defaultdict(list)
        for point in selected:
            daily[point.day].append(point.value)
        return metric, {day: float(median(values)) for day, values in daily.items()}, provenance
    return None, {}, {}


def _robust(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 7:
        return None
    center = median(values)
    spread = 1.4826 * median(abs(value - center) for value in values)
    return center, spread


def _clamp(value: float) -> float:
    return round(min(100.0, max(0.0, value)), 1)


WAKE_CHARGE_DAYS = 60
WAKE_CHARGE_MIN_DAYS = 14


def _wake_charge_selection(raw: RawDailyProfile) -> tuple[dict[date, float], dict]:
    points = [
        point for point in raw.series.get("hybrid_charge", [])
        # A vendor daily summary is not a reading on waking.
        if not point.is_daily_summary
        and (point.is_daily_feature or isinstance(point.observed_at, datetime))
    ]
    selected, provenance = select_analysis_stream(
        points, raw.day - timedelta(days=WAKE_CHARGE_DAYS), raw.day, metric="hybrid_charge",
    )
    by_day: dict[date, float] = {}
    for point in selected:
        value = point.maximum if point.maximum is not None else point.value
        if value is not None and value > by_day.get(point.day, float("-inf")):
            by_day[point.day] = float(value)
    provenance["aggregation"] = "same_stream_daily_maximum"
    return by_day, provenance


def wake_charge_daily(raw: RawDailyProfile) -> dict[date, float]:
    """Daily wake proxy from one stream across the complete 60-day baseline.

    Keep the established daily-maximum proxy, but never take the maximum of
    different devices or borrow another stream's history. Daily features carry
    a stream's maximum; detail days use only that stream's timestamped samples.
    """
    return _wake_charge_selection(raw)[0]


def wake_charge_summary(raw: RawDailyProfile) -> dict:
    """The day's wake-up Charge with the median and 20th/80th percentile of
    the previous 60 days of the same source (none with fewer than 14 days)."""
    by_day, provenance = _wake_charge_selection(raw)
    history = sorted(
        value for day, value in by_day.items()
        if raw.day - timedelta(days=WAKE_CHARGE_DAYS) <= day < raw.day
    )
    output = {
        "wake_charge": by_day.get(raw.day),
        "wake_charge_reference": None, "wake_charge_low": None, "wake_charge_high": None,
        "wake_charge_provenance": provenance,
    }
    if len(history) >= WAKE_CHARGE_MIN_DAYS:
        pick = lambda q: history[int(q * (len(history) - 1))]
        output.update(
            wake_charge_reference=round(float(median(history)), 1),
            wake_charge_low=pick(0.2), wake_charge_high=pick(0.8),
        )
    return output



def _hrv_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    metric, daily, provenance = _canonical_daily(
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
        return {"metric": metric, "score": None, "value": current, "range": None, "streak": 0,
                "provenance": provenance}
    center, spread = stats
    low, high = center - HRV_RANGE_SD * spread, center + HRV_RANGE_SD * spread
    streak = 0
    for offset in range(0, 14):
        value = mean7(day - timedelta(days=offset))
        if value is None or low <= value <= high:
            break
        streak += 1
    z_week = (current - center) / spread
    # The 7-day mean alone trails a recovery by about a week; last night's
    # value carries half the weight when it exists.
    z_night = (ln_daily[day] - center) / spread if day in ln_daily else None
    z = z_week if z_night is None else (z_week + z_night) / 2
    if z_night is not None and z_night >= 0.75:
        sentence = "昨晚 HRV 高于你平时"
    elif z_night is not None and z_night <= -0.75:
        sentence = "昨晚 HRV 低于你平时"
    else:
        sentence = (
            "HRV 7 天均值高于你 60 天的常态范围" if current > high
            else "HRV 7 天均值低于你 60 天的常态范围" if current < low
            else "HRV 7 天均值在你 60 天的常态范围内"
        )
    return {
        "metric": metric, "score": _clamp(50 + 20 * z), "value": round(current, 4),
        "range": (round(low, 4), round(high, 4)), "streak": streak, "z": round(z, 2),
        "z_night": round(z_night, 2) if z_night is not None else None, "sentence": sentence,
        "provenance": provenance,
    }


def _sleep_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, duration, provenance = _canonical_daily(raw, ("sleep_duration",), day - timedelta(days=28), day)
    if day not in duration:
        return {"score": None, "provenance": provenance}
    history = [value for item, value in duration.items() if item < day]
    stats = _robust(history)
    if stats is None:
        return {"score": None, "provenance": provenance}
    need, spread = stats
    z = (duration[day] - need) / spread if spread else 0.0
    # Net 7-day debt: a long night pays back a short one.
    debt_hours = max(0.0, sum(
        need - duration[item] for item in duration if day - timedelta(days=6) <= item <= day
    )) / 60
    _, bedtimes, bedtime_provenance = _canonical_daily(
        raw, ("sleep_bedtime_offset_minutes",), day - timedelta(days=6), day,
    )
    provenance["bedtime_provenance"] = bedtime_provenance
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
        "need_minutes": round(need, 1), "sentence": sentence, "provenance": provenance,
    }


def _rhr_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, daily, provenance = _canonical_daily(raw, ("sleep_rhr", "resting_hr"), day - timedelta(days=28), day)
    stats = _robust([value for item, value in daily.items() if item < day])
    if day not in daily or stats is None or stats[1] == 0:
        return {"score": None, "provenance": provenance}
    z = (daily[day] - stats[0]) / stats[1]
    sentence = (
        "静息心率高于你平时" if z > 0.75 else "静息心率低于你平时" if z < -0.75
        else "静息心率接近你平时"
    )
    return {"score": _clamp(50 - 20 * z), "z": round(z, 2), "sentence": sentence,
            "provenance": provenance}


def _load_component(raw: RawDailyProfile) -> dict:
    day = raw.day
    _, load, provenance = _canonical_daily(raw, ("training_load",), day - timedelta(days=27), day)
    if not load:
        return {"score": None, "provenance": provenance}
    acute = sum(value for item, value in load.items() if item > day - timedelta(days=7)) / 7
    chronic = sum(load.values()) / 28
    if chronic <= 0:
        return {"score": None, "provenance": provenance}
    ratio = acute / chronic
    sentence = (
        "近 7 天负荷明显高于近 4 周平均" if ratio > 1.3
        else "近 7 天负荷低于近 4 周平均" if ratio < 0.8
        else "近 7 天负荷与近 4 周平均相当"
    )
    # 50 is the personal norm: an ordinary ratio is neutral, a fast rise
    # lowers readiness and a lighter week adds a little.
    score = 50 - 100 * (ratio - 1.3) if ratio > 1.3 else 55 if ratio < 0.8 else 50
    return {"score": _clamp(score), "acwr": round(ratio, 2), "sentence": sentence,
            "provenance": provenance}


def _charge_component(raw: RawDailyProfile) -> dict:
    by_day, provenance = _wake_charge_selection(raw)
    history = [value for day, value in by_day.items()
               if raw.day - timedelta(days=WAKE_CHARGE_DAYS) <= day < raw.day]
    stats = _robust(history) if len(history) >= WAKE_CHARGE_MIN_DAYS else None
    if raw.day not in by_day or stats is None or stats[1] == 0:
        return {"score": None, "provenance": provenance}
    z = (by_day[raw.day] - stats[0]) / stats[1]
    sentence = (
        "晨起 Charge 低于你平时" if z <= -0.75 else "晨起 Charge 高于你平时" if z >= 0.75
        else "晨起 Charge 接近你平时"
    )
    return {"score": _clamp(50 + 15 * z), "z": round(z, 2), "value": by_day[raw.day],
            "sentence": sentence, "provenance": provenance}


def readiness_score(raw: RawDailyProfile) -> dict:
    """0–100 readiness from HRV status, sleep, resting HR and recent load."""
    components = {
        "hrv": _hrv_component(raw),
        "sleep": _sleep_component(raw),
        "rhr": _rhr_component(raw),
        "load": _load_component(raw),
        "charge": _charge_component(raw),
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
    present = {
        name: item for name, item in components.items()
        if item.get("score") is not None and name != "charge"
    }
    factors = [
        item["sentence"]
        for _, item in sorted(present.items(), key=lambda pair: abs(pair[1]["score"] - 50), reverse=True)[:3]
    ]
    composite = insight.payload.composite_score
    return {
        "version": READINESS_VERSION,
        "insight": insight.model_dump(mode="json"),
        "composite": composite,
        "score": display_score(composite),
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
        metric, daily, provenance = _canonical_daily(raw, metrics, day - timedelta(days=30), day)
        nights = [day - timedelta(days=1), day]
        reference = [
            value for item, value in daily.items()
            if nights[0] - timedelta(days=28) <= item < nights[0]
        ]
        if metric is None or any(night not in daily for night in nights) or len(reference) < 14:
            output[name] = {"status": "INSUFFICIENT", "metric": metric, "provenance": provenance}
            continue
        center = median(reference)
        baseline = BaselineStats(
            status=Availability.AVAILABLE, metric=metric,
            source=provenance["source"], source_scope=provenance["source_scope"],
            device_id=provenance["device_id"], unit=provenance["unit"],
            window_days=28, sample_count=len(reference), distinct_days=len(reference),
            minimum_days=14, coverage_ratio=round(len(reference) / 28, 4),
            median=center, mad=median(abs(value - center) for value in reference),
            reference_value=center,
        )
        deviations = [BaselineEngine.deviation(daily[night], baseline) for night in nights]
        directions = {item.direction for item in deviations}
        # A temperature delta is already a difference around zero; a percentage
        # of it is meaningless, so it qualifies by robust z only.
        percents = [] if metric in _SIGNED_METRICS else [
            item.percent for item in deviations if item.percent is not None
        ]
        zs = [item.robust_z for item in deviations if item.robust_z is not None]
        output[name] = {
            "status": "AVAILABLE",
            "metric": metric,
            "direction": directions.pop() if len(directions) == 1 else "near",
            "percent": round(fmean(percents), 2) if len(percents) == 2 else None,
            "robust_z": round(fmean(zs), 2) if len(zs) == 2 else None,
            "values": [round(daily[night], 3) for night in nights],
            "baseline": round(center, 3), "provenance": provenance,
        }
    return output


def body_signal_warning(raw: RawDailyProfile) -> dict:
    """Warn when two or more body signals leave their personal range together.

    It never names a disease; the morning report only lowers the intensity.
    """
    inputs = body_signal_inputs(raw)
    event = detect_body_signal_warning(raw.user_id, raw.day, inputs)
    return {
        "version": BODY_SIGNAL_VERSION,
        "event": event,
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


def display_score(composite: float | None) -> int | None:
    if composite is None:
        return None
    return round(min(100.0, max(0.0, DISPLAY_NORM + DISPLAY_SCALE * (composite - 50))))


def readiness_band(score: int | None) -> str | None:
    """Recovery band of a displayed score."""
    if score is None:
        return None
    return "GOOD" if score >= GOOD_FROM else "SUPPRESSED" if score < PASS_FROM else "NORMAL"


def readiness_label(score: int | None) -> str | None:
    if score is None:
        return None
    return (
        "较好" if score >= GOOD_FROM else "还行" if score >= FAIR_FROM
        else "正常" if score >= PASS_FROM else "偏低"
    )


def apply_readiness(recovery, readiness: dict):
    """Let the displayed score's band set the recovery state the plan follows.

    The signal vote stays in ``vote_state`` for comparison; without a score
    the vote still decides.
    """
    from .contracts import RecoveryState
    from .localization import RECOVERY_LABELS

    score = readiness.get("score")
    update = {
        "readiness_score": score,
        "readiness_factors": list(readiness.get("factors") or []),
        "readiness_hrv_state": readiness.get("hrv_state"),
        "readiness_component_provenance": {
            name: item["provenance"] for name, item in (readiness.get("components") or {}).items()
            if item.get("provenance")
        },
        "readiness_missing_components": list(
            ((readiness.get("insight") or {}).get("payload") or {}).get("missing_components") or []
        ),
        "vote_state": recovery.state,
    }
    band = readiness_band(score)
    if band is not None:
        state = RecoveryState(band)
        update.update(state=state, state_label=RECOVERY_LABELS[state.value], status=Availability.AVAILABLE)
    return recovery.model_copy(update=update)
