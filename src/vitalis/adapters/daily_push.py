"""Run deterministic daily PushPlus reports with local application services."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from vitalis.adapters.notifications import PushService
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.zepp import ZeppAuthError, ZeppConnector
from vitalis.adapters.zepp.sync_manager import SyncReport
from vitalis.application.delivery_policy import ReportPeriod, prepare_delivery
from vitalis.application.delivery_policy import retrospective_age as _retrospective_age
from vitalis.application.delivery_policy import (
    stored_profile_is_usable as _stored_profile_is_usable,
)
from vitalis.bootstrap import get_intelligence_command
from vitalis.domain import User
from vitalis.time import local_day_utc_bounds, local_today

# Kept as a narrow test seam for direct manual analysis composition.
IntelligenceCommand = get_intelligence_command


DEFAULT_STATE_DIR = Path.home() / ".hermes" / "vitalis_push"


class DailyPushDeliveryError(RuntimeError):
    """Push failed; ``ambiguous`` means the provider may have accepted it."""

    def __init__(self, message: str = "PushPlus delivery failed", *, ambiguous: bool):
        super().__init__(message)
        self.ambiguous = ambiguous
SYNC_POLL_INTERVAL_SECONDS = 2
SYNC_POLL_MAX_ATTEMPTS = 90


def run_daily_push(
    user_id: str,
    pushplus_token: str,
    *,
    period: ReportPeriod,
    api: str = "http://localhost:8000",
    sync_days: int | None = None,
    target_date: date | None = None,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    test_delivery: bool = False,
    retrospective: bool = False,
) -> dict:
    """Synchronize, analyze, and deliver a scheduled or non-marking test report."""
    if not user_id:
        raise ValueError("VITALIS_USER is required")
    if not pushplus_token:
        raise ValueError("PUSHPLUS_TOKEN is required")
    if period not in ("morning", "evening"):
        raise ValueError("period must be morning or evening")

    today = local_today()
    current_date = target_date or today
    marker = _delivery_marker(Path(state_dir), user_id, current_date, period)
    # Direct manual delivery uses the marker as its first expensive-work gate.
    if not test_delivery and marker.exists():
        return {
            "status": "already_sent",
            "period": period,
            "date": current_date.isoformat(),
        }
    _require_local_api(api)

    days = sync_days if sync_days is not None else (2 if period == "morning" else 1)
    if retrospective:
        age = _retrospective_age(target_date, today, period, test_delivery)
        days = max(days, age + 1)
    elif current_date != today:
        raise ValueError("非当日报告必须显式使用测试晚报补发模式")
    if not 1 <= days <= 730:
        raise ValueError("sync_days must be between 1 and 730")

    sync_degraded, sync_status, sync_detail = _assess_sync(
        _sync_health(user_id, days)
    )
    daily = _analyze(user_id, current_date)

    # A degraded sync may use saved data, but only after the profile-level gate.
    if sync_degraded and not _stored_profile_is_usable(
        daily, current_date, period
    ):
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "stored_data_incomplete",
            "sync_degraded": True,
            "sync_status": sync_status,
        }

    return deliver_daily_report(
        user_id,
        pushplus_token,
        daily,
        period=period,
        target_date=target_date,
        state_dir=state_dir,
        test_delivery=test_delivery,
        sync_degraded=sync_degraded,
        sync_status=sync_status,
        sync_detail=sync_detail,
        retrospective=retrospective,
    )



def deliver_daily_report(
    user_id: str,
    pushplus_token: str,
    daily: dict,
    *,
    period: ReportPeriod,
    target_date: date | None = None,
    state_dir: Path | str = DEFAULT_STATE_DIR,
    test_delivery: bool = False,
    sync_degraded: bool = False,
    sync_status: str | None = None,
    sync_detail: str | None = None,
    plan_expires_at: datetime | None = None,
    retrospective: bool = False,
    scheduled_delivery: bool = False,
) -> dict:
    """Deliver a saved report using the pure policy and concrete transport."""
    today = local_today()
    current_date = target_date or today
    timezone_name = (
        (daily.get("report_context") or {}).get("timezone") or "UTC"
    )
    marker_authoritative = not test_delivery and not scheduled_delivery
    marker = (
        _delivery_marker(Path(state_dir), user_id, current_date, period)
        if marker_authoritative else None
    )
    guard = nullcontext() if not marker_authoritative else _delivery_lock(marker)
    with guard:
        already_sent = bool(marker_authoritative and marker.exists())
        if already_sent:
            return {
                "status": "already_sent",
                "period": period,
                "date": current_date.isoformat(),
            }
        if plan_expires_at is None and not retrospective and (
            scheduled_delivery or target_date is None
        ):
            _, plan_expires_at = local_day_utc_bounds(current_date)
        decision = prepare_delivery(
            daily,
            period=period,
            target_date=target_date,
            today=today,
            as_of=datetime.now(UTC),
            timezone=timezone_name,
            test_delivery=test_delivery,
            already_sent=already_sent,
            sync_degraded=sync_degraded,
            sync_status=sync_status,
            sync_detail=sync_detail,
            plan_expires_at=plan_expires_at,
            retrospective=retrospective,
        )
        if decision["status"] != "ready":
            return decision
        payload = decision["payload"]
        results = PushService(pushplus_token=pushplus_token).push_daily_profile(
            user_id, payload, period=period
        )
        if results.get("_pushplus_handler") != "ok":
            raise DailyPushDeliveryError(
                ambiguous=results.get("_delivery_outcome") == "uncertain"
            )
        if marker_authoritative:
            _mark_delivered(marker)
        outcome = {
            "status": "test_sent" if test_delivery else "sent",
            "period": period,
            "date": payload["date"],
            "quality": payload.get("data_quality", {}).get("status", "UNKNOWN"),
            "sync_degraded": sync_degraded,
            "sync_status": sync_status,
        }
        if test_delivery:
            outcome["scheduled_delivery_unchanged"] = True
        if retrospective:
            outcome["retrospective"] = True
        if decision["facts_only"]:
            outcome.update(
                mode="facts_only",
                facts_only=True,
                coverage_reason=decision["facts_only_reason"],
            )
        return outcome


def deliver_period_report(
    user_id: str,
    pushplus_token: str,
    profile: dict,
    *,
    period: str,
    target_date: date,
) -> dict:
    """Deliver a saved weekly or monthly projection without recomputing it."""
    if period not in {"weekly", "monthly"}:
        raise ValueError("period must be weekly or monthly")
    service = PushService(pushplus_token=pushplus_token)
    if period == "weekly":
        results = service.push_weekly_profile(user_id, profile)
    else:
        results = service.push_monthly_profile(user_id, profile)
    if results.get("_pushplus_handler") != "ok":
        raise DailyPushDeliveryError(
            ambiguous=results.get("_delivery_outcome") == "uncertain"
        )
    return {
        "status": "sent",
        "period": period,
        "date": target_date.isoformat(),
        "quality": (profile.get("data_quality") or {}).get("status", "UNKNOWN"),
    }


def _require_local_api(api: str) -> None:
    """Keep the CLI argument, but never silently redirect a remote run locally."""
    parsed = urlsplit(api)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment or parsed.username or parsed.password
    ):
        raise ValueError("VITALIS_API must be local for direct daily push; remote API requires bearer auth")
    _ = parsed.port  # Reject malformed ports even though direct calls do not use them.


def _sync_health(user_id: str, days: int) -> dict:
    connector = ZeppConnector()
    try:
        with session_scope() as db:
            auth = connector.load_token(HealthRepository(db), user_id)
        if auth is None and not connector.mock:
            return {"status": "token_required", "detail": "Zepp credentials required"}
        user = User(id=user_id)
        if connector.mock:
            with session_scope() as db:
                report = connector.sync_with_report(
                    user, days=days, repo=HealthRepository(db), trigger="manual"
                )
            timed_out = False
        else:
            report = connector.sync_with_report(user, days=days, trigger="manual")
            report, timed_out = _await_sync_report(connector, user, days, report)
    except ZeppAuthError as exc:
        if exc.needs_reauth:
            status = "needs_reauth"
        elif exc.kind in {"network", "service", "timeout"}:
            status = "transient_error"
        else:
            status = "failed"
        return {"status": status, "retryable": status == "transient_error", "detail": str(exc)}
    result = _sync_report_result(report)
    if timed_out:
        result.update(status="timeout", retryable=True, detail="等待同步完成超时，可重试")
    return result


def _await_sync_report(
    connector: ZeppConnector, user: User, days: int, report: SyncReport
) -> tuple[SyncReport, bool]:
    attempt_id = (report.progress or {}).get("attempt_id")
    pending = {"queued", "running", "retry_wait"}
    if not attempt_id:
        return report, False
    for _ in range(SYNC_POLL_MAX_ATTEMPTS):
        if report.needs_reauth or (report.progress or {}).get("status") not in pending:
            return report, False
        time.sleep(SYNC_POLL_INTERVAL_SECONDS)
        report = connector.sync_with_report(
            user, days=days, attempt_id=attempt_id, trigger="manual"
        )
    return report, not report.needs_reauth and (report.progress or {}).get("status") in pending


def _sync_report_result(report: SyncReport) -> dict:
    progress = report.progress or {}
    attempt_status = progress.get("status")
    status = {
        "succeeded": "synced",
        "partial": "incomplete",
        "retry_wait": "transient_error",
        "queued": "timeout",
        "running": "timeout",
        "needs_reauth": "needs_reauth",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(attempt_status, "synced" if report.success else "failed")
    if report.needs_reauth:
        status = "needs_reauth"
    return {
        "status": status,
        "success": report.success,
        "attempt_status": attempt_status,
        "attempt_id": progress.get("attempt_id"),
        "retryable": status in {"transient_error", "timeout"},
        "streams": [{"needs_reauth": stream.needs_reauth} for stream in report.streams],
        "detail": (
            "等待同步完成超时，可重试" if status == "timeout" else report.message
        ),
    }


def _assess_sync(sync: dict) -> tuple[bool, str, str | None]:
    status = str(sync.get("status", "unknown"))
    stream_needs_reauth = any(
        stream.get("needs_reauth") is True
        for stream in sync.get("streams", [])
        if isinstance(stream, dict)
    )
    if status in {"needs_reauth", "token_required"} or stream_needs_reauth:
        raise RuntimeError(f"Vitalis sync did not complete: {status}")
    if status == "synced" and sync.get("success") is True:
        return False, status, None
    if status in {"synced", "incomplete"} or sync.get("retryable") is True:
        return True, status, sync.get("detail") or sync.get("message")
    raise RuntimeError(f"Vitalis sync did not complete: {status}")


def _analyze(user_id: str, day: date) -> dict:
    return IntelligenceCommand().analyze(user_id, day).daily.model_dump(mode="json")



def _delivery_marker(
    state_dir: Path, user_id: str, day: date, period: ReportPeriod
) -> Path:
    user_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return state_dir / f"{day.isoformat()}-{period}-{user_key}.sent"


@contextmanager
def _delivery_lock(marker: Path) -> Iterator[None]:
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(marker.parent, 0o700)
    lock_path = marker.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _mark_delivered(marker: Path) -> None:
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as marker_file:
        os.chmod(marker, 0o600)
        marker_file.write("sent\n")
        marker_file.flush()
        os.fsync(marker_file.fileno())
