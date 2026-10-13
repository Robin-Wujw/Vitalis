"""Calendar-week training-load target, progress and today's share.

The weekly target follows the last four complete weeks (the user's goal is
general health, so a gentle 5 % progression).  The acute-to-chronic ratio of
the last 7 against the last 28 days sorts the recent load into building,
maintaining or excessive.  Days without a verified training record are
unknown, so any gap in the 28-day reference leaves the plan absent.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from .contracts import TrainingPreferences, WeeklyLoadPlan
from .profile import RawDailyProfile

HEALTH_PROGRESSION = 1.05
OVER_TARGET = 1.10
CATCH_UP_PACE = 0.75
# A catch-up day may take at most this much more than an even share.
CATCH_UP_CAP = 1.25
STATUS_LABELS = {
    "WEEK_START": "本周刚开始",
    "ON_TRACK": "进度正常",
    "CATCH_UP": "需要补齐",
    "OVER": "已超过目标",
}
TIER_LABELS = {"BUILDING": "提升", "MAINTAINING": "维持", "EXCESSIVE": "过量"}


def _verified(raw: RawDailyProfile) -> set[date]:
    coverage = raw.training_history_coverage or {}
    return {
        value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        for value in coverage.get("verified_days", [])
        if isinstance(value, (date, str))
    }


def _load(raw: RawDailyProfile, day: date, verified: set[date]) -> float | None:
    record = raw.training_by_day.get(day)
    if record is not None:
        value = record.get("total_load")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return 0.0 if day in verified else None


def _sum(raw: RawDailyProfile, start: date, end: date, verified: set[date]) -> float | None:
    total = 0.0
    day = start
    while day <= end:
        value = _load(raw, day, verified)
        if value is None:
            return None
        total += value
        day += timedelta(days=1)
    return total


def _load_per_minute(raw: RawDailyProfile, day: date) -> float | None:
    ratios = []
    for item_day, record in raw.training_by_day.items():
        if not day - timedelta(days=27) <= item_day < day:
            continue
        load, duration = record.get("total_load"), record.get("total_duration")
        if isinstance(load, (int, float)) and isinstance(duration, (int, float)) and load > 0 and duration > 0:
            ratios.append(float(load) / float(duration))
    return float(median(ratios)) if len(ratios) >= 3 else None


def _sessions_done(raw: RawDailyProfile, start: date, end: date) -> int:
    return sum(
        int(record.get("workout_count") or 0)
        for item_day, record in raw.training_by_day.items()
        if start <= item_day <= end
    )


def weekly_load_plan(
    raw: RawDailyProfile, preferences: TrainingPreferences | None,
) -> WeeklyLoadPlan | None:
    day = raw.day
    verified = _verified(raw)
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=6)
    chronic_total = _sum(raw, week_start - timedelta(days=28), week_start - timedelta(days=1), verified)
    if chronic_total is None or chronic_total <= 0:
        return None
    chronic = chronic_total / 4
    target = chronic * HEALTH_PROGRESSION
    before_today = _sum(raw, week_start, day - timedelta(days=1), verified) if day > week_start else 0.0
    if before_today is None:
        return None
    today_load = _load(raw, day, verified)
    done = before_today + (today_load or 0.0)
    remaining = max(0.0, target - done)
    elapsed_days = (day - week_start).days
    expected = target * elapsed_days / 7
    if done >= target * OVER_TARGET:
        status = "OVER"
    elif elapsed_days == 0 and done == 0:
        status = "WEEK_START"
    elif done < expected * CATCH_UP_PACE:
        status = "CATCH_UP"
    else:
        status = "ON_TRACK"

    prefs = preferences
    available = set(prefs.available_weekdays) if prefs and prefs.available_weekdays else set(range(1, 8))
    days_left = sum(
        1 for offset in range((week_end - day).days + 1)
        if (day + timedelta(days=offset)).isoweekday() in available
    )
    planned = min(7, (prefs.weekly_running_target + prefs.weekly_strength_target) if prefs else 6)
    sessions_left = max(0, planned - _sessions_done(raw, week_start, day - timedelta(days=1)))
    share_count = max(1, min(days_left, sessions_left)) if days_left else 0

    acute = _sum(raw, day - timedelta(days=6), day, verified)
    chronic_28 = _sum(raw, day - timedelta(days=27), day, verified)
    ratio = round(acute / (chronic_28 / 4), 2) if acute is not None and chronic_28 else None
    tier = None
    if ratio is not None:
        tier = "EXCESSIVE" if ratio > 1.3 else "BUILDING" if ratio > 1.05 else "MAINTAINING"

    per_minute = _load_per_minute(raw, day)
    today_target = None
    if status != "OVER" and share_count and remaining > 0 and day.isoweekday() in available:
        even = remaining / share_count
        today_target = min(even * CATCH_UP_CAP, remaining) if status == "CATCH_UP" else even
    minutes = None
    if today_target is not None and per_minute:
        cap = (prefs.max_session_minutes if prefs and prefs.max_session_minutes else 90)
        minutes = int(round(min(max(today_target / per_minute, 20), cap)))

    if status == "OVER":
        reason = f"本周负荷已达目标的 {done / target:.0%}，今天以恢复为主。"
    elif status == "CATCH_UP":
        reason = f"本周已完成目标的 {done / target:.0%}，落后于进度，剩余 {days_left} 天内补齐。"
    elif status == "WEEK_START":
        reason = f"新的一周从近 4 周周均负荷 {chronic:.0f} 起步，目标 {target:.0f}。"
    else:
        reason = f"本周已完成目标的 {done / target:.0%}，按剩余 {share_count} 次平均分配。"
    return WeeklyLoadPlan(
        week_start=week_start,
        week_end=week_end,
        target_load=round(target, 1),
        chronic_weekly_load=round(chronic, 1),
        done_load=round(done, 1),
        today_load=round(today_load, 1) if today_load is not None else None,
        remaining_load=round(remaining, 1),
        progress_ratio=round(done / target, 3),
        days_left=days_left,
        sessions_left=sessions_left,
        status=status,
        status_label=STATUS_LABELS[status],
        acute_chronic_ratio=ratio,
        load_tier=tier,
        load_tier_label=TIER_LABELS[tier] if tier else None,
        today_target_load=round(today_target, 1) if today_target is not None else None,
        today_minutes=minutes,
        load_per_minute=round(per_minute, 3) if per_minute else None,
        reason=reason,
    )
