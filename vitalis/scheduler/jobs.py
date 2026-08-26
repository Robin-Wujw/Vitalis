"""多时段调度任务：
  - 02:00 全量同步（补齐历史）
  - 09:30 增量同步 + 数据完整性检查 + 分析推送
  - 14:00 重试（如果早上数据不完整）

解决用户早上 9:30 还没起床、睡眠数据不完整的问题。
"""
import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from vitalis.config import settings

log = logging.getLogger("vitalis.scheduler")

# 内存中记录今天已推送过的用户（每天 00:00 重置）
_pushed_today: set[str] = set()
_last_push_date: date = date.today()


def _record_browser_link_sync(repo, user_id: str, report, label: str) -> None:
    """Reflect scheduled-sync authentication health in the browser link status."""
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
    """获取所有已保存凭据的用户。"""
    from sqlalchemy import select
    from vitalis.storage import session_scope
    from vitalis.storage.models import AuthToken as OrmAuthToken

    with session_scope() as db:
        rows = db.execute(select(OrmAuthToken.user_id).distinct())
        return {r[0] for r in rows}


def _reset_push_state_if_new_day() -> None:
    """跨天时重置推送状态。"""
    global _pushed_today, _last_push_date
    today = date.today()
    if today != _last_push_date:
        _pushed_today.clear()
        _last_push_date = today


def nightly_sync_job() -> None:
    """凌晨 2:00：全量同步（最近 7 天），补齐历史数据。"""
    from vitalis.connectors import get_connector
    from vitalis.connectors.zepp import ZeppConnector
    from vitalis.models import User
    from vitalis.storage import HealthRepository, session_scope

    user_ids = _get_authorized_users()
    if not user_ids:
        log.info("nightly sync: no authorized users")
        return

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]

    for uid in user_ids:
        try:
            with session_scope() as db:
                repo = HealthRepository(db)
                auth = connector.load_token(repo, uid)
                if auth is None:
                    continue
                try:
                    connector.authenticate()
                    client = connector._client_for(repo, User(id=uid))
                    client.verify()
                except Exception as exc:
                    log.warning("nightly sync token invalid: user=%s %s", uid, exc)
                    repo.mark_user_browser_links_reauth(
                        uid, "Zepp 连接验证失败，请重新登录"
                    )
                    continue
                report = connector.sync_with_report(
                    User(id=uid), days=7, repo=repo
                )
                _record_browser_link_sync(repo, uid, report, "夜间同步")
                log.info(
                    "nightly sync ok: user=%s success=%s records=%s",
                    uid, report.success, report.records_written,
                )
        except Exception:
            log.exception("nightly sync failed: user=%s", uid)


def morning_analysis_job() -> None:
    """早上 9:30：增量同步 + 数据完整性检查 + 分析推送。

    如果用户还没起床（睡眠数据不完整），推迟到下午 14:00 重试。
    """
    from vitalis.connectors import get_connector
    from vitalis.connectors.zepp import ZeppConnector
    from vitalis.models import User
    from vitalis.services import SummaryService
    from vitalis.services.completeness_service import CompletenessService
    from vitalis.services.push_service import PushService
    from vitalis.storage import HealthRepository, session_scope

    _reset_push_state_if_new_day()
    user_ids = _get_authorized_users()
    if not user_ids:
        log.info("morning analysis: no authorized users")
        return

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    summary = SummaryService()
    completeness = CompletenessService()
    push = PushService()

    for uid in user_ids:
        if uid in _pushed_today:
            continue  # 今天已经推送过了

        try:
            # 1. 先增量同步（确保拿到最新数据）
            with session_scope() as db:
                repo = HealthRepository(db)
                try:
                    report = connector.sync_with_report(
                        User(id=uid), days=2, repo=repo
                    )
                    _record_browser_link_sync(repo, uid, report, "晨间同步")
                    log.info("morning sync: user=%s records=%s", uid, report.records_written)
                except Exception as exc:
                    log.warning("morning sync failed: user=%s %s", uid, exc)
                    continue

                # 2. 检查数据完整性
                check = completeness.check_today(repo, uid)
                if not check.complete:
                    log.info(
                        "morning analysis deferred: user=%s reason=%s retry_at=%s",
                        uid, check.reason, check.retry_at,
                    )
                    continue

            # 3. 生成分析并推送
            result = summary.today(uid)
            if result.get("found"):
                push.push_daily_summary(uid, result)
                _pushed_today.add(uid)
                log.info("morning analysis pushed: user=%s score=%s", uid, result.get("overall_score"))
            else:
                log.info("morning analysis no data: user=%s", uid)
        except Exception:
            log.exception("morning analysis failed: user=%s", uid)


def afternoon_retry_job() -> None:
    """下午 14:00：重试早上因数据不完整而未推送的用户。"""
    from vitalis.connectors import get_connector
    from vitalis.connectors.zepp import ZeppConnector
    from vitalis.models import User
    from vitalis.services import SummaryService
    from vitalis.services.completeness_service import CompletenessService
    from vitalis.services.push_service import PushService
    from vitalis.storage import HealthRepository, session_scope

    _reset_push_state_if_new_day()
    user_ids = _get_authorized_users()
    if not user_ids:
        return

    connector: ZeppConnector = get_connector("zepp")  # type: ignore[assignment]
    summary = SummaryService()
    completeness = CompletenessService()
    push = PushService()

    for uid in user_ids:
        if uid in _pushed_today:
            continue  # 已经推送过

        try:
            with session_scope() as db:
                repo = HealthRepository(db)
                # 增量同步
                try:
                    report = connector.sync_with_report(User(id=uid), days=2, repo=repo)
                    _record_browser_link_sync(repo, uid, report, "午后同步")
                except Exception as exc:
                    log.warning("afternoon sync failed: user=%s %s", uid, exc)
                    continue

                # 再次检查完整性
                check = completeness.check_today(repo, uid)
                if not check.complete:
                    # 下午了不再等，用现有数据推送
                    log.info("afternoon analysis forced: user=%s missing=%s", uid, check.missing)

            result = summary.today(uid)
            if result.get("found"):
                push.push_daily_summary(uid, result)
                _pushed_today.add(uid)
                log.info("afternoon analysis pushed: user=%s score=%s", uid, result.get("overall_score"))
        except Exception:
            log.exception("afternoon retry failed: user=%s", uid)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    # 02:00 全量同步
    scheduler.add_job(
        nightly_sync_job,
        CronTrigger(hour=2, minute=0),
        id="nightly_sync",
        misfire_grace_time=3600,
    )
    # 09:30 分析推送（等用户起床）
    scheduler.add_job(
        morning_analysis_job,
        CronTrigger(hour=9, minute=30),
        id="morning_analysis",
        misfire_grace_time=1800,
    )
    # 14:00 重试（早上没推送的）
    scheduler.add_job(
        afternoon_retry_job,
        CronTrigger(hour=14, minute=0),
        id="afternoon_retry",
        misfire_grace_time=1800,
    )
    scheduler.start()
    log.info(
        "scheduler started: sync at 02:00, analysis at 09:30, retry at 14:00"
    )
    return scheduler
