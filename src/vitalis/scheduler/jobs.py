"""Scheduled durable synchronization and profile jobs."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from vitalis.config import settings

log = logging.getLogger("vitalis.scheduler")


def _get_authorized_users() -> set[str]:
    from sqlalchemy import select

    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.models import (
        AuthToken as OrmAuthToken,
    )
    from vitalis.adapters.persistence.models import (
        SourceAccount as OrmSourceAccount,
    )
    from vitalis.adapters.persistence.models import (
        User as OrmUser,
    )

    statement = (
        select(OrmSourceAccount.user_id)
        .join(
            OrmAuthToken,
            OrmAuthToken.source_account_id == OrmSourceAccount.id,
        )
        .join(OrmUser, OrmUser.id == OrmSourceAccount.user_id)
        .where(
            OrmSourceAccount.source == "zepp",
            OrmSourceAccount.status == "active",
            OrmSourceAccount.revoked_at.is_(None),
            OrmAuthToken.access_token.is_not(None),
            OrmAuthToken.access_token != "",
        )
        .distinct()
    )
    with session_scope() as db:
        return set(db.execute(statement).scalars())


def _create_attempt(user_id: str, days: int, trigger: str):
    from vitalis.bootstrap import get_connector
    connector = get_connector("zepp")
    create = getattr(connector, "create_attempt", None)
    if create is None:
        return None
    return create(user_id, days=days, trigger=trigger, trigger_ref=user_id)


def _sync_user(user_id: str, days: int, label: str) -> str | None:
    """Create a ledger entry only; the dispatcher owns network execution."""
    attempt = _create_attempt(user_id, days, trigger=label)
    attempt_id = attempt.id if attempt is not None else None
    log.info("%s queued: user=%s attempt=%s", label, user_id, attempt_id)
    return attempt_id


def nightly_sync_job() -> None:
    """Create the configured nightly attempts."""
    for user_id in _get_authorized_users():
        try:
            _sync_user(user_id, days=7, label="nightly")
        except Exception:
            log.exception("nightly sync enqueue failed: user=%s", user_id)
    dispatcher_job()


def _profile_push_job(period: str, sync_days: int) -> None:
    """Queue the morning/evening attempt at its existing local time."""
    for user_id in _get_authorized_users():
        try:
            _sync_user(user_id, days=sync_days, label=period)
        except Exception:
            log.exception("%s sync enqueue failed: user=%s", period, user_id)
    dispatcher_job()


def morning_analysis_job() -> None:
    _profile_push_job("morning", sync_days=2)


def evening_analysis_job() -> None:
    _profile_push_job("evening", sync_days=1)


def worker_heartbeat_job() -> None:
    """Record worker liveness independently of a potentially slow sync pass."""
    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.models import WorkerHeartbeat

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, "primary")
        if row is None:
            db.add(WorkerHeartbeat(name="primary", last_seen_at=now))
        else:
            row.last_seen_at = now


MAX_NOTIFICATION_ATTEMPTS = 3
NOTIFICATION_LEASE_SECONDS = 300


def drain_notification_deliveries(*, max_deliveries: int = 1) -> int:
    """Claim saved report intents and deliver them without recomputing health data."""
    from vitalis.adapters.daily_push import DailyPushDeliveryError, deliver_daily_report
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.models import NotificationDelivery
    from vitalis.time import local_day_utc_bounds

    if not isinstance(max_deliveries, int) or max_deliveries < 1:
        raise ValueError("max_deliveries must be positive")
    drained = 0
    for _ in range(max_deliveries):
        with session_scope() as db:
            row = HealthRepository(db).claim_notification_delivery(
                lease_seconds=NOTIFICATION_LEASE_SECONDS,
                max_attempts=MAX_NOTIFICATION_ATTEMPTS,
            )
            if row is None:
                break
            delivery = {
                "id": row.id,
                "user_id": row.user_id,
                "analysis_run_id": row.analysis_run_id,
                "period": row.period,
                "target_date": row.target_date,
                "lease_token": row.lease_token,
            }
        status = "failed"
        error = "render_failed"
        next_attempt_at = None
        if not settings.push_user or not settings.pushplus_token or settings.push_user != delivery["user_id"]:
            status = "deferred"
            error = "delivery_disabled"
        else:
            try:
                with session_scope() as db:
                    snapshot = HealthRepository(db).notification_delivery_snapshot(
                        delivery["id"], delivery["user_id"], delivery["analysis_run_id"],
                        delivery["lease_token"],
                    )
                    payload = dict(snapshot.payload) if snapshot is not None else None
                if payload is None:
                    status = "deferred"
                    error = "snapshot_unavailable"
                else:
                    _, plan_expires_at = local_day_utc_bounds(delivery["target_date"])
                    result = deliver_daily_report(
                        delivery["user_id"],
                        settings.pushplus_token,
                        payload,
                        period=delivery["period"],
                        target_date=delivery["target_date"],
                        plan_expires_at=plan_expires_at,
                        scheduled_delivery=True,
                    )
                    if result.get("status") == "deferred":
                        status = "deferred"
                        error = str(result.get("reason") or "stored_data_incomplete")
                    else:
                        status = "succeeded"
                        error = None
            except DailyPushDeliveryError as exc:
                if exc.ambiguous:
                    status = "uncertain"
                    error = "transport_ambiguous"
                else:
                    status = "failed"
                    error = "transport_rejected"
            except Exception:
                status = "failed"
                error = "render_failed"

        if status == "failed":
            with session_scope() as db:
                current = db.get(NotificationDelivery, delivery["id"])
                if current is not None and current.attempt_count < MAX_NOTIFICATION_ATTEMPTS:
                    next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                        seconds=min(3600, 60 * (2 ** max(0, current.attempt_count - 1)))
                    )
        with session_scope() as db:
            HealthRepository(db).complete_notification_delivery(
                delivery["id"], delivery["lease_token"], status,
                error=error, next_attempt_at=next_attempt_at,
            )
        drained += 1
    return drained


def dispatcher_job() -> int:
    """Drain due sync, analysis, and delivery work in one worker-owned pass."""
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
    from vitalis.application.jobs import drain_analysis_jobs
    from vitalis.bootstrap import configure_analysis_jobs, get_connector

    configure_analysis_jobs()
    analysis_drained = drain_analysis_jobs(max_jobs=1)
    if analysis_drained:
        log.info("analysis dispatcher drained jobs=%s", analysis_drained)
    coordinator = ZeppSyncCoordinator(
        connector=get_connector("zepp"),
        lease_seconds=getattr(settings, "sync_lease_seconds", 120),
        attempt_lease_seconds=getattr(settings, "sync_attempt_lease_seconds", 300),
    )
    drained = 0
    # A bounded, fair pass prevents one backlog from monopolizing the worker.
    for _ in range(max(1, settings.sync_dispatcher_batch_chunks)):
        report = coordinator.drain_once()
        if report is None:
            break
        drained += 1
    if drained:
        log.info("sync dispatcher drained chunks=%s", drained)
    notification_drained = drain_notification_deliveries(max_deliveries=1)
    if notification_drained:
        log.info("notification dispatcher drained deliveries=%s", notification_drained)
    return drained + analysis_drained + notification_drained


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        nightly_sync_job,
        CronTrigger(
            hour=settings.sync_cron_hour, minute=settings.sync_cron_minute,
            timezone=settings.timezone,
        ),
        id="nightly_sync", misfire_grace_time=3600,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        morning_analysis_job,
        CronTrigger(hour=9, minute=30, timezone=settings.timezone),
        id="morning_analysis", misfire_grace_time=1800,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        evening_analysis_job,
        CronTrigger(hour=21, minute=30, timezone=settings.timezone),
        id="evening_analysis", misfire_grace_time=1800,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        dispatcher_job,
        IntervalTrigger(
            seconds=max(1, settings.sync_dispatcher_interval_seconds),
            timezone=settings.timezone,
        ),
        id="sync_dispatcher", misfire_grace_time=60,
        max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        worker_heartbeat_job,
        IntervalTrigger(seconds=30, timezone=settings.timezone),
        id="worker_heartbeat", misfire_grace_time=60,
        max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()
    log.info(
        "scheduler started: nightly %02d:%02d, dispatcher every %ss, timezone=%s",
        settings.sync_cron_hour, settings.sync_cron_minute,
        settings.sync_dispatcher_interval_seconds, settings.timezone,
    )
    return scheduler
