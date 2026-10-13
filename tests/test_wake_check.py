"""One-week review of wake-triggered morning reports."""

from datetime import date, datetime, time

from vitalis.application.wake_check import WakeDay, summarize


def _day(day, wake, synced, created, probes=2):
    return WakeDay(
        day=day, wake_time=wake, probes=probes,
        morning_sync_at=datetime.fromisoformat(f"{day}T{synced}:00") if synced else None,
        report_created_at=datetime.fromisoformat(f"{day}T{created}:00") if created else None,
        report_updated_at=None, report_status="delivered",
    )


def test_review_passes_when_reports_follow_waking_and_none_start_early():
    # Stored times are UTC; the review reads them in Asia/Shanghai (UTC+8).
    days = [
        _day(date(2026, 10, 14), "07:10", "23:20", "23:31"),
        _day(date(2026, 10, 15), "06:50", "23:05", "23:12"),
    ]
    result = summarize(days, timezone_name="UTC", poll_interval_seconds=900, window_end=time(11, 0))
    assert result["partial_sleep_reports"] == 0
    assert result["limit_minutes"] == 25


def test_report_started_before_the_stored_wake_time_counts_as_partial_sleep():
    days = [_day(date(2026, 10, 11), "07:30", "02:10", "02:20")]
    result = summarize(days, timezone_name="UTC", poll_interval_seconds=900, window_end=time(11, 0))
    assert result["partial_sleep_reports"] == 1 and result["passes"] is False
    assert result["days"][0]["started_before_wake"] is True


def test_median_delay_is_checked_against_poll_interval_plus_ten_minutes():
    days = [_day(date(2026, 10, 12), "07:00", "07:05", "07:20"), _day(date(2026, 10, 13), "07:00", "07:40", "07:50")]
    result = summarize(days, timezone_name="UTC", poll_interval_seconds=900, window_end=time(11, 0))
    assert result["median_minutes_after_wake"] == 35 and result["passes"] is False
