"""Shadow-only robust multivariate nightly anomaly screen.

The robust Mahalanobis structure is adapted from OpenStrap analytics (MIT,
revision 45d72ed989c004008b919b366cd5ceda7061b7df). Coverage, persistence,
small-sample inflation, and non-diagnostic wording are Vitalis policy.
"""

from __future__ import annotations

from datetime import date, timedelta
from math import log, sqrt
from statistics import median, stdev
from typing import Any, Iterable

from vitalis.intelligence.contracts import (
    AnomalyInsight,
    ConfidenceBand,
    OpenHealthInsights,
    OpenHealthProvenance,
    OpenHealthRefusalReason,
    OpenHealthStatus,
)

from .common import OpenHealthObservation, sorted_observations
from .ewma import UPSTREAM_REVISION

DIMENSIONS = ("ln_rmssd", "rhr", "resp")
CHI_SQUARE_999 = {2: 13.82, 3: 16.27}


def _components(row: OpenHealthObservation) -> dict[str, float]:
    values: dict[str, float] = {}
    if row.rmssd_ms is not None and row.rmssd_ms > 0:
        values["ln_rmssd"] = -log(row.rmssd_ms)
    if row.rhr_bpm is not None:
        values["rhr"] = float(row.rhr_bpm)
    if row.respiratory_rate is not None:
        values["resp"] = float(row.respiratory_rate)
    return values


def _center_scale(values: list[float]) -> tuple[float, float, str] | None:
    center = median(values)
    mad = median(abs(value - center) for value in values)
    if mad > 0:
        return center, 1.4826 * mad, "MAD"
    if len(values) >= 2:
        sd = stdev(values)
        if sd > 0:
            return center, sd, "SD_FALLBACK"
    return None


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    work = [row[:] + [1.0 if i == j else 0.0 for j in range(size)] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) < 1e-12:
            raise ValueError("singular covariance")
        work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        work[column] = [value / divisor for value in work[column]]
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            work[row] = [
                work[row][index] - factor * work[column][index]
                for index in range(size * 2)
            ]
    return [row[size:] for row in work]


def _covariance(z_rows: list[list[float]]) -> list[list[float]]:
    size = len(z_rows[0])
    means = [sum(row[index] for row in z_rows) / len(z_rows) for index in range(size)]
    covariance = [
        [
            sum((row[i] - means[i]) * (row[j] - means[j]) for row in z_rows)
            / max(len(z_rows) - 1, 1)
            for j in range(size)
        ]
        for i in range(size)
    ]
    for index in range(size):
        covariance[index][index] += 0.1
    for i in range(size):
        for j in range(i):
            denominator = sqrt(max(covariance[i][i] * covariance[j][j], 1e-12))
            correlation = max(-0.95, min(0.95, covariance[i][j] / denominator))
            covariance[i][j] = covariance[j][i] = correlation * denominator
    return covariance


def _quadratic(values: list[float], inverse: list[list[float]]) -> float:
    return max(
        0.0,
        sum(
            values[i] * inverse[i][j] * values[j]
            for i in range(len(values))
            for j in range(len(values))
        ),
    )


def _base(target: date, row: OpenHealthObservation | None, revision: int) -> dict[str, Any]:
    return {
        "algorithm_id": "open_health.anomaly",
        "version": "1.0",
        "upstream_revision": UPSTREAM_REVISION,
        "shadow_only": True,
        "tier": "refused",
        "inputs_used": ["nightly.rmssd_ms", "nightly.rhr_bpm", "nightly.respiratory_rate"],
        "coverage": {"window_days": 28, "minimum_baseline_nights": 10, "persistence_days": 2},
        "confidence": ConfidenceBand.NONE,
        "drivers": [],
        "provenance": [OpenHealthProvenance(
            source=row.source if row else "unknown",
            source_scope=row.source_scope if row else "nightly_observation",
            device_id=row.device_id if row else None,
            module="vitalis.intelligence.open_health.anomaly",
            algorithm="robust_mahalanobis",
            upstream_revision=UPSTREAM_REVISION,
        )],
        "profile_revision_used": revision,
    }


def _insight(base: dict[str, Any], **updates: Any) -> OpenHealthInsights:
    values = dict(base)
    values.update(updates)
    return OpenHealthInsights(**values)


