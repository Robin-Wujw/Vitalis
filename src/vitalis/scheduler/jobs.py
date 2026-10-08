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
    """Queue one scheduled sync whose terminal state creates the report job."""
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


def weekly_analysis_job() -> None:
    _profile_push_job("weekly", sync_days=8)


def monthly_analysis_job() -> None:
    _profile_push_job("monthly", sync_days=2)


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
    """Claim durable intents and complete sends; accepted rows are only polled."""
    from vitalis.adapters.daily_push import DailyPushDeliveryError, _provider_result
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.models import NotificationDelivery

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
                "id": row.id, "user_id": row.user_id,
                "analysis_run_id": row.analysis_run_id, "period": row.period,
                "target_date": row.target_date, "lease_token": row.lease_token,
                "send_attempt_id": row.send_attempt_id,
            }
        status = "failed"
        error = "render_failed"
        next_attempt_at = None
        render = {}
        provider = {}
        if (
            not settings.push_user or not settings.pushplus_token
            or settings.push_user != delivery["user_id"]
            or (delivery["period"] == "weekly" and not settings.weekly_report_enabled)
            or (delivery["period"] == "monthly" and not settings.monthly_report_enabled)
        ):
            status, error = "deferred", "delivery_disabled"
        else:
            try:
                with session_scope() as db:
                    snapshot = HealthRepository(db).notification_delivery_snapshot(
                        delivery["id"], delivery["user_id"], delivery["analysis_run_id"],
                        delivery["lease_token"],
                    )
                    payload = dict(snapshot.payload) if snapshot is not None else None
                if payload is None:
                    status, error = "deferred", "snapshot_unavailable"
                else:
                    from vitalis.adapters import daily_push
                    if delivery["period"] in {"weekly", "monthly"}:
                        try:
                            service = daily_push.PushService(
                                pushplus_token=settings.pushplus_token,
                                pushplus_access_key=settings.pushplus_access_key,
                                send_attempt_id=delivery["send_attempt_id"],
                            )
                        except TypeError:
                            service = daily_push.PushService(pushplus_token=settings.pushplus_token)
                        result = (
                            service.push_weekly_profile(delivery["user_id"], payload)
                            if delivery["period"] == "weekly"
                            else service.push_monthly_profile(delivery["user_id"], payload)
                        )
                    else:
                        # Preserve the shared date, sleep, history, and plan-expiry
                        # gates while the scheduler remains the lease owner.
                        result = daily_push.deliver_daily_report(
                            delivery["user_id"], settings.pushplus_token, payload,
                            period=delivery["period"], target_date=delivery["target_date"],
                            scheduled_delivery=True, send_attempt_id=delivery["send_attempt_id"],
                        )
                    if result.get("status") == "deferred":
                        status, error = "deferred", result.get("reason", "render_failed")
                    else:
                        provider = _provider_result(result)
                        render = result.get("_render") or {}
                        status = provider["status"]
                        error = None if status in {"accepted", "delivered"} else (
                            "transport_ambiguous" if status == "uncertain" else "transport_rejected"
                        )
            except DailyPushDeliveryError as exc:
                status = "uncertain" if exc.ambiguous else "failed"
                error = "transport_ambiguous" if exc.ambiguous else "transport_rejected"
            except Exception:
                status, error = "failed", "render_failed"

        if status == "failed":
            with session_scope() as db:
                current = db.get(NotificationDelivery, delivery["id"])
                if current is not None and current.attempt_count < MAX_NOTIFICATION_ATTEMPTS:
                    next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                        seconds=min(3600, 60 * (2 ** max(0, current.attempt_count - 1)))
                    )
        next_poll_at = None
        if status == "accepted" and provider.get("provider_id") and getattr(settings, "pushplus_access_key", ""):
            next_poll_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=getattr(settings, "pushplus_query_interval_seconds", 60)
            )
        with session_scope() as db:
            HealthRepository(db).complete_notification_delivery(
                delivery["id"], delivery["lease_token"], status,
                error=error, next_attempt_at=next_attempt_at, next_poll_at=next_poll_at,
                provider_id=provider.get("provider_id"), provider_status=provider.get("provider_status"),
                template=render.get("template"), renderer_version=render.get("renderer_version"),
                content_sha256=render.get("content_sha256"), send_attempt_id=delivery["send_attempt_id"],
            )
        drained += 1
    return drained


