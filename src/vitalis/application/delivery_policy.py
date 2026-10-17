"""Pure eligibility and payload policy for daily report delivery.

This module deliberately knows nothing about transport, persistence, filesystem
markers, process settings, or clock access. Callers provide the local date and
comparison instant explicitly so manual and scheduled delivery share one gate.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from vitalis.intelligence.sleep_completion import (
    MIN_MAIN_SLEEP_MINUTES,
    main_sleep_finished,
    minutes_recorded_after,
)
from vitalis.time import local_clock_at

ReportPeriod = Literal["morning", "evening"]


_UNUSABLE_SYNC_STATES = {"needs_reauth", "token_required", "failed", "cancelled"}
# Mornings follow the wake time: the report may go out from the window start,
# stops waiting for a finished sleep at the window end (13:00, user decision
# 2026-10-17), and only then turns late.
DEFAULT_MORNING_WINDOW = (time(7, 30), time(13, 0))
EVENING_SCHEDULE = time(21, 30)
LATE_AFTER_SECONDS = 30 * 60
MORNING_DEADLINE_AFTER_WINDOW_SECONDS = 90 * 60
EVENING_DEADLINE_SECONDS = 6 * 3600
EVENING_DELIVERY_TARGET_SECONDS = 60 * 60
# A main sleep ending this long before the window start is treated as a record
# the phone uploaded while the user was still asleep.
MORNING_EARLIEST_WAKE_BEFORE_WINDOW = timedelta(hours=3)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def scheduled_report_window(
    target: date,
    period: ReportPeriod,
    timezone: str,
    *,
    morning_window: tuple[time, time] = DEFAULT_MORNING_WINDOW,
) -> dict:
    """Local report times expressed as finite real elapsed time in UTC.

    ``scheduled_for`` is the earliest send time, ``partial_fallback_after`` ends
    the wait for required data, ``late_after`` turns a send into a facts-only
    report, ``delivery_target_at`` is the alert target, and ``deadline_at``
    abandons the report.
    """
    zone = ZoneInfo(timezone)
    # Local wall-clock anchors; every offset below is real elapsed time in UTC.
    if period == "morning":
        start, end = morning_window
        if end <= start:
            raise ValueError("morning window end must be later than its start")
        scheduled = datetime.combine(target, start, tzinfo=zone).astimezone(UTC)
        fallback = datetime.combine(target, end, tzinfo=zone).astimezone(UTC)
        late_after = fallback + timedelta(seconds=LATE_AFTER_SECONDS)
        target_at = late_after
        deadline = fallback + timedelta(seconds=MORNING_DEADLINE_AFTER_WINDOW_SECONDS)
    else:
        scheduled = datetime.combine(target, EVENING_SCHEDULE, tzinfo=zone).astimezone(UTC)
        fallback = late_after = scheduled + timedelta(seconds=LATE_AFTER_SECONDS)
        target_at = scheduled + timedelta(seconds=EVENING_DELIVERY_TARGET_SECONDS)
        deadline = scheduled + timedelta(seconds=EVENING_DEADLINE_SECONDS)
    return {
        "scheduled_for": _utc_text(scheduled),
        "partial_fallback_after": _utc_text(fallback),
        "late_after": _utc_text(late_after),
        "delivery_target_at": _utc_text(target_at),
        "deadline_at": _utc_text(deadline),
        "allowed_delay_seconds": int((deadline - scheduled).total_seconds()),
        "timezone": timezone,
        "required_signals": ["sleep_complete", "prior_7d_training_history"]
        if period == "morning" else ["same_day_facts", "training_history"],
    }


def wake_time_is_plausible(
    target: date,
    wake_time: str | time | None,
    sleep_minutes: float | None,
    *,
    morning_window: tuple[time, time] = DEFAULT_MORNING_WINDOW,
) -> bool:
    """Whether a stored main sleep is finished enough to trigger the morning report.

    Zepp keeps a sleep the phone uploaded mid-night with the upload time as its
    wake time (2026-10-11: a record fetched at 02:00 stayed unchanged until the
    09:57 wake). Such a record is short or ends long before the window.
    """
    if wake_time is None or sleep_minutes is None or sleep_minutes < MIN_MAIN_SLEEP_MINUTES:
        return False
    if isinstance(wake_time, str):
        try:
            wake_time = time.fromisoformat(wake_time.strip())
        except ValueError:
            return False
    earliest = datetime.combine(target, morning_window[0]) - MORNING_EARLIEST_WAKE_BEFORE_WINDOW
    return datetime.combine(target, wake_time) >= earliest


def morning_sleep_has_ended(
    target: date,
    wake_time: str | None,
    sleep_minutes: float | None,
    *,
    usual_minutes: float | None,
    data_until: datetime | None,
    timezone: str,
    morning_window: tuple[time, time] = DEFAULT_MORNING_WINDOW,
) -> bool:
    """Whether the stored main sleep may start the morning report.

    Besides a plausible wake time, the sleep must have finished: close to the
    usual night, or followed by half an hour of uploaded watch data. A brief
    awakening that made the phone upload (2026-10-17: 02:26-07:04, 275 minutes,
    then asleep past 09:00) is neither.
    """
    if not wake_time_is_plausible(target, wake_time, sleep_minutes, morning_window=morning_window):
        return False
    wake_at = local_clock_at(target, wake_time.strip() if isinstance(wake_time, str) else wake_time, timezone)
    return main_sleep_finished(sleep_minutes, usual_minutes, minutes_recorded_after(wake_at, data_until))


def retrospective_age(
    target: date | None,
    today: date,
    period: ReportPeriod,
    test_delivery: bool,
) -> int:
    """Validate a bounded explicit-date evening replay and return its age."""
    if target is None or period != "evening" or not test_delivery:
        raise ValueError("指定日期补发仅支持明确日期的测试晚报")
    age = (today - target).days
    if not 0 <= age <= 6:
        raise ValueError("晚报补发仅支持最近七天，不支持未来日期")
    return age


def _stored_sleep(daily: dict) -> dict | None:
    """Last night's main sleep when the snapshot has one with a wake time."""
    sleep = daily.get("features", {}).get("sleep", {})
    wake_time = sleep.get("wake_time")
    if sleep.get("status") == "AVAILABLE" and isinstance(wake_time, str) and wake_time.strip():
        return sleep
    return None


