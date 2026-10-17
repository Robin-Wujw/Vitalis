"""Independent delivery health check, run by its own timer outside the worker.

A frozen worker cannot report its own stale heartbeat, so this check runs as a
short-lived process (``vitalis alerts check``). Each incident is stored once
per user. No second channel is configured yet: events stay readable through
``GET /api/delivery-alerts`` and missed reports through ``GET /api/deliveries``.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from vitalis.config import settings

log = logging.getLogger("vitalis.alerts")

EXTERNAL_CHANNEL = "unconfigured"


def delivery_targets(day: date) -> dict[str, datetime]:
    """Alert instants for a report day, including its evening's next-day target."""
    from vitalis.application.delivery_policy import scheduled_report_window

    targets = {}
    for period in ("morning", "evening"):
        window = scheduled_report_window(
            day, period, settings.timezone,
            morning_window=settings.morning_wake_window(),
        )
        targets[period] = datetime.fromisoformat(
            window["delivery_target_at"].replace("Z", "+00:00")
        )
    return targets


def check_delivery_health(*, now: datetime | None = None) -> dict:
    """Store new alert events and return a summary without personal data."""
    from sqlalchemy import select

    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.models import AnalysisRun, NotificationDelivery
    from vitalis.application.delivery_alerts import (
        AnalysisFailureObservation,
        DeliveryObservation,
        ProviderRejectionObservation,
        evaluate_alert_candidates,
        heartbeat_is_stale,
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = current.astimezone(ZoneInfo(settings.timezone)).date()
    owner = settings.push_user if settings.push_user and settings.pushplus_token else None
    created = []
    with session_scope() as db:
        repository = HealthRepository(db)
        user_ids = set(db.execute(select(NotificationDelivery.user_id).distinct()).scalars())
        user_ids.update(db.execute(select(AnalysisRun.user_id).where(
            AnalysisRun.status == "FAILED",
        ).distinct()).scalars())
        if owner:
            user_ids.add(owner)
        heartbeat = repository.worker_heartbeat("primary")
        heartbeat_at = heartbeat.last_seen_at if heartbeat is not None else None
        for user_id in sorted(user_ids):
            if not repository.user_exists(user_id):
                continue
            deliveries = repository.notification_deliveries(user_id, limit=100)
            observations = [
                DeliveryObservation(
                    period=row.period, target_date=row.target_date,
                    status=row.status, last_error=row.last_error,
                    provider_status=row.provider_status, delivery_id=row.id,
                )
                for row in deliveries
            ]
            is_owner = user_id == owner
            missing_cause = (
                "worker_not_running" if heartbeat_is_stale(heartbeat_at, current)
                else "snapshot_unavailable"
                if repository.latest_analysis_snapshot(user_id, "daily", today) is None
                else "delivery_not_created"
            )
            candidates = evaluate_alert_candidates(
                user_id,
                now=current,
                local_day=today,
                targets=delivery_targets(today) if is_owner else None,
                deliveries=observations,
                missing_causes={"morning": missing_cause, "evening": missing_cause},
                heartbeat_at=heartbeat_at if is_owner else None,
                analysis_failures=[
                    AnalysisFailureObservation(
                        run_id=row.id, target_date=row.target_date,
                        failed_at=row.completed_at, error=row.error,
                    )
                    for row in repository.failed_analysis_runs(user_id, limit=100)
                ],
                provider_rejections=[
                    ProviderRejectionObservation(
                        delivery_id=row.id, period=row.period, target_date=row.target_date,
                        provider_status=row.provider_status, rejected_at=row.updated_at,
                    )
                    for row in repository.provider_rejected_deliveries(user_id, limit=100)
                ],
            )
            if is_owner:
                previous_day = today - timedelta(days=1)
                previous_cause = (
                    "worker_not_running" if heartbeat_is_stale(heartbeat_at, current)
                    else "snapshot_unavailable"
                    if repository.latest_analysis_snapshot(user_id, "daily", previous_day) is None
                    else "delivery_not_created"
                )
                candidates += evaluate_alert_candidates(
                    user_id, now=current, local_day=previous_day,
                    targets={"evening": delivery_targets(previous_day)["evening"]},
                    deliveries=observations, missing_causes={"evening": previous_cause},
                )
            for candidate in candidates:
                if repository.delivery_alert(user_id, candidate.event_key) is not None:
                    continue
                repository.create_delivery_alert(
                    user_id,
                    event_key=candidate.event_key,
                    category=candidate.category,
                    reason=candidate.reason,
                    observed_at=candidate.observed_at,
                    period=candidate.period,
                    target_date=candidate.target_date,
                    payload=candidate.payload,
                )
                created.append(candidate)
    for candidate in created:
        log.warning(
            "delivery alert: reason=%s period=%s date=%s cause=%s",
            candidate.reason, candidate.period or "-", candidate.target_date or "-",
            (candidate.payload or {}).get("cause", "-"),
        )
    return {
        "checked_at": current.isoformat().replace("+00:00", "Z"),
        "new_alerts": len(created),
        "reasons": dict(Counter(candidate.reason for candidate in created)),
        "external_channel": EXTERNAL_CHANNEL,
    }
