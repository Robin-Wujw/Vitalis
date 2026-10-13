"""Vitalis readiness policy on the OpenStrap nightly lnRMSSD stream.

Ported/adapted upstream analytics are limited to the lnRMSSD/EWMA conventions;
thresholds, coverage gates, and wording are Vitalis policy. No training decision
is emitted here.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
from math import log
from statistics import mean, stdev
from typing import Any, Iterable, Mapping

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    OpenHealthInsights,
    OpenHealthProvenance,
    OpenHealthRefusalReason,
    OpenHealthStatus,
    ReadinessInsight,
)

from .common import OpenHealthObservation, distinct_observations, sorted_observations
from .ewma import UPSTREAM_REVISION


def _valid_nightly_rmssd(observation: OpenHealthObservation) -> bool:
    if observation.rmssd_ms is None or observation.rmssd_ms <= 0:
        return False
    if observation.sample_count is None:
        return True
    extra = observation.model_extra or {}
    span_minutes = extra.get("span_minutes")
    return observation.sample_count >= 3 and (
        span_minutes is None or float(span_minutes) >= 30.0
    )


def _rr_present(observation: OpenHealthObservation) -> bool:
    if observation.rr_available is not None:
        return observation.rr_available
    extra = observation.model_extra or {}
    return bool(extra.get("rr_intervals") or extra.get("rr_ms") or extra.get("respiratory_intervals"))


def compute_readiness(
    observations: Iterable[OpenHealthObservation | dict[str, Any]],
    *,
    target_date: date | None = None,
    profile_revision_used: int = 0,
) -> OpenHealthInsights:
    rows = sorted_observations(list(observations))
    target = target_date or (rows[-1].date if rows else date.today())
    # Resolve duplicate calendar days before applying the nightly gates. The
    # selected row is the latest explicit timestamp, or a deterministic input
    # tie-break when a vendor only supplies a date.
    valid_rows = distinct_observations(rows, valid=_valid_nightly_rmssd)
    target_row = next((row for row in valid_rows if row.date == target), None)
    stream_key = (
        target_row.source,
        target_row.source_scope,
        target_row.device_id,
    ) if target_row else None
    history = distinct_observations([
        row for row in rows
        if row.date <= target
        and _valid_nightly_rmssd(row)
        and (row.source, row.source_scope, row.device_id) == stream_key
    ], valid=_valid_nightly_rmssd) if stream_key else []
    prior = [
        row for row in history
        if target - timedelta(days=7) <= row.date < target
    ][-7:]
    common = dict(
        algorithm_id="open_health.readiness",
        version="1.0",
        upstream_revision=UPSTREAM_REVISION,
        shadow_only=True,
        tier="refused",
        inputs_used=["nightly.rmssd_ms", "observation.date"],
        coverage={"history_nights": len(history), "prior_nights": len(prior), "window_days": 7},
        confidence=ConfidenceBand.NONE,
        drivers=[],
        provenance=[OpenHealthProvenance(
            source=target_row.source if target_row else "unknown",
            source_scope=target_row.source_scope if target_row else "nightly_observation",
            device_id=target_row.device_id if target_row else None,
            module="vitalis.intelligence.open_health.readiness",
            algorithm="nightly_ln_rmssd",
            upstream_revision=UPSTREAM_REVISION,
        )],
        profile_revision_used=profile_revision_used,
    )
    if target_row is None:
        common.update(
            status=OpenHealthStatus.REFUSED,
            note="目标夜缺少有效 RMSSD。",
            refusal_reason=OpenHealthRefusalReason(
                code="MISSING_TARGET_RMSSD",
                detail="Readiness 只使用同一 nightly lnRMSSD stream。",
                missing_inputs=["target.rmssd_ms"],
            ),
            payload=ReadinessInsight(target_date=target),
        )
        return OpenHealthInsights(**common)
    if len(history) < 4 or len(prior) < 2:
        common.update(
            status=OpenHealthStatus.REFUSED,
            note="历史夜数不足，未生成 readiness 状态。",
            refusal_reason=OpenHealthRefusalReason(
                code="INSUFFICIENT_READINESS_HISTORY",
                detail="至少需要 4 夜总历史且目标夜之前至少 2 夜。",
                missing_inputs=["history_nights>=4", "prior_nights>=2"],
            ),
            payload=ReadinessInsight(
                target_date=target, ln_rmssd=log(target_row.rmssd_ms),
                history_nights=len(history), prior_nights=len(prior),
            ),
        )
        return OpenHealthInsights(**common)

    target_ln = log(target_row.rmssd_ms)
    prior_ln = [log(row.rmssd_ms) for row in prior]
    baseline = mean(prior_ln)
    sd = stdev(prior_ln) if len(prior_ln) >= 2 else 0.0
    if sd <= 1e-9:
        common.update(
            status=OpenHealthStatus.REFUSED,
            note="历史 lnRMSSD 没有可解释离散度，未生成 readiness 状态。",
            refusal_reason=OpenHealthRefusalReason(
                code="ZERO_READINESS_DISPERSION",
                detail="SWC 需要非零的 prior-window 标准差。",
                missing_inputs=["prior_ln_rmssd_dispersion>0"],
            ),
            payload=ReadinessInsight(
                target_date=target,
                ln_rmssd=target_ln,
                baseline_ln_rmssd=baseline,
                history_nights=len(history),
                prior_nights=len(prior),
                rr_available=_rr_present(target_row),
            ),
        )
        return OpenHealthInsights(**common)
    swc = 0.5 * sd
    delta = target_ln - baseline
    if delta < -swc:
        state = "suppressed"
    elif delta > swc:
        state = "elevated"
    else:
        state = "normal"
    rr_available = _rr_present(target_row)
    note = None if rr_available else "缺少 RR interval；readiness 仍仅按 lnRMSSD 计算。"
    confidence = min(1.0, len(prior) / 7.0)
    common.update(
        status=OpenHealthStatus.AVAILABLE,
        tier="trusted" if len(history) >= 14 else "provisional",
        coverage={"history_nights": len(history), "prior_nights": len(prior), "window_days": 7, "rr_available": rr_available},
        confidence=confidence,
        drivers=[f"lnRMSSD delta={delta:.6f}", f"SWC={swc:.6f}"],
        note=note,
        payload=ReadinessInsight(
            target_date=target, ln_rmssd=target_ln, baseline_ln_rmssd=baseline,
            delta=delta, swc=swc, state=state, history_nights=len(history),
            prior_nights=len(prior), rr_available=rr_available,
        ),
    )
    return OpenHealthInsights(**common)


def compute_composite_readiness(
    component_scores: Mapping[str, float | None],
    *,
    target_date: date,
    hrv_value: float | None = None,
    hrv_normal_range: tuple[float, float] | None = None,
    hrv_out_of_range_streak: int = 0,
    profile_revision_used: int = 0,
) -> OpenHealthInsights:
    """Build the gated 0–100 readiness shadow score from saved component scores.

    Callers supply already-qualified component scores; this function never reads
    raw data or invents a missing component. Weights are renormalized across
    present components and the output remains shadow-only until promotion review.
    """
    base_weights = {"hrv": 0.35, "sleep": 0.25, "rhr": 0.20, "load": 0.20}
    present: dict[str, float] = {}
    for name, value in component_scores.items():
        if name not in base_weights or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("readiness component scores must be finite numbers")
        if not 0 <= float(value) <= 100:
            raise ValueError("readiness component scores must be between 0 and 100")
        present[name] = float(value)
    present = {name: value for name, value in present.items() if name in base_weights}
    missing = sorted(set(base_weights) - set(present))
    total = sum(base_weights[name] for name in present)
    weights = {
        name: round(base_weights[name] / total, 6)
        for name in present
    } if total else {}
    score = round(sum(present[name] * weights[name] for name in present)) if present else None
    drivers = [
        f"{name} {present[name]:.0f} 分（权重 {weights[name] * 100:.0f}%）"
        for name in sorted(present, key=lambda key: abs(present[key] - 50), reverse=True)[:3]
    ]
    low, high = hrv_normal_range or (None, None)
    hrv_state = "unavailable"
    if hrv_value is not None and low is not None and high is not None:
        if hrv_out_of_range_streak >= 2:
            hrv_state = "suppressed" if hrv_value < low else "elevated" if hrv_value > high else "normal"
        else:
            hrv_state = "normal"
    payload = ReadinessInsight(
        target_date=target_date,
        ln_rmssd=None,
        state=hrv_state,
        composite_score=score,
        component_scores=present,
        applied_weights=weights,
        missing_components=missing,
        driver_labels=drivers,
        hrv_normal_low=low,
        hrv_normal_high=high,
        hrv_out_of_range_streak=hrv_out_of_range_streak,
    )
    return OpenHealthInsights(
        algorithm_id="open_health.readiness_composite",
        version="1.0",
        upstream_revision=UPSTREAM_REVISION,
        shadow_only=True,
        status=OpenHealthStatus.AVAILABLE if score is not None else OpenHealthStatus.REFUSED,
        tier="provisional" if score is not None else "refused",
        inputs_used=sorted(present),
        coverage={"component_count": len(present), "required_component_count": len(base_weights)},
        confidence=ConfidenceBand.LOW if score is not None else ConfidenceBand.NONE,
        drivers=drivers,
        note=("缺少部分分项，已按剩余分项重新分配权重。" if missing else None),
        refusal_reason=(OpenHealthRefusalReason(
            code="MISSING_READINESS_COMPONENTS",
            detail="没有一个合资格的准备度分项。",
            missing_inputs=list(base_weights),
        ) if score is None else None),
        provenance=[OpenHealthProvenance(
            source="vitalis", source_scope="qualified_daily_features",
            module="vitalis.intelligence.open_health.readiness",
            algorithm="weighted_robust_component_score",
            upstream_revision=UPSTREAM_REVISION,
        )],
        profile_revision_used=profile_revision_used,
        payload=payload,
    )


readiness_insight = compute_readiness
