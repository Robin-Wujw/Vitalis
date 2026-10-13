"""Scheduled durable synchronization and profile jobs."""

import logging
import os
from pathlib import Path
import socket
import time
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from vitalis.config import settings

log = logging.getLogger("vitalis.scheduler")

MORNING_FALLBACK_NO_FACTS_REASON = "morning_fallback_no_facts"
# The evening report is a cron job; the morning follows the wake window below.
SCHEDULED_REPORT_SYNCS = {"evening": (21, 30, 1)}
CATCH_UP_GRACE = timedelta(minutes=2)
MORNING_PROBE_TRIGGER = "morning_probe"
MORNING_REPORT_SYNC_DAYS = 2
# One retry when a morning sync ends without writing anything.
MORNING_REPORT_SYNCS_PER_DAY = 2
MORNING_WAKE_CHECK_SECONDS = 60
_ACTIVE_SYNC_STATES = {"queued", "running", "retry_wait"}
# The 30 s heartbeat logs memory every 20 beats, about every 10 minutes.
HEARTBEATS_PER_MEMORY_LOG = 20
_heartbeats = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _memory_text() -> str:
    """This process's RSS/swap and its cgroup usage against memory.high, for logs."""
    fields: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                if key in {"VmRSS", "VmSwap"}:
                    fields[key] = int(value.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return "memory=unavailable"
    text = f"rss={fields.get('VmRSS', '?')}MB swap={fields.get('VmSwap', '?')}MB"
    try:
        with open("/proc/self/cgroup", encoding="ascii") as handle:
            path = next(line for line in handle if line.startswith("0::"))[3:].strip()
        base = Path("/sys/fs/cgroup") / path.lstrip("/")
        current = int((base / "memory.current").read_text()) // 2**20
        high = (base / "memory.high").read_text().strip()
        events = dict(
            line.split() for line in (base / "memory.events").read_text().splitlines()
        )
        high_text = high if high == "max" else f"{int(high) // 2**20}MB"
        text += f" cgroup={current}MB high={high_text} high_events={events.get('high', '0')}"
    except (OSError, ValueError, StopIteration):
        pass
    return text


def _release_memory() -> None:
    """Hand memory freed by an analysis back to the OS so a long-lived worker stays small."""
    import gc

    gc.collect()
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _notify_systemd(message: str) -> bool:
    """Send one sd_notify datagram when systemd supervises this process."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.connect(address)
            notify.sendall(message.encode("ascii"))
    except OSError:
        log.warning("systemd notify failed: %s", message)
        return False
    return True


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


def _create_attempt(
    user_id: str,
    days: int,
    trigger: str,
    *,
    trigger_ref: str | None = None,
    repository=None,
    sleep_only: bool = False,
):
    from vitalis.bootstrap import get_connector
    connector = get_connector("zepp")
    create = getattr(connector, "create_attempt", None)
    if create is None:
        return None
    kwargs = {
        "days": days,
        "trigger": trigger,
        "trigger_ref": trigger_ref or user_id,
    }
    if repository is not None:
        kwargs["repository"] = repository
    if sleep_only:
        kwargs["sleep_only"] = True
    return create(user_id, **kwargs)


def _sync_user(
    user_id: str,
    days: int,
    label: str,
    *,
    trigger_ref: str | None = None,
    repository=None,
    sleep_only: bool = False,
) -> str | None:
    """Create a ledger entry only; the dispatcher owns network execution."""
    if trigger_ref is None and repository is None and not sleep_only:
        attempt = _create_attempt(user_id, days, trigger=label)
    else:
        attempt = _create_attempt(
            user_id,
            days,
            trigger=label,
            trigger_ref=trigger_ref,
            repository=repository,
            sleep_only=sleep_only,
        )
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
    users = _get_authorized_users()
    log.info("%s report job started: users=%s sync_days=%s", period, len(users), sync_days)
    for user_id in users:
        try:
            _sync_user(user_id, days=sync_days, label=period)
        except Exception:
            log.exception("%s sync enqueue failed: user=%s", period, user_id)
    dispatcher_job()


def _morning_window(day: date) -> dict[str, datetime]:
    """Today's morning report times as aware UTC instants."""
    from vitalis.application.delivery_policy import scheduled_report_window

    window = scheduled_report_window(
        day, "morning", settings.timezone,
        morning_window=settings.morning_wake_window(),
    )
    return {
        key: datetime.fromisoformat(window[key].replace("Z", "+00:00"))
        for key in ("scheduled_for", "partial_fallback_after", "deadline_at")
    }


def morning_wake_job(now: datetime | None = None) -> int:
    """Start each user's morning report soon after they wake.

    From the window start a sleep-only probe runs at most once per poll
    interval, capped per day, until last night's main sleep is stored with a
    plausible wake time. Then one full morning sync queues the analysis whose
    delivery intent sends the report. At the window end the morning sync runs
    anyway and the delivery policy sends facts only or keeps a missed record.
    A restarted worker resumes from the stored attempts, so no catch-up is needed.
    """
    from zoneinfo import ZoneInfo

    current = now or _utc_now()
    today = current.astimezone(ZoneInfo(settings.timezone)).date()
    window = _morning_window(today)
    if not window["scheduled_for"] <= current < window["deadline_at"]:
        return 0
    queued = 0
    for user_id in sorted(_get_authorized_users()):
        try:
            queued += int(_morning_wake_step(
                user_id, today, current, window["partial_fallback_after"],
            ) is not None)
        except Exception:
            log.exception("morning wake step failed: user=%s", user_id)
    return queued


def _morning_wake_step(
    user_id: str, today: date, now: datetime, wait_until: datetime,
) -> str | None:
    """Queue at most one probe or morning sync for one user; return which."""
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.application.delivery_policy import wake_time_is_plausible

    with session_scope() as db:
        repo = HealthRepository(db)
        if repo.notification_delivery_for_key(user_id, today, "morning") is not None:
            return None
        attempts = repo.sync_attempts_for_trigger_prefix(
            user_id, "zepp",
            {"morning": f"morning:{today.isoformat()}:",
             MORNING_PROBE_TRIGGER: f"morning-probe:{today.isoformat()}:"},
        )
        syncs = [row for row in attempts if row.trigger == "morning"]
        probes = [row for row in attempts if row.trigger == MORNING_PROBE_TRIGGER]
        if any(row.status not in {"failed", "cancelled"} for row in syncs):
            # Queued, running or finished: its analysis owns today's report.
            return None
        if any(row.records_written for row in syncs) or len(syncs) >= MORNING_REPORT_SYNCS_PER_DAY:
            return None
        if any(row.status in _ACTIVE_SYNC_STATES for row in probes):
            return None
        wake_time, sleep_minutes = repo.sleep_wake_summary(user_id, today)
        awake = wake_time_is_plausible(
            today, wake_time, sleep_minutes,
            morning_window=settings.morning_wake_window(),
        )
        if awake or now >= wait_until:
            reason = "sleep_found" if awake else "window_end"
            _sync_user(
                user_id, days=MORNING_REPORT_SYNC_DAYS, label="morning",
                trigger_ref=f"morning:{today.isoformat()}:{len(syncs) + 1}",
                repository=repo,
            )
            log.info(
                "morning report sync queued: user=%s reason=%s probes=%s",
                user_id, reason, len(probes),
            )
            return "morning"
        if len(probes) >= settings.morning_wake_max_probes:
            return None
        latest = max((row.created_at for row in probes), default=None)
        interval = timedelta(seconds=settings.morning_wake_poll_interval_seconds)
        if latest is not None and now.astimezone(timezone.utc).replace(tzinfo=None) < latest + interval:
            return None
        _sync_user(
            user_id, days=1, label=MORNING_PROBE_TRIGGER,
            trigger_ref=f"morning-probe:{today.isoformat()}:{len(probes) + 1}",
            repository=repo, sleep_only=True,
        )
        return "probe"


def evening_analysis_job() -> None:
    _profile_push_job("evening", sync_days=SCHEDULED_REPORT_SYNCS["evening"][2])


def catch_up_scheduled_reports() -> int:
    """Queue today's evening sync when the worker missed its cron run.

    The scheduler keeps cron state in memory, so a worker that was frozen or
    restarted across 21:30 would otherwise skip that report entirely. Only the
    PushPlus delivery owner is caught up, since the missed report is what
    matters. A run counts once its scheduled sync or delivery intent exists;
    catch-up stops at the report deadline and never crosses midnight. The
    morning needs no catch-up: its wake window job resumes from stored state.
    """
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.models import NotificationDelivery, SyncAttempt
    from vitalis.application.delivery_policy import scheduled_report_window

    if not settings.push_user or not settings.pushplus_token:
        return 0
    now = _utc_now()
    zone = ZoneInfo(settings.timezone)
    today = now.astimezone(zone).date()
    midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), zone)
    queued = 0
    for period, (_hour, _minute, sync_days) in SCHEDULED_REPORT_SYNCS.items():
        window = scheduled_report_window(today, period, settings.timezone)
        scheduled = datetime.fromisoformat(window["scheduled_for"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(window["deadline_at"].replace("Z", "+00:00"))
        if not scheduled + CATCH_UP_GRACE <= now < min(deadline, midnight):
            continue
        user_id = settings.push_user
        if user_id not in _get_authorized_users():
            return queued
        with session_scope() as db:
            ran = db.execute(select(SyncAttempt.id).where(
                SyncAttempt.user_id == user_id,
                SyncAttempt.trigger == period,
                SyncAttempt.trigger_ref == user_id,
                SyncAttempt.created_at >= scheduled.replace(tzinfo=None),
            ).limit(1)).first() or db.execute(select(NotificationDelivery.id).where(
                NotificationDelivery.user_id == user_id,
                NotificationDelivery.target_date == today,
                NotificationDelivery.period == period,
            ).limit(1)).first()
        if ran:
            continue
        log.warning("%s report run was missed; catching up: user=%s", period, user_id)
        try:
            _sync_user(user_id, days=sync_days, label=period)
            queued += 1
        except Exception:
            log.exception("%s catch-up enqueue failed: user=%s", period, user_id)
    return queued


def weekly_analysis_job() -> None:
    _profile_push_job("weekly", sync_days=8)


def monthly_analysis_job() -> None:
    _profile_push_job("monthly", sync_days=2)


def worker_heartbeat_job() -> None:
    """Record worker liveness independently of a potentially slow sync pass.

    Each written heartbeat also feeds the systemd watchdog, so a worker that
    stops making progress (for example frozen under memory.high) is restarted.
    """
    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.models import WorkerHeartbeat

    global _heartbeats
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as db:
        row = db.get(WorkerHeartbeat, "primary")
        if row is None:
            db.add(WorkerHeartbeat(name="primary", last_seen_at=now))
        else:
            row.last_seen_at = now
    _notify_systemd("WATCHDOG=1")
    if _heartbeats % HEARTBEATS_PER_MEMORY_LOG == 0:
        log.info("worker alive: %s", _memory_text())
    _heartbeats += 1


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
        log.info(
            "notification delivery %s %s: status=%s reason=%s",
            delivery["period"], delivery["target_date"], status, error or "-",
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
    from vitalis.adapters.persistence.models import NotificationDelivery

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
    """Rearm deferred daily intents once usable facts or the wait end arrive."""
    import hashlib

    from sqlalchemy import select

    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.models import AnalysisJob, NotificationDelivery, User
    from vitalis.adapters.persistence.repositories import _current_analysis_config_digest
    from vitalis.application.delivery_policy import (
        has_same_day_facts,
        scheduled_report_window,
        stored_profile_is_usable,
    )
    from vitalis.application.jobs import create_analysis_job
    from vitalis.time import local_today

    if not settings.push_user or not settings.pushplus_token:
        return 0
    today = local_today()
    now = _utc_now()
    window = _morning_window(today)
    fallback = window["partial_fallback_after"]
    deadline = window["deadline_at"]
    evening_window = scheduled_report_window(today, "evening", settings.timezone)
    evening_scheduled = datetime.fromisoformat(
        evening_window["scheduled_for"].replace("Z", "+00:00")
    )
    evening_deadline = datetime.fromisoformat(
        evening_window["deadline_at"].replace("Z", "+00:00")
    )
    changed = 0
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
                "snapshot_unavailable",
                "sleep_incomplete",
                "stored_data_incomplete",
                MORNING_FALLBACK_NO_FACTS_REASON,
            )),
        ).order_by(NotificationDelivery.created_at).limit(2)).scalars().all()
        if not rows:
            return 0
        pending = ", ".join(f"{row.period}:{row.last_error}" for row in rows)
        if now >= deadline:
            changed += repo.expire_morning_deferred_notification_delivery(
                owner.id, today, now=now
            )
            if changed:
                log.info("deferred reports [%s]: morning expired at deadline", pending)
            rows = [row for row in rows if row.period != "morning"]
            if not rows:
                return changed
        snapshot = repo.latest_analysis_snapshot(owner.id, "daily", today)
        if snapshot is None:
            recoverable = [
                row for row in rows
                if not (row.period == "morning" and row.last_error == MORNING_FALLBACK_NO_FACTS_REASON)
            ]
            if not recoverable:
                return changed
            material = (
                f"{owner.id}:{today}:{owner.analysis_input_revision}:"
                f"{_current_analysis_config_digest()}"
            )
            key = "daily-recovery:" + hashlib.sha256(material.encode()).hexdigest()
            if db.execute(select(AnalysisJob.id).where(
                AnalysisJob.user_id == owner.id,
                AnalysisJob.idempotency_key == key,
            )).first() is not None:
                return changed
            user_id = owner.id
        else:
            for row in rows:
                usable = (
                    stored_profile_is_usable(snapshot.payload, today, row.period)
                    and has_same_day_facts(snapshot.payload)
                )
                if row.period == "morning":
                    if usable and row.last_error == "sleep_incomplete":
                        changed += repo.rearm_morning_deferred_notification_delivery(
                            owner.id, snapshot.analysis_run_id, today
                        )
                    elif usable and row.last_error != MORNING_FALLBACK_NO_FACTS_REASON:
                        changed += repo.rearm_unavailable_notification_deliveries(
                            owner.id, snapshot.analysis_run_id, today, period="morning",
                        )
                    elif row.last_error == "sleep_incomplete" and fallback <= now < deadline:
                        # The wake window is over: send what was observed, or
                        # keep the missed record when there is nothing to show.
                        if has_same_day_facts(snapshot.payload):
                            changed += repo.rearm_morning_deferred_notification_delivery(
                                owner.id, snapshot.analysis_run_id, today
                            )
                        else:
                            changed += int(repo.mark_morning_fallback_no_facts(owner.id, today))
                elif usable and evening_scheduled <= now < evening_deadline:
                    changed += repo.rearm_unavailable_notification_deliveries(
                        owner.id, snapshot.analysis_run_id, today, period=row.period,
                    )
            if changed:
                log.info("deferred reports [%s]: %s intents updated", pending, changed)
            return changed
    create_analysis_job(user_id, today, key)
    log.info("deferred reports [%s]: recovery analysis queued", pending)
    return 1


