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

ReportPeriod = Literal["morning", "evening"]


_UNUSABLE_SYNC_STATES = {"needs_reauth", "token_required", "failed", "cancelled"}
SCHEDULED_DELAY_SECONDS = {"morning": 3 * 3600, "evening": 6 * 3600}
PARTIAL_FALLBACK_DELAY_SECONDS = 30 * 60


def scheduled_report_window(target: date, period: ReportPeriod, timezone: str) -> dict:
    """A local cutoff plus a finite amount of real elapsed time in UTC."""
    cutoff = time(9, 30) if period == "morning" else time(21, 30)
    scheduled = datetime.combine(target, cutoff, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    deadline = scheduled + timedelta(seconds=SCHEDULED_DELAY_SECONDS[period])
    return {
        "scheduled_for": scheduled.isoformat().replace("+00:00", "Z"),
        "cutoff": scheduled.isoformat().replace("+00:00", "Z"),
        "deadline_at": deadline.isoformat().replace("+00:00", "Z"),
        "allowed_delay_seconds": SCHEDULED_DELAY_SECONDS[period],
        "partial_fallback_after": (scheduled + timedelta(seconds=PARTIAL_FALLBACK_DELAY_SECONDS)).isoformat().replace("+00:00", "Z"),
        "timezone": timezone,
        "required_signals": ["sleep_complete", "prior_7d_training_history"]
        if period == "morning" else ["same_day_facts", "training_history"],
    }


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


def sleep_is_complete(daily: dict) -> bool:
    sleep = daily.get("features", {}).get("sleep", {})
    wake_time = sleep.get("wake_time")
    return (
        sleep.get("status") == "AVAILABLE"
        and isinstance(wake_time, str)
        and bool(wake_time.strip())
    )


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
    features = daily.get("features") or {}
    sleep = features.get("sleep") or {}
    if sleep.get("status") in {"AVAILABLE", "PARTIAL"} and any(
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
) -> dict:
    window = scheduled_report_window(target, period, timezone)
    now = _as_aware(as_of, timezone).astimezone(UTC)
    scheduled = datetime.fromisoformat(window["scheduled_for"].replace("Z", "+00:00"))
    deadline = datetime.fromisoformat(window["deadline_at"].replace("Z", "+00:00"))
    fallback = datetime.fromisoformat(window["partial_fallback_after"].replace("Z", "+00:00"))
    context = daily.get("report_context") or {}
    metadata = {
        **window, "as_of": context.get("as_of"),
        "delivered_as_of": now.isoformat().replace("+00:00", "Z"),
        "late": now >= fallback,
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
    reason = "late_report" if late else missing[0] if missing else "sync_degraded" if sync_degraded else None
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


# Private spellings preserve the names used by the original policy tests while
# keeping the public functions descriptive for new callers.
_retrospective_age = retrospective_age
_sleep_is_complete = sleep_is_complete
_training_history = training_history
_morning_facts_only_reason = morning_facts_only_reason
_stored_profile_is_usable = stored_profile_is_usable
_has_same_day_facts = has_same_day_facts
