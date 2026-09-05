"""Run deterministic daily PushPlus reports through the public Vitalis API."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import time
import os
from pathlib import Path
from typing import Iterator, Literal

import httpx

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from vitalis.services.push_service import PushService
from vitalis.time import local_day_utc_bounds, local_today


ReportPeriod = Literal["morning", "evening"]
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "vitalis_push"
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
) -> dict:
    """Synchronize, analyze, and deliver a scheduled or non-marking test report."""
    if not user_id:
        raise ValueError("VITALIS_USER is required")
    if not pushplus_token:
        raise ValueError("PUSHPLUS_TOKEN is required")
    if period not in ("morning", "evening"):
        raise ValueError("period must be morning or evening")

    current_date = target_date or local_today()
    days = sync_days if sync_days is not None else (2 if period == "morning" else 1)
    marker = _delivery_marker(Path(state_dir), user_id, current_date, period)
    if not test_delivery and marker.exists():
        return {
            "status": "already_sent",
            "period": period,
            "date": current_date.isoformat(),
        }

    headers = {"X-User-Id": user_id}
    with httpx.Client(
        base_url=api.rstrip("/"),
        headers=headers,
        timeout=180.0,
        trust_env=False,
    ) as client:
        sync = _await_sync(client, _sync_health(client, days))
        sync_degraded, sync_status, sync_detail = _assess_sync(sync)
        daily = _analyze(client, current_date)

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
) -> dict:
    """Deliver an analyzed report under the shared date/lock/marker gate."""
    current_date = target_date or local_today()
    if plan_expires_at is None and target_date is None:
        _, plan_expires_at = local_day_utc_bounds(current_date)
    if plan_expires_at is not None:
        expires = plan_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc):
            return {
                "status": "deferred",
                "period": period,
                "date": current_date.isoformat(),
                "reason": "stale_plan_expired",
            }
    marker = _delivery_marker(Path(state_dir), user_id, current_date, period)
    guard = nullcontext() if test_delivery else _delivery_lock(marker)
    with guard:
        if not test_delivery and marker.exists():
            return {
                "status": "already_sent",
                "period": period,
                "date": current_date.isoformat(),
            }
        if daily.get("date") != current_date.isoformat():
            return {
                "status": "deferred",
                "period": period,
                "date": current_date.isoformat(),
                "reason": "stale_report_date",
                "report_date": daily.get("date"),
            }
        if period == "morning" and not _sleep_is_complete(daily):
            return {
                "status": "deferred",
                "period": period,
                "date": current_date.isoformat(),
                "reason": "sleep_incomplete",
                "sync_degraded": sync_degraded,
                "sync_status": sync_status,
            }
        if not _stored_profile_is_usable(daily, current_date, period):
            return {
                "status": "deferred",
                "period": period,
                "date": current_date.isoformat(),
                "reason": "stored_data_incomplete",
                "sync_degraded": sync_degraded,
                "sync_status": sync_status,
            }
        payload = deepcopy(daily) if sync_degraded else daily
        if sync_degraded:
            payload["delivery_metadata"] = {
                "sync_degraded": True,
                "sync_status": sync_status,
                "sync_detail": sync_detail,
            }
        results = PushService(pushplus_token=pushplus_token).push_daily_profile(
            user_id, payload, period=period
        )
        if results.get("_pushplus_handler") != "ok":
            raise RuntimeError("PushPlus delivery failed")
        if not test_delivery:
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
        return outcome


def _sync_health(client: httpx.Client, days: int) -> dict:
    try:
        response = client.post(
            "/api/v1/health/sync", params={"days": days, "enqueue_only": "true"}
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            raise
        return {
            "status": "transport_error",
            "retryable": True,
            "detail": str(exc),
        }
    except httpx.RequestError as exc:
        return {
            "status": "transport_error",
            "retryable": True,
            "detail": str(exc),
        }


def _await_sync(client: httpx.Client, sync: dict) -> dict:
    attempt_id = sync.get("attempt_id")
    if sync.get("status") != "queued" or not attempt_id:
        return sync
    latest = sync
    for _ in range(SYNC_POLL_MAX_ATTEMPTS):
        try:
            response = client.get(f"/api/v1/health/sync/{attempt_id}")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            return {
                **latest,
                "status": "transport_error",
                "retryable": True,
                "detail": str(exc),
            }
        except httpx.RequestError as exc:
            return {
                **latest,
                "status": "transport_error",
                "retryable": True,
                "detail": str(exc),
            }
        attempt = payload.get("attempt") or {}
        status = attempt.get("status")
        if status in {"succeeded", "partial", "failed", "needs_reauth", "cancelled"}:
            latest = {**sync, "status": "synced" if status == "succeeded" else "incomplete" if status == "partial" else status, "success": status == "succeeded", "attempt_status": status, "progress": payload.get("progress")}
            break
        time.sleep(SYNC_POLL_INTERVAL_SECONDS)
    else:
        return {
            **latest,
            "status": "timeout",
            "retryable": True,
            "detail": "等待同步完成超时，可重试",
        }
    return latest


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


def _analyze(client: httpx.Client, day: date) -> dict:
    response = client.post(
        "/api/v1/intelligence/analyze", params={"day": day.isoformat()}
    )
    response.raise_for_status()
    return response.json()["daily"]


def _sleep_is_complete(daily: dict) -> bool:
    sleep = daily.get("features", {}).get("sleep", {})
    return sleep.get("status") == "AVAILABLE" and bool(sleep.get("wake_time"))


def _stored_profile_is_usable(
    daily: dict, day: date, period: ReportPeriod
) -> bool:
    if daily.get("date") != day.isoformat():
        return False
    context = daily.get("report_context")
    history = context.get("training_history") if isinstance(context, dict) else None
    if not isinstance(history, dict):
        training = (daily.get("features") or {}).get("training") or {}
        history = training.get("history_coverage")
    if not isinstance(history, dict):
        return False
    if period == "morning":
        # Morning delivery needs the prior seven days to be verified.  The
        # current day may still be an in-progress window; absence of a workout
        # today is not an incompleteness signal.
        return bool(history.get("prior_7d_verified")) and _sleep_is_complete(daily)
    if history.get("status") in {"COMPLETE", "PARTIAL"}:
        return True
    # An unknown training window does not erase same-day facts.  Evening may
    # report observed sleep/activity/workout facts, but this path never grants
    # permission to render a training prescription.
    return _has_same_day_facts(daily)


def _has_same_day_facts(daily: dict) -> bool:
    features = daily.get("features") or {}
    sleep = features.get("sleep") or {}
    if sleep.get("status") == "AVAILABLE" and any(
        sleep.get(key) is not None
        for key in ("duration_minutes", "wake_time", "bedtime", "vendor_sleep_score")
    ):
        return True
    activity = features.get("activity") or {}
    if activity.get("status") == "AVAILABLE" or any(
        activity.get(key) is not None
        for key in ("steps", "step_count", "distance_km", "calories")
    ):
        return True
    training = features.get("training") or {}
    if training.get("status") == "AVAILABLE":
        return True
    for key in ("workouts", "recent_workouts", "sessions"):
        if training.get(key):
            return True
    return False


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