def dispatcher_job() -> int:
    """Drain due sync, analysis, and delivery work in one worker-owned pass."""
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
    from vitalis.application.jobs import drain_analysis_jobs
    from vitalis.bootstrap import configure_analysis_jobs, get_connector

    configure_analysis_jobs()
    reconcile_deferred_daily_reports()
    started = time.monotonic()
    analysis_drained = drain_analysis_jobs(max_jobs=1)
    if analysis_drained:
        _release_memory()
        log.info(
            "analysis dispatcher drained jobs=%s seconds=%.1f %s",
            analysis_drained, time.monotonic() - started, _memory_text(),
        )
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
    hour, minute, _days = SCHEDULED_REPORT_SYNCS["evening"]
    scheduler.add_job(
        evening_analysis_job,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        id="evening_analysis", misfire_grace_time=1800,
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        catch_up_scheduled_reports,
        IntervalTrigger(minutes=5, timezone=settings.timezone),
        id="report_catch_up", misfire_grace_time=60,
        max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        morning_wake_job,
        IntervalTrigger(seconds=MORNING_WAKE_CHECK_SECONDS, timezone=settings.timezone),
        id="morning_wake", misfire_grace_time=60,
        max_instances=1, coalesce=True,
        next_run_time=datetime.now(timezone.utc),
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