def sleep_is_complete(daily: dict) -> bool:
    sleep = _stored_sleep(daily)
    return sleep is not None and sleep.get("finished") is True


def sleep_is_unconfirmed(daily: dict) -> bool:
    """A stored main sleep that may still be cut short by a mid-sleep upload."""
    sleep = _stored_sleep(daily)
    return sleep is not None and sleep.get("finished") is not True


def training_history(daily: dict) -> dict | None:
    context = daily.get("report_context")
    history = context.get("training_history") if isinstance(context, dict) else None
    if isinstance(history, dict):
        return history
    training = (daily.get("features") or {}).get("training") or {}
    history = training.get("history_coverage") if isinstance(training, dict) else None
    return history if isinstance(history, dict) else None


def morning_facts_only_reason(daily: dict) -> str | None:
    """Return the coverage reason for a safe, non-prescriptive morning report."""
    if not sleep_is_complete(daily):
        return None
    if (daily.get("data_quality") or {}).get("status") not in {"SUFFICIENT", "PARTIAL"}:
        return None
    history = training_history(daily)
    if history is None:
        return "training_history_missing"
    if history.get("prior_7d_verified") is True:
        return None
    return "prior_7d_unverified"


def stored_profile_is_usable(daily: dict, day: date, period: ReportPeriod) -> bool:
    """Check whether saved observations can support this period's report."""
    if daily.get("date") != day.isoformat():
        return False
    history = training_history(daily)
    if period == "morning":
        # Full reports need verified history; facts-only admits the narrower gate.
        return sleep_is_complete(daily) and (
            (isinstance(history, dict) and history.get("prior_7d_verified") is True)
            or morning_facts_only_reason(daily) is not None
        )
    if not isinstance(history, dict):
        return False
    if history.get("status") in {"COMPLETE", "PARTIAL"}:
        return True
    # Unknown history may still support observed same-day facts, but never a plan.
    return has_same_day_facts(daily)