def compute_anomaly(
    observations: Iterable[OpenHealthObservation | dict[str, Any]],
    *,
    target_date: date | None = None,
    profile_revision_used: int = 0,
) -> OpenHealthInsights:
    rows = sorted_observations(list(observations))
    target = target_date or (rows[-1].date if rows else date.today())
    target_row = next((row for row in reversed(rows) if row.date == target), None)
    base = _base(target, target_row, profile_revision_used)
    empty = AnomalyInsight(target_date=target)
    if target_row is None:
        return _insight(
            base,
            status=OpenHealthStatus.REFUSED,
            note="目标夜缺少观测。",
            refusal_reason=OpenHealthRefusalReason(
                code="MISSING_TARGET_NIGHT",
                detail="异常检测需要目标夜观测。",
                missing_inputs=["target_night"],
            ),
            payload=empty,
        )

    dated = [row for row in rows if row.date <= target and _components(row)]
    suffix = [dated[-1]] if dated and dated[-1].date == target else []
    for row in reversed(dated[:-1]):
        if suffix and suffix[0].date - row.date == timedelta(days=1):
            suffix.insert(0, row)
        else:
            break
    current = suffix[-2:]
    if len(current) < 2:
        return _insight(
            base,
            status=OpenHealthStatus.REFUSED,
            note="需要连续两个自然日才能确认异常。",
            refusal_reason=OpenHealthRefusalReason(
                code="INSUFFICIENT_ANOMALY_PERSISTENCE",
                detail="单日 candidate 不进入报告提醒。",
                missing_inputs=["consecutive_current_nights>=2"],
            ),
            payload=empty,
        )

    start = current[0].date
    baseline_rows = [row for row in dated if start - timedelta(days=28) <= row.date < start]
    common_dimensions = set(_components(current[0])) & set(_components(current[1]))
    candidates = [dimension for dimension in DIMENSIONS if dimension in common_dimensions]
    stats: dict[str, tuple[float, float, str]] = {}
    counts: dict[str, int] = {}
    for dimension in candidates:
        values = [
            _components(row)[dimension]
            for row in baseline_rows
            if dimension in _components(row)
        ]
        counts[dimension] = len(values)
        if len(values) >= 10:
            estimate = _center_scale(values)
            if estimate is not None:
                stats[dimension] = estimate
    dimensions = [dimension for dimension in candidates if dimension in stats]
    if len(dimensions) < 2:
        return _insight(
            base,
            status=OpenHealthStatus.REFUSED,
            note="可标准化的夜间信号少于两维。",
            refusal_reason=OpenHealthRefusalReason(
                code="INSUFFICIENT_ANOMALY_DIMENSIONS",
                detail="至少两维需要 10 个基线夜且离散度非零。",
                missing_inputs=[f"baseline_{dimension}_n>=10" for dimension in candidates if dimension not in stats],
            ),
            coverage={**base["coverage"], "baseline_counts": counts},
            payload=empty,
        )

    complete_baseline = [
        row for row in baseline_rows
        if all(dimension in _components(row) for dimension in dimensions)
    ]
    if len(complete_baseline) < 10:
        return _insight(
            base,
            status=OpenHealthStatus.REFUSED,
            note="完整多维基线夜数不足。",
            refusal_reason=OpenHealthRefusalReason(
                code="INSUFFICIENT_COMPLETE_BASELINE",
                detail="协方差估计至少需要 10 个完整基线夜。",
                missing_inputs=["complete_baseline_nights>=10"],
            ),
            coverage={**base["coverage"], "baseline_counts": counts, "complete_baseline_nights": len(complete_baseline)},
            payload=empty,
        )

    z_baseline = [
        [
            (_components(row)[dimension] - stats[dimension][0]) / stats[dimension][1]
            for dimension in dimensions
        ]
        for row in complete_baseline
    ]
    try:
        inverse = _inverse(_covariance(z_baseline))
    except ValueError:
        return _insight(
            base,
            status=OpenHealthStatus.REFUSED,
            note="多维协方差不可逆。",
            refusal_reason=OpenHealthRefusalReason(
                code="SINGULAR_ANOMALY_COVARIANCE",
                detail="无法建立稳定的多信号协方差。",
            ),
            payload=empty,
        )

    scores: list[float] = []
    target_z: dict[str, float] = {}
    for row in current:
        z_values = [
            (_components(row)[dimension] - stats[dimension][0]) / stats[dimension][1]
            for dimension in dimensions
        ]
        scores.append(_quadratic(z_values, inverse))
        if row.date == target:
            target_z = {
                dimension: round(abs(z_values[index]), 6)
                for index, dimension in enumerate(dimensions)
            }
    baseline_n = len(complete_baseline)
    threshold = CHI_SQUARE_999[len(dimensions)] * (1.0 + 2.0 / sqrt(baseline_n))
    streak = sum(score > threshold for score in scores)
    flagged = streak == 2
    older = [row for row in dated if row.date < current[0].date]
    gap_reset = bool(older and current[0].date - older[-1].date > timedelta(days=1))
    drivers = [
        f"{dimension}:|z|={target_z[dimension]:.3f}"
        for dimension in sorted(target_z, key=target_z.get, reverse=True)
    ]
    return _insight(
        base,
        status=OpenHealthStatus.AVAILABLE,
        tier="trusted" if baseline_n >= 14 else "provisional",
        confidence=min(1.0, baseline_n / 28.0),
        drivers=drivers,
        coverage={
            **base["coverage"],
            "dimensions": dimensions,
            "baseline_counts": counts,
            "complete_baseline_nights": baseline_n,
            "ridge": 0.1,
            "correlation_clamp": 0.95,
            "chi_square_probability": 0.999,
        },
        note="仅表示偏离个人常态，不构成疾病诊断。",
        payload=AnomalyInsight(
            target_date=target,
            dimensions=dimensions,
            score=scores[-1],
            threshold=threshold,
            flagged=flagged,
            streak_days=streak,
            gap_reset=gap_reset,
            dimension_scores=target_z,
        ),
    )


detect_anomalies = compute_anomaly
compute_anomalies = compute_anomaly
