"""Pure delivery-alert evaluation.

The evaluator only creates durable candidates. It never sends mail, Hermes, or
PushPlus traffic; the independent health check decides how they are stored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

AlertCategory = Literal["delivery", "worker", "analysis", "provider"]
AlertReason = Literal[
    "delivery_target_missed",
    "worker_heartbeat_stale",
    "analysis_failed",
    "provider_rejected",
]

DEFAULT_HEARTBEAT_STALE_AFTER = timedelta(minutes=30)
# Older failures stay auditable in their own ledgers; a new check (for example
# the first one after a release) must not turn them into fresh alerts.
RECENT_INCIDENT_WINDOW = timedelta(hours=24)
# Why a report that has no delivery intent at its target was not sent.
MISSING_CAUSES = ("worker_not_running", "snapshot_unavailable", "delivery_not_created")


@dataclass(frozen=True)
class DeliveryObservation:
    """Sanitized delivery state needed by the target evaluator."""

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
    rejected_at: datetime | None = None


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


def _utc_text(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


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


def _is_recent(value: datetime | None, now: datetime) -> bool:
    return value is not None and _as_utc(now) - _as_utc(value) <= RECENT_INCIDENT_WINDOW


def _provider_rejected(status: str | None, last_error: str | None, provider_status: str | None) -> bool:
    """An explicit provider rejection; an uncertain transport is never one."""
    if (last_error or "").strip().lower() == "transport_rejected":
        return True
    return (
        (provider_status or "").strip().lower() in {"rejected", "failed", "error"}
        and status == "failed"
    )


def evaluate_delivery_target(
    user_id: str,
    *,
    period: str,
    target_date: date,
    target_at: datetime,
    now: datetime,
    delivery: DeliveryObservation | None = None,
    missing_cause: str | None = None,
) -> AlertCandidate | None:
    """Raise one event when a report is not accepted by its target time."""
    if _as_utc(now) < _as_utc(target_at):
        return None
    status = delivery.status if delivery else None
    if status in {"accepted", "delivered"}:
        return None
    if delivery is not None and _provider_rejected(status, delivery.last_error, delivery.provider_status):
        # The provider rejection is reported once by its own evaluator.
        return None
    cause = (
        (delivery.last_error or delivery.status) if delivery is not None
        else missing_cause if missing_cause in MISSING_CAUSES else "delivery_not_created"
    )
    return _candidate(
        user_id=user_id,
        event_key=f"delivery:{period}:{target_date.isoformat()}:target-missed",
        category="delivery",
        reason="delivery_target_missed",
        observed_at=now,
        period=period,
        target_date=target_date,
        payload={
            "target_at": _utc_text(target_at),
            "delivery_present": delivery is not None,
            "delivery_status": status,
            "delivery_id": delivery.delivery_id if delivery else None,
            "cause": cause,
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
    return _candidate(
        user_id=user_id,
        # One frozen worker keeps the same last heartbeat: one incident.
        event_key=f"worker:heartbeat-stale:{_as_utc(last_seen_at).isoformat()}",
        category="worker",
        reason="worker_heartbeat_stale",
        observed_at=now,
        payload={
            "last_seen_at": _utc_text(last_seen_at),
            "age_seconds": round(age, 3) if age is not None else None,
            "stale_after_seconds": stale_after.total_seconds(),
        },
    )


def evaluate_analysis_failure(
    user_id: str, failure: AnalysisFailureObservation, *, now: datetime,
) -> AlertCandidate | None:
    """Evaluate one recent failed analysis run using only a stable error category."""
    if not _is_recent(failure.failed_at, now):
        return None
    safe_error = failure.error if failure.error in {
        "analysis_failed", "analysis_input_changed", "analysis_attempt_limit",
    } else None
    return _candidate(
        user_id=user_id,
        event_key=f"analysis:{failure.run_id}:failed",
        category="analysis",
        reason="analysis_failed",
        observed_at=failure.failed_at or now,
        target_date=failure.target_date,
        payload={"analysis_run_id": failure.run_id, "error": safe_error},
    )


def evaluate_provider_rejection(
    user_id: str, rejection: ProviderRejectionObservation, *, now: datetime,
) -> AlertCandidate | None:
    """Evaluate one recent explicit provider rejection."""
    if not _is_recent(rejection.rejected_at, now):
        return None
    status = (rejection.provider_status or "").strip().lower()
    return _candidate(
        user_id=user_id,
        event_key=f"provider:{rejection.delivery_id}:rejected",
        category="provider",
        reason="provider_rejected",
        observed_at=now,
        period=rejection.period,
        target_date=rejection.target_date,
        payload={
            "delivery_id": rejection.delivery_id,
            "provider_status": status if status in {"rejected", "failed", "error"} else None,
        },
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
    local_day: date,
    targets: Mapping[str, datetime] | None = None,
    deliveries: Iterable[DeliveryObservation] = (),
    missing_causes: Mapping[str, str] | None = None,
    heartbeat_at: datetime | None = None,
    analysis_failures: Iterable[AnalysisFailureObservation] = (),
    provider_rejections: Iterable[ProviderRejectionObservation] = (),
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
) -> tuple[AlertCandidate, ...]:
    """Evaluate all alert conditions for one user and deduplicate by event key.

    ``targets`` maps today's report periods to their alert instants; only the
    delivery owner has targets. Older report periods remain auditable in their
    delivery rows and do not create a new alert on every check.
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    current = _as_utc(now)
    candidates: dict[str, AlertCandidate] = {}
    by_period = {
        item.period: item for item in deliveries
        if item.target_date == local_day
    }
    for period, target_at in (targets or {}).items():
        candidate = evaluate_delivery_target(
            user_id, period=period, target_date=local_day, target_at=target_at,
            now=current, delivery=by_period.get(period),
            missing_cause=(missing_causes or {}).get(period),
        )
        if candidate is not None:
            candidates[candidate.event_key] = candidate
    heartbeat = evaluate_stale_heartbeat(
        user_id, last_seen_at=heartbeat_at, now=current, stale_after=heartbeat_stale_after,
    )
    if heartbeat is not None:
        candidates[heartbeat.event_key] = heartbeat
    for failure in analysis_failures:
        candidate = evaluate_analysis_failure(user_id, failure, now=current)
        if candidate is not None:
            candidates[candidate.event_key] = candidate
    for rejection in provider_rejections:
        candidate = evaluate_provider_rejection(user_id, rejection, now=current)
        if candidate is not None:
            candidates[candidate.event_key] = candidate
    return tuple(candidates.values())