def has_same_day_facts(daily: dict) -> bool:
    """Return true only when the daily projection contains observed facts."""
    features = daily.get("features") or {}
    sleep = features.get("sleep") or {}
    if sleep.get("status") in {"AVAILABLE", "PARTIAL"} and any(
        sleep.get(key) is not None
        for key in ("duration_minutes", "wake_time", "bedtime", "vendor_sleep_score")
    ):
        return True

    activity = features.get("activity") or {}
    for key in ("steps", "step_count", "distance_km", "active_minutes", "calories"):
        value = activity.get(key)
        if isinstance(value, dict):
            if value.get("value") is not None:
                return True
        elif value is not None:
            return True
    if activity.get("energy"):
        return True

    target_day = daily.get("date")
    training = features.get("training") or {}
    if any(
        training.get(key) is not None
        for key in ("today_duration_minutes", "today_load")
    ):
        return True
    for key in ("workouts", "recent_workouts", "sessions"):
        items = training.get(key) or []
        if any(
            isinstance(item, dict) and (
                item.get("date") == target_day
                or item.get("calendar_day") == target_day
            )
            for item in items
        ):
            return True
    return False


def _as_aware(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=ZoneInfo(timezone_name))


def _expiry_is_reached(
    plan_expires_at: datetime | None,
    as_of: datetime,
    timezone_name: str,
) -> bool:
    if plan_expires_at is None:
        return False
    expiry = _as_aware(plan_expires_at, "UTC")
    now = _as_aware(as_of, timezone_name)
    return now.astimezone(UTC) >= expiry.astimezone(UTC)


def prepare_delivery(
    daily: dict,
    *,
    period: ReportPeriod,
    target_date: date | None,
    today: date,
    as_of: datetime,
    timezone: str,
    test_delivery: bool = False,
    already_sent: bool = False,
    sync_degraded: bool = False,
    sync_status: str | None = None,
    sync_detail: str | None = None,
    plan_expires_at: datetime | None = None,
    retrospective: bool = False,
    scheduled_delivery: bool = False,
    morning_window: tuple[time, time] = DEFAULT_MORNING_WINDOW,
) -> dict:
    """Apply delivery gates and return either a deferred result or send payload.

    The first gate is the caller-provided authoritative idempotency result. This
    lets an adapter perform an atomic marker or database claim before expensive
    validation while keeping all report eligibility decisions pure.
    """
    current_date = target_date or today
    if already_sent:
        return {
            "status": "already_sent",
            "period": period,
            "date": current_date.isoformat(),
        }
    if scheduled_delivery and not retrospective:
        return _prepare_scheduled_delivery(
            daily, period=period, target=current_date, as_of=as_of, timezone=timezone,
            test_delivery=test_delivery, sync_degraded=sync_degraded,
            sync_status=sync_status, plan_expires_at=plan_expires_at,
            morning_window=morning_window,
        )
    if retrospective:
        retrospective_age(target_date, today, period, test_delivery)
    elif current_date != today:
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "stale_report_date",
        }
    if not retrospective and _expiry_is_reached(plan_expires_at, as_of, timezone):
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "stale_plan_expired",
        }
    if sync_status in _UNUSABLE_SYNC_STATES:
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "unusable_sync_state",
            "sync_status": sync_status,
        }
    if daily.get("date") != current_date.isoformat():
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "stale_report_date",
            "report_date": daily.get("date"),
        }
    if period == "morning" and not sleep_is_complete(daily):
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "sleep_incomplete",
            "sync_degraded": sync_degraded,
            "sync_status": sync_status,
        }
    if not stored_profile_is_usable(daily, current_date, period):
        return {
            "status": "deferred",
            "period": period,
            "date": current_date.isoformat(),
            "reason": "stored_data_incomplete",
            "sync_degraded": sync_degraded,
            "sync_status": sync_status,
        }

    facts_only_reason = morning_facts_only_reason(daily) if period == "morning" else None
    facts_only = facts_only_reason is not None
    payload = deepcopy(daily) if sync_degraded or retrospective or facts_only else daily
    if sync_degraded or retrospective or facts_only:
        metadata = dict(payload.get("delivery_metadata") or {})
        if sync_degraded:
            metadata.update(
                sync_degraded=True,
                sync_status=sync_status,
                sync_detail=sync_detail,
            )
        if retrospective:
            metadata["retrospective"] = True
            payload.pop("decision", None)
        if facts_only:
            metadata.update(facts_only=True, coverage_reason=facts_only_reason)
        payload["delivery_metadata"] = metadata
    return {
        "status": "ready",
        "period": period,
        "date": current_date.isoformat(),
        "payload": payload,
        "facts_only": facts_only,
        "facts_only_reason": facts_only_reason,
        "sync_degraded": sync_degraded,
        "sync_status": sync_status,
        "retrospective": retrospective,
        "test_delivery": test_delivery,
    }


