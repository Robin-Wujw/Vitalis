from datetime import date

import pytest

from vitalis.intelligence.report_periods import (
    PeriodMode,
    resolve_month_period,
    resolve_week_period,
)


def test_calendar_week_ends_before_target_week():
    period = resolve_week_period(date(2026, 8, 28))

    assert period.mode is PeriodMode.CALENDAR
    assert (period.start, period.end) == (date(2026, 8, 17), date(2026, 8, 23))
    assert (period.reference_start, period.reference_end) == (
        date(2026, 8, 10),
        date(2026, 8, 16),
    )
    assert period.metadata()["period_mode"] == "calendar"


def test_calendar_month_handles_short_february_and_previous_month():
    period = resolve_month_period(date(2026, 3, 15))

    assert (period.start, period.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert (period.reference_start, period.reference_end) == (
        date(2026, 1, 1),
        date(2026, 1, 31),
    )
    assert period.days == 28
    assert period.reference_days == 31


def test_rolling_periods_keep_explicit_window_labels():
    week = resolve_week_period(date(2026, 8, 28), "rolling_7d")
    month = resolve_month_period(date(2026, 8, 28), "rolling_28d")

    assert week.mode is month.mode is PeriodMode.ROLLING
    assert (week.start, week.end) == (date(2026, 8, 22), date(2026, 8, 28))
    assert (month.start, month.end) == (date(2026, 8, 1), date(2026, 8, 28))
    assert week.metadata()["period_kind"] == "rolling_week"
    assert month.metadata()["period_kind"] == "rolling_month"


def test_unknown_period_mode_is_rejected():
    with pytest.raises(ValueError, match="period mode"):
        resolve_week_period(date(2026, 8, 28), "fiscal")
