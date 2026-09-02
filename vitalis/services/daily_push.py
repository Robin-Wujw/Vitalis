"""Run deterministic daily PushPlus reports through the public Vitalis API."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date
import hashlib
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
from vitalis.time import local_today


ReportPeriod = Literal["morning", "evening"]
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "vitalis_push"


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

    guard = nullcontext() if test_delivery else _delivery_lock(marker)
    with guard:
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
            sync = _sync_health(client, days)
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

        if period == "morning" and not _sleep_is_complete(daily):
            return {
                "status": "deferred",
                "period": period,
                "date": current_date.isoformat(),
                "reason": "sleep_incomplete",
                "sync_degraded": sync_degraded,
                "sync_status": sync_status,
            }

        if sync_degraded:
            daily = deepcopy(daily)
            daily["delivery_metadata"] = {
                "sync_degraded": True,
                "sync_status": sync_status,
                "sync_detail": sync_detail,
            }
        results = PushService(pushplus_token=pushplus_token).push_daily_profile(
            user_id, daily, period=period
        )
        if results.get("_pushplus_handler") != "ok":
            raise RuntimeError("PushPlus delivery failed")
        if not test_delivery:
            _mark_delivered(marker)
        outcome = {
            "status": "test_sent" if test_delivery else "sent",
            "period": period,
            "date": daily["date"],
            "quality": daily["data_quality"]["status"],
            "sync_degraded": sync_degraded,
            "sync_status": sync_status,
        }
        if test_delivery:
            outcome["scheduled_delivery_unchanged"] = True
        return outcome


def _sync_health(client: httpx.Client, days: int) -> dict:
    try:
        response = client.post("/api/v1/health/sync", params={"days": days})
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
    if daily.get("data_quality", {}).get("status") != "SUFFICIENT":
        return False
    return period != "morning" or _sleep_is_complete(daily)


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