def drain_notification_polls(*, max_polls: int = 1) -> int:
    """Query accepted PushPlus messages without ever re-POSTing them."""
    from vitalis.adapters.notifications import PushService
    from vitalis.adapters.persistence import HealthRepository, session_scope

    if not isinstance(max_polls, int) or max_polls < 1:
        raise ValueError("max_polls must be positive")
    if not settings.push_user or not settings.pushplus_token or not getattr(settings, "pushplus_access_key", ""):
        return 0
    drained = 0
    for _ in range(max_polls):
        with session_scope() as db:
            row = HealthRepository(db).claim_notification_poll(
                user_id=settings.push_user,
                lease_seconds=NOTIFICATION_LEASE_SECONDS,
                max_attempts=getattr(settings, "pushplus_query_max_attempts", MAX_NOTIFICATION_ATTEMPTS),
            )
            if row is None:
                break
            info = {
                "id": row.id, "token": row.lease_token, "provider_id": row.provider_id,
                "poll_attempt_id": row.poll_attempt_id,
            }
        result = PushService(
            pushplus_token=settings.pushplus_token,
            pushplus_access_key=getattr(settings, "pushplus_access_key", ""),
        ).query_pushplus(info["provider_id"], poll_attempt_id=info["poll_attempt_id"])
        status = result.get("status", "accepted")
        next_poll_at = None
        next_attempt_at = None
        if status == "accepted":
            next_poll_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=getattr(settings, "pushplus_query_interval_seconds", 60)
            )
        elif status == "failed":
            with session_scope() as db:
                current = db.get(NotificationDelivery, info["id"])
                if current is not None and current.attempt_count < MAX_NOTIFICATION_ATTEMPTS:
                    next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
                        seconds=min(3600, 60 * (2 ** max(0, current.attempt_count - 1)))
                    )
        with session_scope() as db:
            HealthRepository(db).complete_notification_delivery(
                info["id"], info["token"], status,
                error=("transport_ambiguous" if status == "uncertain" else None),
                next_attempt_at=next_attempt_at, next_poll_at=next_poll_at,
                provider_id=info["provider_id"], provider_status=result.get("provider_status"),
                poll_attempt_id=info["poll_attempt_id"],
            )
        drained += 1
    return drained


def reconcile_deferred_daily_reports() -> int:
    """Recover an existing unsent daily intent once per changed input snapshot."""
    import hashlib

    from sqlalchemy import select

    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.models import AnalysisJob, NotificationDelivery, User
    from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
    from vitalis.application.delivery_policy import stored_profile_is_usable
    from vitalis.application.jobs import create_analysis_job
    from vitalis.time import local_today

    if not settings.push_user or not settings.pushplus_token:
        return 0
    today = local_today()
    with session_scope() as db:
        repo = HealthRepository(db)
        owner = db.get(User, settings.push_user)
        if owner is None:
            return 0
        rows = db.execute(select(NotificationDelivery).where(
            NotificationDelivery.user_id == owner.id,
            NotificationDelivery.target_date == today,
            NotificationDelivery.period.in_(("morning", "evening")),
            NotificationDelivery.status == "deferred",
            NotificationDelivery.last_error.in_((
                "snapshot_unavailable", "sleep_incomplete", "stored_data_incomplete",
            )),
        ).order_by(NotificationDelivery.created_at).limit(2)).scalars().all()
        if not rows:
            return 0
        snapshot = repo.latest_analysis_snapshot(owner.id, "daily", today)
        if snapshot is not None:
            if any(stored_profile_is_usable(snapshot.payload, today, row.period) for row in rows):
                return repo.rearm_unavailable_notification_deliveries(
                    owner.id, snapshot.analysis_run_id, today
                )
            return 0
        material = f"{owner.id}:{today}:{owner.analysis_input_revision}:{_current_analysis_config_digest()}"
        key = "daily-recovery:" + hashlib.sha256(material.encode()).hexdigest()
        if db.execute(select(AnalysisJob.id).where(
            AnalysisJob.user_id == owner.id,
            AnalysisJob.idempotency_key == key,
        )).first() is not None:
            return 0
        user_id = owner.id
    create_analysis_job(user_id, today, key)
    return 1


def dispatcher_job() -> int:
    """Drain due sync, analysis, and delivery work in one worker-owned pass."""
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
    from vitalis.application.jobs import drain_analysis_jobs
    from vitalis.bootstrap import configure_analysis_jobs, get_connector

    configure_analysis_jobs()
    reconcile_deferred_daily_reports()
    analysis_drained = drain_analysis_jobs(max_jobs=1)
    if analysis_drained:
        log.info("analysis dispatcher drained jobs=%s", analysis_drained)
    notification_drained = drain_notification_deliveries(max_deliveries=1)
    poll_drained = drain_notification_polls(max_polls=1)
    if notification_drained or poll_drained:
        log.info(
            "notification dispatcher drained sends=%s polls=%s",
            notification_drained, poll_drained,
        )
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
    return drained + analysis_drained + notification_drained + poll_drained


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
    if settings.weekly_report_enabled:
        scheduler.add_job(
            weekly_analysis_job,
            CronTrigger(
                day_of_week="mon",
                hour=settings.weekly_report_hour,
                minute=settings.weekly_report_minute,
                timezone=settings.timezone,
            ),
            id="weekly_analysis", misfire_grace_time=3600,
            max_instances=1, coalesce=True,
        )
    if settings.monthly_report_enabled:
        scheduler.add_job(
            monthly_analysis_job,
            CronTrigger(
                day=1,
                hour=settings.monthly_report_hour,
                minute=settings.monthly_report_minute,
                timezone=settings.timezone,
            ),
            id="monthly_analysis", misfire_grace_time=3600,
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
