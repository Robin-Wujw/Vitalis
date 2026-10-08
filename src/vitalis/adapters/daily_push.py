"""Run deterministic daily PushPlus reports with local application services."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

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
from vitalis.config import settings
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
    # Durable NotificationDelivery is the single authority for normal sends.
    # test_delivery is deliberately isolated and never mutates that intent.
    _require_local_api(api)
    if not test_delivery:
        existing = _existing_direct_delivery(user_id, current_date, period)
        if existing is not None and existing["status"] in {
            "accepted", "delivered", "uncertain", "running",
        }:
            return existing

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
    send_attempt_id: str | None = None,
) -> dict:
    """Deliver a saved report through the durable notification intent."""
    today = local_today()
    current_date = target_date or today
    timezone_name = (
        (daily.get("report_context") or {}).get("timezone") or "UTC"
    )
    if plan_expires_at is None and not retrospective and (
        scheduled_delivery or target_date is None
    ):
        _, plan_expires_at = local_day_utc_bounds(current_date)

    # A test delivery is intentionally isolated from the ordinary outbox.  It
    # exercises the real renderer and fake transport but cannot mark a report sent.
    if test_delivery:
        decision = prepare_delivery(
            daily, period=period, target_date=target_date, today=today,
            as_of=datetime.now(UTC), timezone=timezone_name, test_delivery=True,
            already_sent=False, sync_degraded=sync_degraded, sync_status=sync_status,
            sync_detail=sync_detail, plan_expires_at=plan_expires_at,
            retrospective=retrospective,
        )
        if decision["status"] != "ready":
            return decision
        payload = decision["payload"]
        try:
            service = PushService(
                pushplus_token=pushplus_token,
                pushplus_access_key=getattr(settings, "pushplus_access_key", ""),
            )
        except TypeError:
            service = PushService(pushplus_token=pushplus_token)
        results = service.push_daily_profile(user_id, payload, period=period)
        provider = _provider_result(results)
        if provider["status"] not in {"accepted", "delivered"}:
            raise DailyPushDeliveryError(
                ambiguous=provider["status"] == "uncertain"
            )
        outcome = _delivery_outcome(
            payload, period, provider, sync_degraded, sync_status,
            test=True, retrospective=retrospective, decision=decision,
        )
        return outcome

    decision = prepare_delivery(
        daily, period=period, target_date=target_date, today=today,
        as_of=datetime.now(UTC), timezone=timezone_name, test_delivery=False,
        already_sent=False, sync_degraded=sync_degraded, sync_status=sync_status,
        sync_detail=sync_detail, plan_expires_at=plan_expires_at,
        retrospective=retrospective,
    )
    if decision["status"] != "ready":
        return decision
    payload = decision["payload"]

    claim = None
    if not scheduled_delivery:
        claim = _claim_direct_notification(
            user_id, payload, period, current_date,
        )
        if claim is None:
            return {
                "status": "already_sent",
                "period": period,
                "date": current_date.isoformat(),
            }
        if claim.get("status") == "deferred":
            return {
                "status": "deferred", "period": period,
                "date": current_date.isoformat(),
                "reason": claim.get("reason", "analysis_run_required"),
            }
        if claim.get("status") in {"accepted", "delivered", "uncertain"}:
            return {
                "status": claim["status"], "period": period,
                "date": current_date.isoformat(),
                "provider_id": claim.get("provider_id"),
            }

    try:
        service = PushService(
            pushplus_token=pushplus_token,
            pushplus_access_key=getattr(settings, "pushplus_access_key", ""),
            send_attempt_id=claim.get("send_attempt_id") if claim else send_attempt_id,
        )
    except TypeError:
        service = PushService(pushplus_token=pushplus_token)
    results = service.push_daily_profile(user_id, payload, period=period)
    provider = _provider_result(results)
    if claim is not None:
        _complete_direct_notification(claim, provider, results)
    if provider["status"] not in {"accepted", "delivered"}:
        raise DailyPushDeliveryError(
            ambiguous=provider["status"] == "uncertain"
        )
    outcome = _delivery_outcome(
        payload, period, provider, sync_degraded, sync_status,
        test=False, retrospective=retrospective, decision=decision,
    )
    if scheduled_delivery:
        outcome["_pushplus_result"] = provider
        outcome["_render"] = results.get("_render") or {}
    return outcome


def _existing_direct_delivery(
    user_id: str, target_date: date, period: ReportPeriod,
) -> dict | None:
    with session_scope() as db:
        row = HealthRepository(db).notification_delivery_for_key(
            user_id, target_date, period,
        )
        if row is None:
            return None
        if row.status == "running" and (
            row.lease_expires_at is None or row.lease_expires_at <= datetime.utcnow()
        ):
            return None
        return {
            "status": row.status,
            "period": period,
            "date": target_date.isoformat(),
            "provider_id": row.provider_id,
        }


def _provider_result(results: dict) -> dict:
    result = results.get("_pushplus_result")
    if isinstance(result, dict):
        return result
    handler = results.get("_pushplus_handler")
    if handler in {"ok", "accepted"}:
        return {"status": "accepted", "provider_id": None}
    if handler == "delivered":
        return {"status": "delivered", "provider_id": None}
    return {
        "status": "uncertain" if results.get("_delivery_outcome") == "uncertain" else "failed",
        "provider_id": None,
    }


def _delivery_outcome(
    payload: dict, period: str, provider: dict, sync_degraded: bool,
    sync_status: str | None, *, test: bool, retrospective: bool, decision: dict,
) -> dict:
    outcome = {
        "status": "test_accepted" if test else provider["status"],
        "period": period,
        "date": payload["date"],
        "quality": payload.get("data_quality", {}).get("status", "UNKNOWN"),
        "sync_degraded": sync_degraded,
        "sync_status": sync_status,
    }
    if provider.get("provider_id"):
        outcome["provider_id"] = provider["provider_id"]
    if test:
        outcome["scheduled_delivery_unchanged"] = True
    if retrospective:
        outcome["retrospective"] = True
    if decision.get("facts_only"):
        outcome.update(
            mode="facts_only", facts_only=True,
            coverage_reason=decision["facts_only_reason"],
        )
    return outcome


def _claim_direct_notification(
    user_id: str, payload: dict, period: ReportPeriod, target_date: date,
) -> dict | None:
    analysis_run_id = payload.get("analysis_run_id")
    if not analysis_run_id:
        # A normal send must point at a canonical succeeded analysis run. Test
        # deliveries are handled above and do not enter this path.
        return {"status": "deferred", "reason": "analysis_run_required"}
    with session_scope() as db:
        repo = HealthRepository(db)
        row = repo.enqueue_notification_delivery(
            user_id, str(analysis_run_id), period, target_date,
        )
        existing_status = row.status
        if existing_status in {"accepted", "delivered", "uncertain"}:
            return {
                "status": existing_status, "provider_id": row.provider_id,
                "delivery_id": row.id, "lease_token": None,
            }
        claimed = repo.claim_notification_delivery(delivery_id=row.id)
        if claimed is None:
            return None
        return {
            "status": claimed.status, "delivery_id": claimed.id,
            "lease_token": claimed.lease_token,
            "send_attempt_id": claimed.send_attempt_id,
        }


def _complete_direct_notification(claim: dict, provider: dict, results: dict) -> None:
    if not claim.get("delivery_id") or not claim.get("lease_token"):
        return
    render = results.get("_render") or {}
    status = provider.get("status", "failed")
    next_poll_at = None
    if status == "accepted":
        from datetime import timedelta
        if provider.get("provider_id"):
            next_poll_at = datetime.now(UTC) + timedelta(
                seconds=getattr(settings, "pushplus_query_interval_seconds", 60)
            ) if getattr(settings, "pushplus_access_key", "") else None
    error = None if status in {"accepted", "delivered"} else (
        "transport_ambiguous" if status == "uncertain" else "transport_rejected"
    )
    with session_scope() as db:
        HealthRepository(db).complete_notification_delivery(
            claim["delivery_id"], claim["lease_token"], status,
            error=error, next_poll_at=next_poll_at,
            provider_id=provider.get("provider_id"),
            provider_status=provider.get("provider_status"),
            template=render.get("template"),
            renderer_version=render.get("renderer_version"),
            content_sha256=render.get("content_sha256"),
            send_attempt_id=claim.get("send_attempt_id"),
        )


def deliver_period_report(
    user_id: str,
    pushplus_token: str,
    profile: dict,
    *,
    period: str,
    target_date: date,
) -> dict:
    """Deliver a saved calendar report through the same durable intent path."""
    if period not in {"weekly", "monthly"}:
        raise ValueError("period must be weekly or monthly")
    claim = _claim_direct_notification(user_id, profile, period, target_date)
    if claim is None:
        return {"status": "already_sent", "period": period, "date": target_date.isoformat()}
    if claim.get("status") in {"accepted", "delivered", "uncertain"}:
        return {
            "status": claim["status"], "period": period,
            "date": target_date.isoformat(), "provider_id": claim.get("provider_id"),
        }
    if claim.get("status") == "deferred":
        return {
            "status": "deferred", "period": period, "date": target_date.isoformat(),
            "reason": claim.get("reason", "analysis_run_required"),
        }
    try:
        service = PushService(
            pushplus_token=pushplus_token,
            pushplus_access_key=getattr(settings, "pushplus_access_key", ""),
            send_attempt_id=claim.get("send_attempt_id"),
        )
    except TypeError:
        service = PushService(pushplus_token=pushplus_token)
    results = (
        service.push_weekly_profile(user_id, profile)
        if period == "weekly" else service.push_monthly_profile(user_id, profile)
    )
    provider = _provider_result(results)
    _complete_direct_notification(claim, provider, results)
    if provider["status"] not in {"accepted", "delivered"}:
        raise DailyPushDeliveryError(ambiguous=provider["status"] == "uncertain")
    return {
        "status": provider["status"], "period": period,
        "date": target_date.isoformat(),
        "quality": (profile.get("data_quality") or {}).get("status", "UNKNOWN"),
        "provider_id": provider.get("provider_id"),
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
