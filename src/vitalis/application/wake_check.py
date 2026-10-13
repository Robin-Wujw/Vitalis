"""Daily record of the wake-triggered morning report, for the one-week review.

Per day: the stored wake time, when the morning sync was queued (the first
probe that found a plausible main sleep, or the window end), how many probes
ran, and when the report intent was created and last updated.  A morning sync
queued before the finally stored wake time means the report started from a
partial overnight upload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WakeDay:
    day: date
    wake_time: str | None
    probes: int
    morning_sync_at: datetime | None
    report_created_at: datetime | None
    report_updated_at: datetime | None
    report_status: str | None


def _local(value: datetime | None, zone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(zone) if value.tzinfo is None else value.astimezone(zone)


def summarize(days: list[WakeDay], *, timezone_name: str, poll_interval_seconds: int,
              window_end: time) -> dict:
    zone = ZoneInfo(timezone_name)
    rows, delays = [], []
    partial = 0
    for item in sorted(days, key=lambda value: value.day):
        wake = None
        if item.wake_time:
            hour, minute = (int(part) for part in item.wake_time.split(":")[:2])
            wake = datetime.combine(item.day, time(hour, minute), tzinfo=zone)
        synced = _local(item.morning_sync_at, zone)
        created = _local(item.report_created_at, zone)
        started_early = bool(wake and synced and synced < wake)
        partial += int(started_early)
        at_window_end = bool(synced and synced.time() >= window_end)
        delay = round((created - wake).total_seconds() / 60) if wake and created and created >= wake else None
        if delay is not None:
            delays.append(delay)
        rows.append({
            "date": item.day.isoformat(),
            "wake_time": item.wake_time,
            "probes": item.probes,
            "morning_sync_at": synced.strftime("%H:%M") if synced else None,
            "report_created_at": created.strftime("%H:%M") if created else None,
            "report_status": item.report_status,
            "minutes_after_wake": delay,
            "started_before_wake": started_early,
            "waited_for_window_end": at_window_end,
        })
    limit = poll_interval_seconds / 60 + 10
    middle = median(delays) if delays else None
    return {
        "days": rows,
        "median_minutes_after_wake": middle,
        "limit_minutes": limit,
        "partial_sleep_reports": partial,
        "passes": bool(delays) and middle <= limit and partial == 0,
    }


def collect(repo, user_id: str, end: date, days: int) -> list[WakeDay]:
    output = []
    for offset in range(days):
        day = end - timedelta(days=offset)
        attempts = repo.sync_attempts_for_trigger_prefix(
            user_id, "zepp",
            {"morning": f"morning:{day.isoformat()}:", "morning_probe": f"morning-probe:{day.isoformat()}:"},
        )
        syncs = sorted((row for row in attempts if row.trigger == "morning"), key=lambda row: row.created_at)
        delivery = repo.notification_delivery_for_key(user_id, day, "morning")
        wake_time, _ = repo.sleep_wake_summary(user_id, day)
        output.append(WakeDay(
            day=day,
            wake_time=wake_time,
            probes=sum(1 for row in attempts if row.trigger == "morning_probe"),
            morning_sync_at=syncs[0].created_at if syncs else None,
            report_created_at=delivery.created_at if delivery is not None else None,
            report_updated_at=delivery.updated_at if delivery is not None else None,
            report_status=delivery.status if delivery is not None else None,
        ))
    return output
