"""Sleep timing and regularity insights for shadow-only Open Health output.

Time arithmetic is Vitalis policy. The circular-MAD presentation is adapted
from OpenStrap-style personal timing analytics and is not a clinical measure.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any, Iterable

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    OpenHealthInsights,
    OpenHealthProvenance,
    OpenHealthRefusalReason,
    OpenHealthStatus,
    SleepInsight,
    SleepRegularityInsight,
)

from .common import OpenHealthObservation, as_minutes, parse_clock, profile_value, sorted_observations
from .ewma import UPSTREAM_REVISION


def _window(row: OpenHealthObservation) -> tuple[float, float] | None:
    bed = parse_clock(row.bedtime)
    wake = parse_clock(row.wake_time)
    if bed is None or wake is None:
        return None
    bed_minutes, wake_minutes = as_minutes(bed), as_minutes(wake)
    tib = (wake_minutes - bed_minutes) % 1440
    if tib == 0:
        tib = 1440
    return bed_minutes, tib


def _circular_distance(a: float, b: float) -> float:
    return abs((a - b + 720) % 1440 - 720)


def _circular_mad(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    candidate = min(values, key=lambda item: sum(_circular_distance(item, value) for value in values))
    return candidate, median([_circular_distance(candidate, value) for value in values])


def _clock_string(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    minute = int(round(minutes)) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _regularity(rows: list[OpenHealthObservation], target: date, days: int, minimum: int) -> SleepRegularityInsight:
    selected = [row for row in rows if target - timedelta(days=days - 1) <= row.date <= target and _window(row) is not None]
    if len(selected) < minimum:
        return SleepRegularityInsight(window_days=days, minimum_nights=minimum, available_nights=len(selected), status="REFUSED")
    beds = [_window(row)[0] for row in selected]
    wakes = [(beds[index] + _window(row)[1]) % 1440 for index, row in enumerate(selected)]
    midpoints = [(beds[index] + _window(row)[1] / 2) % 1440 for index, row in enumerate(selected)]
    return SleepRegularityInsight(
        window_days=days,
        minimum_nights=minimum,
        available_nights=len(selected),
        bedtime_circular_mad_minutes=_circular_mad(beds)[1],
        wake_circular_mad_minutes=_circular_mad(wakes)[1],
        midpoint_circular_mad_minutes=_circular_mad(midpoints)[1],
        status="AVAILABLE",
    )


def compute_sleep(
    observations: Iterable[OpenHealthObservation | dict[str, Any]],
    profile: Any = None,
    *,
    target_date: date | None = None,
    profile_revision_used: int | None = None,
) -> OpenHealthInsights:
    rows = sorted_observations(list(observations))
    target = target_date or (rows[-1].date if rows else date.today())
    target_row = next((row for row in reversed(rows) if row.date == target), None)
    extra = target_row.model_extra if target_row else {}
    base = dict(
        algorithm_id="open_health.sleep",
        version="1.0",
        upstream_revision=UPSTREAM_REVISION,
        shadow_only=True,
        tier="refused",
        inputs_used=["sleep.bedtime", "sleep.wake_time", "sleep_minutes", "nap_minutes", "UserProfile.sleep_target_minutes"],
        coverage={"week_window_days": 7, "month_window_days": 28},
        confidence=ConfidenceBand.NONE,
        drivers=[],
        provenance=[OpenHealthProvenance(
            source=str(extra.get("sleep_source") or (target_row.source if target_row else "unknown")),
            source_scope=str(extra.get("sleep_source_scope") or "normalized_daily_record"),
            device_id=extra.get("sleep_device_id"),
            module="vitalis.intelligence.open_health.sleep",
            algorithm="cross_midnight_timing_circular_mad",
            upstream_revision=UPSTREAM_REVISION,
        )],
        profile_revision_used=profile_revision_used if profile_revision_used is not None else int(profile_value(profile, "revision") or 0),
    )
    if target_row is None:
        base.update(status=OpenHealthStatus.REFUSED, note="目标夜缺少睡眠观测。", payload=SleepInsight(target_date=target),
                    refusal_reason=OpenHealthRefusalReason(code="MISSING_TARGET_SLEEP", detail="需要目标夜睡眠记录。", missing_inputs=["target.sleep"]))
        return OpenHealthInsights(**base)

    timing = _window(target_row)
    sleep_minutes = target_row.sleep_minutes
    if timing is None and (sleep_minutes is None or sleep_minutes < 0):
        base.update(status=OpenHealthStatus.REFUSED, note="目标夜缺少有效 bedtime/wake_time 或 sleep_minutes。", payload=SleepInsight(target_date=target),
                    refusal_reason=OpenHealthRefusalReason(code="MISSING_SLEEP_TIMING", detail="无法计算跨午夜 time-in-bed。", missing_inputs=["bedtime", "wake_time", "sleep_minutes"]))
        return OpenHealthInsights(**base)
    tib = timing[1] if timing else target_row.time_in_bed_minutes
    if tib is None or not 120 <= tib <= 960:
        base.update(status=OpenHealthStatus.REFUSED, note="目标夜 time-in-bed 超出可信范围。", payload=SleepInsight(target_date=target),
                    refusal_reason=OpenHealthRefusalReason(code="INVALID_TIME_IN_BED", detail="time-in-bed 必须在 120..960 分钟。", missing_inputs=["time_in_bed_minutes"]))
        return OpenHealthInsights(**base)
    if sleep_minutes is None:
        sleep_minutes = max(0.0, tib - float(target_row.model_extra.get("awake_minutes", 0) or 0))
    if sleep_minutes < 0 or sleep_minutes > tib + 5:
        base.update(status=OpenHealthStatus.REFUSED, note="睡眠时长与在床窗口不一致。", payload=SleepInsight(target_date=target),
                    refusal_reason=OpenHealthRefusalReason(code="INVALID_SLEEP_DURATION", detail="sleep duration 不能明显超过 time-in-bed。", missing_inputs=["sleep_minutes"]))
        return OpenHealthInsights(**base)
    efficiency = min(1.0, sleep_minutes / tib)
    bed = parse_clock(target_row.bedtime)
    wake = parse_clock(target_row.wake_time)
    midpoint = (as_minutes(bed) + tib / 2) % 1440 if bed else None
    target_minutes = profile_value(profile, "sleep_target_minutes")
    if target_minutes is not None:
        target_minutes = int(target_minutes)
    nap_known = target_row.naps_known is not None or target_row.nap_minutes is not None or "has_nap" in (target_row.model_extra or {})
    nap_minutes = target_row.nap_minutes if nap_known else None
    regularity = [_regularity(rows, target, 7, 5), _regularity(rows, target, 28, 14)]
    target_status = "AVAILABLE" if target_minutes is not None else "REFUSED"
    note_parts: list[str] = []
    status = OpenHealthStatus.AVAILABLE
    if target_minutes is None:
        note_parts.append("未提供睡眠目标；达标/缺口子项拒绝计算。")
    if not nap_known:
        status = OpenHealthStatus.PARTIAL
        note_parts.append("缺少 nap 数据，睡眠洞察标记为 PARTIAL。")
    available_regularity = sum(item.status == "AVAILABLE" for item in regularity)
    payload = SleepInsight(
        target_date=target,
        time_in_bed_minutes=tib,
        sleep_minutes=sleep_minutes,
        efficiency=efficiency,
        bedtime=_clock_string(as_minutes(bed) if bed else None),
        wake_time=_clock_string(as_minutes(wake) if wake else None),
        midpoint=_clock_string(midpoint),
        regularity=regularity,
        target_minutes=target_minutes,
        target_met=(sleep_minutes >= target_minutes) if target_minutes is not None else None,
        target_gap_minutes=max(target_minutes - sleep_minutes, 0) if target_minutes is not None else None,
        target_status=target_status,
        naps_known=nap_known,
        nap_minutes=nap_minutes,
    )
    base.update(status=status, tier="trusted" if available_regularity == 2 else "provisional",
                confidence=min(1.0, len([row for row in rows if row.date <= target]) / 28.0),
                coverage={**base["coverage"], "regularity_available_windows": available_regularity, "nap_known": nap_known},
                drivers=[f"time_in_bed={tib:.1f}m", f"efficiency={efficiency:.6f}"],
                note=" ".join(note_parts) if note_parts else None, payload=payload)
    return OpenHealthInsights(**base)


compute_sleep_insight = compute_sleep
sleep_insight = compute_sleep
