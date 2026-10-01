"""Calendar and explicitly rolling report period resolution.

The report engines analyse a RawDailyProfile whose ``day`` is the requested
analysis target.  Calendar reports end at the last completed local calendar
period before that target; rolling reports retain the historical direct-call
window semantics and are labelled as such in report metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class PeriodMode(StrEnum):
    CALENDAR = "calendar"
    ROLLING = "rolling"


@dataclass(frozen=True)
class ReportPeriod:
    """A period and its immediately preceding reference period."""

    kind: str
    mode: PeriodMode
    start: date
    end: date
    reference_start: date
    reference_end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def reference_days(self) -> int:
        return (self.reference_end - self.reference_start).days + 1

    def metadata(self) -> dict[str, object]:
        return {
            "period_kind": self.kind,
            "period_mode": self.mode.value,
            "selected_mode": self.mode.value,
            "period_start": self.start.isoformat(),
            "period_end": self.end.isoformat(),
            "period_days": self.days,
            "reference_period": {
                "start": self.reference_start.isoformat(),
                "end": self.reference_end.isoformat(),
                "days": self.reference_days,
            },
        }


def normalize_period_mode(value: PeriodMode | str | None) -> PeriodMode:
    """Normalize public mode aliases while keeping the public contract small."""
    if value is None:
        return PeriodMode.CALENDAR
    if isinstance(value, PeriodMode):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"calendar", "calendar_week", "calendar_month"}:
        return PeriodMode.CALENDAR
    if normalized in {"rolling", "rolling_7d", "rolling_28d", "rolling_month"}:
        return PeriodMode.ROLLING
    raise ValueError("period mode must be 'calendar' or 'rolling'")


def resolve_week_period(
    target_day: date, mode: PeriodMode | str = PeriodMode.CALENDAR
) -> ReportPeriod:
    """Resolve a Monday-to-Sunday week and its preceding comparison week."""
    selected = normalize_period_mode(mode)
    if selected is PeriodMode.ROLLING:
        end = target_day
        start = end - timedelta(days=6)
        kind = "rolling_week"
    else:
        current_week_start = target_day - timedelta(days=target_day.weekday())
        end = current_week_start - timedelta(days=1)
        start = end - timedelta(days=6)
        kind = "calendar_week"
    reference_end = start - timedelta(days=1)
    reference_start = reference_end - timedelta(days=6)
    return ReportPeriod(
        kind=kind,
        mode=selected,
        start=start,
        end=end,
        reference_start=reference_start,
        reference_end=reference_end,
    )


def resolve_month_period(
    target_day: date, mode: PeriodMode | str = PeriodMode.CALENDAR
) -> ReportPeriod:
    """Resolve a complete local calendar month and the preceding month."""
    selected = normalize_period_mode(mode)
    if selected is PeriodMode.ROLLING:
        end = target_day
        start = end - timedelta(days=27)
        kind = "rolling_month"
        reference_end = start - timedelta(days=1)
        reference_start = reference_end - timedelta(days=27)
    else:
        current_month_start = target_day.replace(day=1)
        end = current_month_start - timedelta(days=1)
        start = end.replace(day=1)
        reference_end = start - timedelta(days=1)
        reference_start = reference_end.replace(day=1)
        kind = "calendar_month"
    return ReportPeriod(
        kind=kind,
        mode=selected,
        start=start,
        end=end,
        reference_start=reference_start,
        reference_end=reference_end,
    )


# Short aliases make the resolver convenient for callers and tests without
# duplicating the period arithmetic in report engines.
weekly_period = resolve_week_period
monthly_period = resolve_month_period

__all__ = [
    "PeriodMode",
    "ReportPeriod",
    "normalize_period_mode",
    "resolve_week_period",
    "resolve_month_period",
    "weekly_period",
    "monthly_period",
]