def _prepare_scheduled_delivery(
    daily: dict, *, period: ReportPeriod, target: date, as_of: datetime,
    timezone: str, test_delivery: bool, sync_degraded: bool,
    sync_status: str | None, plan_expires_at: datetime | None,
    morning_window: tuple[time, time] = DEFAULT_MORNING_WINDOW,
) -> dict:
    window = scheduled_report_window(target, period, timezone, morning_window=morning_window)
    now = _as_aware(as_of, timezone).astimezone(UTC)
    scheduled = datetime.fromisoformat(window["scheduled_for"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(window["deadline_at"].replace("Z", "+00:00"))
    fallback = datetime.fromisoformat(window["partial_fallback_after"].replace("Z", "+00:00"))
    late_after = datetime.fromisoformat(window["late_after"].replace("Z", "+00:00"))
    context = daily.get("report_context") or {}
    metadata = {
        **window, "as_of": context.get("as_of"),
        "delivered_as_of": now.isoformat().replace("+00:00", "Z"),
        "late": now >= late_after,
        "delay_seconds": max(0, (now - scheduled).total_seconds()),
    }
    result = {"period": period, "date": target.isoformat(), "delivery_metadata": metadata}
    if now >= deadline or _expiry_is_reached(plan_expires_at, as_of, timezone):
        metadata["failure_code"] = "delivery_deadline_expired"
        return {**result, "status": "deferred", "reason": "stale_plan_expired"}
    if now < scheduled:
        metadata["failure_code"] = "before_report_cutoff"
        return {**result, "status": "deferred", "reason": "stored_data_incomplete"}
    if sync_status in _UNUSABLE_SYNC_STATES:
        return {**result, "status": "deferred", "reason": "unusable_sync_state", "sync_status": sync_status}
    if daily.get("date") != target.isoformat():
        return {**result, "status": "deferred", "reason": "stale_report_date", "report_date": daily.get("date")}
    missing = []
    history = training_history(daily)
    if period == "morning":
        if not sleep_is_complete(daily):
            missing.append("sleep_complete")
        if not history or history.get("prior_7d_verified") is not True:
            missing.append("prior_7d_training_history")
        if "sleep_complete" in missing and now < fallback:
            metadata["missing_signals"] = missing
            return {**result, "status": "deferred", "reason": "sleep_incomplete"}
    else:
        if not has_same_day_facts(daily):
            missing.append("same_day_facts")
        if not history or not (
            history.get("prior_7d_verified") is True or history.get("status") == "COMPLETE"
        ):
            missing.append("training_history")
    metadata["missing_signals"] = missing
    if not has_same_day_facts(daily):
        return {**result, "status": "deferred", "reason": "stored_data_incomplete"}
    late = metadata["late"]
    facts_only = bool(missing or sync_degraded or late)
    if period == "morning" and "sleep_complete" in missing:
        reason = "sleep_unconfirmed" if sleep_is_unconfirmed(daily) else "sleep_incomplete"
    elif late:
        reason = "late_report"
    elif missing:
        reason = missing[0]
    elif sync_degraded:
        reason = "sync_degraded"
    else:
        reason = None
    metadata.update(
        partial=bool(missing or sync_degraded or context.get("target_day_complete") is False),
        facts_only=facts_only, coverage_reason=reason,
        sync_degraded=sync_degraded, sync_status=sync_status,
    )
    payload = deepcopy(daily)
    if facts_only:
        for key in ("decision", "actions", "action_plan", "suggestions", "findings", "inferences"):
            payload.pop(key, None)
    payload["delivery_metadata"] = metadata
    payload["report_context"] = {**context, "delivery_metadata": metadata}
    return {
        **result, "status": "ready", "payload": payload,
        "facts_only": facts_only, "facts_only_reason": reason,
        "sync_degraded": sync_degraded, "sync_status": sync_status,
        "retrospective": False, "test_delivery": test_delivery,
    }
