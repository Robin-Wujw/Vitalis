"""Pure internal delivery-alert evaluation.

The evaluator only creates durable candidates. It never sends mail, Hermes, or
PushPlus traffic. Persistence and the worker decide how candidates are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal
from zoneinfo import ZoneInfo


AlertCategory = Literal["delivery", "worker", "analysis", "provider"]
AlertReason = Literal[
    "delivery_target_missed",
    "worker_heartbeat_stale",
    "analysis_failed",
    "provider_rejected",
]

DELIVERY_TARGETS = {"morning": (10, 30), "evening": (22, 30)}
DEFAULT_HEARTBEAT_STALE_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class DeliveryObservation:
    """Sanitized delivery state needed by the pure target evaluator."""

    period: str
    target_date: date
    status: str | None
    last_error: str | None = None
    provider_status: str | None = None
    delivery_id: str | None = None


@dataclass(frozen=True)
class AnalysisFailureObservation:
    """One persisted failed analysis run."""

    run_id: str
    target_date: date
    failed_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class ProviderRejectionObservation:
    """One persisted provider rejection, without transport payloads."""

    delivery_id: str
    period: str
    target_date: date
    provider_status: str | None = None


@dataclass(frozen=True)
class AlertCandidate:
    """One idempotent alert event ready for repository persistence."""

    user_id: str
    event_key: str
    category: AlertCategory
    reason: AlertReason
    observed_at: datetime
    period: str | None = None
    target_date: date | None = None
    payload: dict[str, object] | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def delivery_target_at(
    target_date: date, period: str, timezone_name: str
) -> datetime:
    """Return the configured local 10:30/22:30 target as an aware UTC instant."""
    try:
        hour, minute = DELIVERY_TARGETS[period]
    except KeyError as exc:
        raise ValueError(f"unsupported delivery period: {period}") from exc
    local = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=ZoneInfo(timezone_name),
    )
    return local.astimezone(timezone.utc)


def delivery_target_reached(
    now: datetime, target_date: date, period: str, timezone_name: str
) -> bool:
    """Return whether the delivery target has passed."""
    return _as_utc(now) >= delivery_target_at(target_date, period, timezone_name)


def heartbeat_age_seconds(last_seen_at: datetime | None, now: datetime) -> float | None:
    """Return non-negative heartbeat age, or ``None`` when never observed."""
    if last_seen_at is None:
        return None
    return max(0.0, (_as_utc(now) - _as_utc(last_seen_at)).total_seconds())


def heartbeat_is_stale(
    last_seen_at: datetime | None,
    now: datetime,
    *,
    stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
) -> bool:
    """Return whether a known heartbeat is older than the conservative threshold."""
    age = heartbeat_age_seconds(last_seen_at, now)
    return age is not None and age > max(0.0, stale_after.total_seconds())


def analysis_failure_reason(status: str, error: str | None = None) -> str | None:
    """Map persisted analysis state to its stable alert reason."""
    return "analysis_failed" if status.upper() == "FAILED" or status == "failed" else None


def provider_rejection_reason(
    status: str, last_error: str | None, provider_status: str | None = None
) -> str | None:
    """Map explicit provider rejection state without treating uncertainty as rejection."""
    normalized_error = (last_error or "").strip().lower()
    normalized_provider = (provider_status or "").strip().lower()
    if normalized_error == "transport_rejected" or (
        normalized_provider in {"rejected", "failed", "error"}
        and status in {"failed", "uncertain"}
    ):
        return "provider_rejected"
    return None


def evaluate_delivery_target(
    user_id: str,
    *,
    period: str,
    target_date: date,
    status: str | None,
    now: datetime,
    timezone_name: str,
    last_error: str | None = None,
    provider_status: str | None = None,
    delivery_id: str | None = None,
) -> AlertCandidate | None:
    """Evaluate one delivery target, returning no event before or after success."""
    if not delivery_target_reached(now, target_date, period, timezone_name):
        return None
    if status in {"accepted", "delivered"}:
        return None
    if provider_rejection_reason(status or "", last_error, provider_status):
        return _candidate(
            user_id=user_id,
            event_key=(
                f"provider:{delivery_id or period + ':' + target_date.isoformat()}:rejected"
            ),
            category="provider",
            reason="provider_rejected",
            observed_at=now,
            period=period,
            target_date=target_date,
            payload={
                "delivery_id": delivery_id,
                "provider_status": provider_status,
            },
        )
    event_key = f"delivery:{period}:{target_date.isoformat()}:target-missed"
    return _candidate(
        user_id=user_id,
        event_key=event_key,
        category="delivery",
        reason="delivery_target_missed",
        observed_at=now,
        period=period,
        target_date=target_date,
        payload={
            "target_at": delivery_target_at(
                target_date, period, timezone_name
            ).isoformat().replace("+00:00", "Z"),
            "delivery_present": delivery_id is not None,
            "delivery_status": status,
        },
    )


def evaluate_stale_heartbeat(
    user_id: str,
    *,
    last_seen_at: datetime | None,
    now: datetime,
    stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
) -> AlertCandidate | None:
    """Evaluate the known worker heartbeat without treating never-seen as age zero."""
    if not heartbeat_is_stale(last_seen_at, now, stale_after=stale_after):
        return None
    age = heartbeat_age_seconds(last_seen_at, now)
    marker = _as_utc(last_seen_at).isoformat() if last_seen_at else "missing"
    event_key = f"worker:heartbeat-stale:{marker}"
    return _candidate(
        user_id=user_id,
        event_key=event_key,
        category="worker",
        reason="worker_heartbeat_stale",
        observed_at=now,
        payload={
            "last_seen_at": _as_utc(last_seen_at).isoformat().replace("+00:00", "Z")
            if last_seen_at else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "stale_after_seconds": stale_after.total_seconds(),
        },
    )


def evaluate_analysis_failure(
    user_id: str,
    *,
    run_id: str,
    target_date: date,
    status: str,
    now: datetime,
    failed_at: datetime | None = None,
    error: str | None = None,
) -> AlertCandidate | None:
    """Evaluate one failed analysis run using only a stable error category."""
    if analysis_failure_reason(status, error) is None:
        return None
    safe_error = error if error in {
        "analysis_failed", "analysis_input_changed", "analysis_attempt_limit",
    } else None
    return _candidate(
        user_id=user_id,
        event_key=f"analysis:{run_id}:failed",
        category="analysis",
        reason="analysis_failed",
        observed_at=failed_at or now,
        target_date=target_date,
        payload={"analysis_run_id": run_id, "error": safe_error},
    )


def evaluate_provider_rejection(
    user_id: str,
    *,
    delivery_id: str,
    period: str,
    target_date: date,
    status: str,
    last_error: str | None,
    now: datetime,
    provider_status: str | None = None,
) -> AlertCandidate | None:
    """Evaluate an explicit provider rejection, excluding uncertain transport."""
    if provider_rejection_reason(status, last_error, provider_status) is None:
        return None
    safe_status = (
        provider_status.strip().lower()
        if provider_status and provider_status.strip().lower()
        in {"rejected", "failed", "error"}
        else None
    )
    return _candidate(
        user_id=user_id,
        event_key=f"provider:{delivery_id}:rejected",
        category="provider",
        reason="provider_rejected",
        observed_at=now,
        period=period,
        target_date=target_date,
        payload={"delivery_id": delivery_id, "provider_status": safe_status},
    )


def _candidate(
    *,
    user_id: str,
    event_key: str,
    category: AlertCategory,
    reason: AlertReason,
    observed_at: datetime,
    period: str | None = None,
    target_date: date | None = None,
    payload: dict[str, object] | None = None,
) -> AlertCandidate:
    return AlertCandidate(
        user_id=user_id,
        event_key=event_key,
        category=category,
        reason=reason,
        observed_at=_as_utc(observed_at),
        period=period,
        target_date=target_date,
        payload=payload or {},
    )


def evaluate_alert_candidates(
    user_id: str,
    *,
    now: datetime,
    timezone_name: str,
    deliveries: Iterable[DeliveryObservation] = (),
    heartbeat_at: datetime | None = None,
    analysis_failures: Iterable[AnalysisFailureObservation] = (),
    provider_rejections: Iterable[ProviderRejectionObservation] = (),
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
    include_delivery_targets: bool = True,
) -> tuple[AlertCandidate, ...]:
    """Evaluate all internal alert conditions and deduplicate by event key.

    The target check is intentionally bounded to the current local day. Older
    report periods remain auditable in their delivery rows and do not create a
    new alert on every worker restart.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    current = _as_utc(now)
    local_day = current.astimezone(ZoneInfo(timezone_name)).date()
    candidates: dict[str, AlertCandidate] = {}

    by_period = {
        (item.period, item.target_date): item
        for item in deliveries
        if item.period in DELIVERY_TARGETS
    }
    if include_delivery_targets:
        for period in DELIVERY_TARGETS:
            delivery = by_period.get((period, local_day))
            candidate = evaluate_delivery_target(
                user_id,
                period=period,
                target_date=local_day,
                status=delivery.status if delivery else None,
                last_error=delivery.last_error if delivery else None,
                provider_status=delivery.provider_status if delivery else None,
                delivery_id=delivery.delivery_id if delivery else None,
                now=current,
                timezone_name=timezone_name,
            )
            if candidate is not None:
                candidates[candidate.event_key] = candidate

    heartbeat_candidate = evaluate_stale_heartbeat(
        user_id,
        last_seen_at=heartbeat_at,
        now=current,
        stale_after=heartbeat_stale_after,
    )
    if heartbeat_candidate is not None:
        candidates[heartbeat_candidate.event_key] = heartbeat_candidate

    for failure in analysis_failures:
        candidate = evaluate_analysis_failure(
            user_id,
            run_id=failure.run_id,
            target_date=failure.target_date,
            status="FAILED",
            failed_at=failure.failed_at,
            error=failure.error,
            now=current,
        )
        if candidate is not None:
            candidates[candidate.event_key] = candidate

    for rejection in provider_rejections:
        candidate = evaluate_provider_rejection(
            user_id,
            delivery_id=rejection.delivery_id,
            period=rejection.period,
            target_date=rejection.target_date,
            status="failed",
            last_error="transport_rejected",
            provider_status=rejection.provider_status,
            now=current,
        )
        if candidate is not None:
            candidates[candidate.event_key] = candidate

    return tuple(candidates.values())
