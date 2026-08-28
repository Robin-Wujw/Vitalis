"""Scheduled synchronization and renderer-only daily profile pushes."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("vitalis.scheduler")


def _record_browser_link_sync(repo, user_id: str, report, label: str) -> None:
    reauth = next((stream for stream in report.streams if stream.needs_reauth), None)
    if reauth:
        repo.mark_user_browser_links_reauth(
            user_id, f"Zepp 登录已失效，请重新登录：{reauth.message}"
        )
        return
    link = repo.latest_browser_link(user_id)
    if link:
        repo.mark_browser_link_synced(
            link.token_digest, f"{label}完成，写入 {report.records_written} 条记录"
        )


def _get_authorized_users() -> set[str]:
    from sqlalchemy import select

    from vitalis.storage import session_scope
    from vitalis.storage.models import AuthToken as OrmAuthToken

    with session_scope() as db:
        rows = db.execute(select(OrmAuthToken.user_id).distinct())
        return {row[0] for row in rows}


def _sync_user(user_id: str, days: int, label: str) -> bool:
    from vitalis.connectors import get_connector
    from vitalis.connectors.zepp import ZeppConnector
    from vitalis.models import User
    from vitalis.storage import HealthRepository, session_scope

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    with session_scope() as db:
        repo = HealthRepository(db)
        report = connector.sync_with_report(User(id=user_id), days=days, repo=repo)
        _record_browser_link_sync(repo, user_id, report, label)
        log.info(
            "%s: user=%s success=%s records=%s",
            label, user_id, report.success, report.records_written,
        )
        return report.success


def nightly_sync_job() -> None:
    """02:00: refresh the recent seven-day data window."""
    user_ids = _get_authorized_users()
    if not user_ids:
        log.info("nightly sync: no authorized users")
        return
    for user_id in user_ids:
        try:
            _sync_user(user_id, days=7, label="夜间同步")
        except Exception:
            log.exception("nightly sync failed: user=%s", user_id)


def _profile_push_job(period: str, sync_days: int) -> None:
    from vitalis.intelligence.service import IntelligencePipeline
    from vitalis.services.push_service import PushService

    user_ids = _get_authorized_users()
    if not user_ids:
        log.info("%s profile: no authorized users", period)
        return
    service = IntelligencePipeline()
    push = PushService()
    for user_id in user_ids:
        try:
            _sync_user(user_id, days=sync_days, label=f"{period}同步")
            profile = service.build_daily_profile(user_id)
            push.push_daily_profile(user_id, profile, period=period)
            log.info(
                "%s profile pushed: user=%s action=%s quality=%s",
                period, user_id, profile.decision.action, profile.data_quality.status,
            )
        except Exception:
            log.exception("%s profile failed: user=%s", period, user_id)


def morning_analysis_job() -> None:
    """09:30: synchronize and push the morning profile."""
    _profile_push_job("morning", sync_days=2)


def evening_analysis_job() -> None:
    """21:30: synchronize and push the evening profile."""
    _profile_push_job("evening", sync_days=1)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        nightly_sync_job,
        CronTrigger(hour=2, minute=0),
        id="nightly_sync",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        morning_analysis_job,
        CronTrigger(hour=9, minute=30),
        id="morning_analysis",
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        evening_analysis_job,
        CronTrigger(hour=21, minute=30),
        id="evening_analysis",
        misfire_grace_time=1800,
    )
    scheduler.start()
    log.info("scheduler started: sync 02:00, morning 09:30, evening 21:30")
    return scheduler
