"""Shared bounded projections for Open Health period profiles."""

from __future__ import annotations

from datetime import date

from vitalis.intelligence.contracts import (
    OpenHealthBundle,
    OpenHealthCoverage,
    OpenHealthPeriodSummary,
    OpenHealthStatus,
)


def bundle_insights(bundle: OpenHealthBundle | None):
    if bundle is None:
        return []
    return [
        insight
        for insight in (
            bundle.readiness,
            bundle.anomaly,
            bundle.sleep,
            bundle.training_load,
        )
        if insight is not None
    ]


def aggregate_status(bundle: OpenHealthBundle | None) -> OpenHealthStatus:
    statuses = [insight.status for insight in bundle_insights(bundle)]
    if statuses and all(status == OpenHealthStatus.AVAILABLE for status in statuses):
        return OpenHealthStatus.AVAILABLE
    if any(
        status in {OpenHealthStatus.AVAILABLE, OpenHealthStatus.PARTIAL}
        for status in statuses
    ):
        return OpenHealthStatus.PARTIAL
    return OpenHealthStatus.REFUSED


def coverage_summary(
    bundle: OpenHealthBundle | None, *, period_days: int
) -> dict[str, OpenHealthCoverage]:
    if bundle is None:
        return {}
    output: dict[str, OpenHealthCoverage] = {}
    for name, insight in (
        ("readiness", bundle.readiness),
        ("anomaly", bundle.anomaly),
        ("sleep", bundle.sleep),
        ("training_load", bundle.training_load),
    ):
        if insight is None:
            continue
        coverage = insight.coverage or {}
        if name == "readiness":
            observed = int(coverage.get("prior_nights", 0) or 0)
            required = 7
            window = 7
        elif name == "anomaly":
            observed = int(coverage.get("complete_baseline_nights", 0) or 0)
            required = 10
            window = 28
        elif name == "sleep":
            regularity = getattr(insight.payload, "regularity", []) or []
            desired_window = 7 if period_days <= 7 else 28
            selected = next(
                (item for item in regularity if item.window_days == desired_window),
                None,
            )
            observed = selected.available_nights if selected else 0
            required = selected.minimum_nights if selected else (5 if desired_window == 7 else 14)
            window = desired_window
        else:
            observed = int(coverage.get("covered_days", 0) or 0)
            required = int(coverage.get("calendar_days", 42) or 42)
            window = required
        ratio = min(1.0, observed / required) if required else 0.0
        output[name] = OpenHealthCoverage(
            window_days=window,
            observed_days=observed,
            required_days=required,
            ratio=max(0.0, ratio),
            status=insight.status,
        )
    return output


def period_summary(
    bundle: OpenHealthBundle | None, period_start: date, period_end: date
) -> OpenHealthPeriodSummary | None:
    if bundle is None:
        return None
    readiness = bundle.readiness.payload if bundle.readiness else None
    anomaly = bundle.anomaly.payload if bundle.anomaly else None
    drivers = []
    state = getattr(readiness, "state", None)
    label = {
        "suppressed": "目标夜 RMSSD 相对近期个人范围偏低",
        "normal": "目标夜 RMSSD 接近近期个人范围",
        "elevated": "目标夜 RMSSD 相对近期个人范围偏高",
    }.get(state)
    if label:
        drivers.append(label)
    if getattr(anomaly, "flagged", False):
        drivers.append("目标夜多个生理信号连续偏离个人常态")
    refusals = [
        insight.refusal_reason
        for insight in bundle_insights(bundle)
        if insight.refusal_reason is not None
    ][:4]
    return OpenHealthPeriodSummary(
        period_start=period_start,
        period_end=period_end,
        period_days=(period_end - period_start).days + 1,
        target_date=bundle.target_date,
        status=aggregate_status(bundle),
        drivers=drivers[:4],
        refusal_reasons=refusals,
    )
