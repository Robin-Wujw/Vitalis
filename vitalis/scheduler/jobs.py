"""Scheduled durable synchronization and profile jobs."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from vitalis.config import settings

log = logging.getLogger("vitalis.scheduler")


def _get_authorized_users() -> set[str]:
    from sqlalchemy import select
    from vitalis.storage import session_scope
    from vitalis.storage.models import AuthToken as OrmAuthToken

    with session_scope() as db:
        return {row[0] for row in db.execute(select(OrmAuthToken.user_id).distinct())}


def _create_attempt(user_id: str, days: int, trigger: str):
    from vitalis.connectors import get_connector
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


def dispatcher_job() -> int:
    """Drain due attempts; DB leases make concurrent dispatchers safe."""
    from vitalis.connectors import get_connector
    from vitalis.services.zepp_sync_coordinator import ZeppSyncCoordinator

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
    return drained


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
    )
    scheduler.start()
    log.info(
        "scheduler started: nightly %02d:%02d, dispatcher every %ss, timezone=%s",
        settings.sync_cron_hour, settings.sync_cron_minute,
        settings.sync_dispatcher_interval_seconds, settings.timezone,
    )
    return scheduler
