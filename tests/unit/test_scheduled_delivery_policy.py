"""Pure finite report deadlines, partial fallback, and DST elapsed time."""

from copy import deepcopy
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from vitalis.application import delivery_policy
from vitalis.intelligence.public_reports import SLEEP_UNCONFIRMED_NOTICE, facts_only_notice


DAY = date(2026, 10, 9)


def profile(*, period="evening"):
    return {
        "analysis_run_id": "synthetic-report", "date": DAY.isoformat(),
        "data_quality": {"status": "PARTIAL"},
        "report_context": {
            "timezone": "UTC", "as_of": "2026-10-09T21:45:00Z",
            "target_day_complete": False,
            "training_history": {"status": "PARTIAL", "prior_7d_verified": False},
        },
        "features": {
            "sleep": {"status": "PARTIAL", "duration_minutes": 420, "wake_time": None},
            "activity": {"status": "AVAILABLE", "steps": {"value": 4321, "unit": "steps"}},
        },
        "decision": {"action": "TRAIN_NORMAL", "action_plan": {"title": "synthetic plan"}},
    }


def prepare(payload, *, period="evening", now=None, today=None, **kwargs):
    now = now or datetime(2026, 10, 10, 0, 15, tzinfo=UTC)
    return delivery_policy.prepare_delivery(
        payload, period=period, target_date=DAY, today=today or now.date(),
        as_of=now, timezone="UTC", scheduled_delivery=True, **kwargs,
    )


def test_scheduled_evening_survives_midnight_as_visible_facts_only():
    payload = profile()
    original = deepcopy(payload)
    outcome = prepare(payload)
    assert outcome["status"] == "ready"
    assert outcome["facts_only"] is True
    metadata = outcome["payload"]["delivery_metadata"]
    assert metadata["late"] is True
    assert metadata["partial"] is True
    assert metadata["as_of"] == "2026-10-09T21:45:00Z"
    assert metadata["delivered_as_of"] == "2026-10-10T00:15:00Z"
    assert metadata["scheduled_for"] == "2026-10-09T23:00:00Z"
    assert metadata["late_after"] == "2026-10-09T23:30:00Z"
    assert metadata["delivery_target_at"] == "2026-10-10T00:00:00Z"
    assert metadata["deadline_at"] == "2026-10-10T00:30:00Z"
    assert metadata["allowed_delay_seconds"] == 5400
    assert "decision" not in outcome["payload"]
    assert outcome["payload"]["report_context"]["delivery_metadata"] == metadata
    assert payload == original


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 10, 0, 30, tzinfo=UTC),
    datetime(2026, 10, 10, 5, 0, tzinfo=UTC),
])
def test_scheduled_evening_is_dropped_at_half_past_midnight(now):
    result = prepare(profile(), now=now)
    assert result["status"] == "deferred"
    assert result["reason"] == "stale_plan_expired"
    assert result["delivery_metadata"]["failure_code"] == "delivery_deadline_expired"
    assert result["delivery_metadata"]["deadline_at"] == "2026-10-10T00:30:00Z"


def test_scheduled_evening_cannot_send_without_any_observed_facts():
    payload = profile()
    payload["features"] = {}
    payload["report_context"]["training_history"]["status"] = "UNKNOWN"
    result = prepare(payload)
    assert result["status"] == "deferred"
    assert result["reason"] == "stored_data_incomplete"


def test_scheduled_morning_waits_for_sleep_until_the_wake_window_ends():
    payload = profile(period="morning")
    payload["report_context"]["as_of"] = "2026-10-09T10:00:00Z"
    waiting = prepare(payload, period="morning", now=datetime(2026, 10, 9, 12, 55, tzinfo=UTC))
    assert waiting["status"] == "deferred"
    assert waiting["reason"] == "sleep_incomplete"
    result = prepare(payload, period="morning", now=datetime(2026, 10, 9, 13, 5, tzinfo=UTC))
    assert result["status"] == "ready"
    assert result["facts_only"] is True
    assert result["facts_only_reason"] == "sleep_incomplete"
    assert result["payload"]["delivery_metadata"]["late"] is False
    assert "sleep_complete" in result["payload"]["delivery_metadata"]["missing_signals"]
    assert "decision" not in result["payload"]
    assert prepare(payload, period="morning", now=datetime(2026, 10, 9, 14, 30, tzinfo=UTC))["reason"] == "stale_plan_expired"


def test_a_sleep_cut_short_by_an_upload_waits_then_goes_out_as_facts_only():
    payload = profile(period="morning")
    payload["features"]["sleep"] = {
        "status": "AVAILABLE", "duration_minutes": 275, "wake_time": "07:04:00",
        "usual_duration_minutes": 439.5, "recorded_after_wake_minutes": 4, "finished": False,
    }
    payload["report_context"]["training_history"] = {"status": "COMPLETE", "prior_7d_verified": True}
    assert delivery_policy.sleep_is_complete(payload) is False
    waiting = prepare(payload, period="morning", now=datetime(2026, 10, 9, 7, 35, tzinfo=UTC))
    assert (waiting["status"], waiting["reason"]) == ("deferred", "sleep_incomplete")
    result = prepare(payload, period="morning", now=datetime(2026, 10, 9, 13, 5, tzinfo=UTC))
    assert result["status"] == "ready"
    assert (result["facts_only"], result["facts_only_reason"]) == (True, "sleep_unconfirmed")
    assert "decision" not in result["payload"]
    assert result["payload"]["features"]["sleep"]["duration_minutes"] == 275
    assert facts_only_notice(result["payload"]["delivery_metadata"], morning=True) == SLEEP_UNCONFIRMED_NOTICE


def test_morning_window_follows_configured_wake_times():
    window = delivery_policy.scheduled_report_window(
        DAY, "morning", "Asia/Shanghai", morning_window=(time(6, 45), time(10, 15)),
    )
    assert window["scheduled_for"] == "2026-10-08T22:45:00Z"
    assert window["partial_fallback_after"] == "2026-10-09T02:15:00Z"
    assert window["late_after"] == window["delivery_target_at"] == "2026-10-09T02:45:00Z"
    assert window["deadline_at"] == "2026-10-09T03:45:00Z"
    with pytest.raises(ValueError):
        delivery_policy.scheduled_report_window(
            DAY, "morning", "UTC", morning_window=(time(11), time(7, 30)),
        )


def test_full_morning_report_can_go_out_as_soon_as_the_window_opens():
    payload = profile(period="morning")
    payload["features"]["sleep"] = {
        "status": "AVAILABLE", "duration_minutes": 420, "wake_time": "07:20:00", "finished": True,
    }
    payload["report_context"]["training_history"] = {"status": "COMPLETE", "prior_7d_verified": True}
    assert prepare(payload, period="morning", now=datetime(2026, 10, 9, 7, 25, tzinfo=UTC))["status"] == "deferred"
    result = prepare(payload, period="morning", now=datetime(2026, 10, 9, 7, 35, tzinfo=UTC))
    assert result["status"] == "ready"
    assert result["facts_only"] is False
    assert result["payload"]["decision"]["action"] == "TRAIN_NORMAL"


@pytest.mark.parametrize(("wake", "minutes", "expected"), [
    ("08:36:00", 455, True),
    ("04:30:00", 300, True),
    ("04:29:00", 300, False),
    ("09:57:00", 179, False),
    (None, 420, False),
    ("08:36:00", None, False),
    ("not-a-time", 420, False),
])
def test_only_a_plausible_main_sleep_triggers_the_morning(wake, minutes, expected):
    assert delivery_policy.wake_time_is_plausible(DAY, wake, minutes) is expected


def shanghai(hour: int, minute: int) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize(("wake", "minutes", "usual", "data_until", "checked", "expected"), [
    # 2026-10-18: read at 08:35, two minutes of watch data after the 08:27 wake.
    ("08:27:00", 364, 439.5, (8, 29), (8, 35), False),
    # Still the same an hour later, but 83% of the usual night and no watch
    # data showing the user awake: they slept on until 09:35.
    ("08:27:00", 364, 439.5, (8, 29), (9, 27), False),
    ("09:35:00", 429, 439.5, (9, 39), (10, 35), True),
    ("09:35:00", 429, 439.5, (9, 39), (10, 34), False),
    # 2026-10-17: the phone uploaded during a brief awakening; the night went on.
    ("07:04:00", 275, 439.5, (7, 8), (8, 4), False),
    # A short night also needs half an hour of watch data after the wake.
    ("07:04:00", 275, 439.5, (7, 34), (8, 4), True),
    ("07:04:00", 275, 439.5, (7, 34), (8, 3), False),
    ("07:04:00", 275, 439.5, (7, 33), (9, 0), False),
    ("08:36:00", 396, 440.0, (8, 40), (9, 36), True),
    ("08:36:00", 395, 440.0, (8, 40), (9, 36), False),
    ("08:36:00", 395, 440.0, (9, 6), (9, 36), True),
    # Without five usual nights only watch data after the wake counts.
    ("08:36:00", 455, None, (8, 40), (9, 36), False),
    ("08:36:00", 455, None, (9, 6), (9, 36), True),
    ("08:36:00", 455, None, None, (9, 36), False),
    # Without a read of the sleep nothing has been confirmed.
    ("08:36:00", 455, 440.0, (9, 40), None, False),
    # The plausibility floors still apply first.
    ("04:29:00", 400, 400.0, (6, 0), (6, 0), False),
    ("09:57:00", 179, 200.0, (11, 0), (11, 0), False),
    (None, 420, 420.0, (9, 0), (9, 0), False),
])
def test_the_morning_starts_an_hour_after_the_main_sleep_ended(
    wake, minutes, usual, data_until, checked, expected,
):
    assert delivery_policy.morning_sleep_has_ended(
        DAY, wake, minutes, usual_minutes=usual,
        data_until=shanghai(*data_until) if data_until else None,
        checked_at=shanghai(*checked) if checked else None,
        timezone="Asia/Shanghai",
    ) is expected


def test_a_plausible_wake_settles_an_hour_later():
    assert delivery_policy.morning_wake_settles_at(
        DAY, "08:27:00", 364, timezone="Asia/Shanghai",
    ) == shanghai(9, 27)
    assert delivery_policy.morning_wake_settles_at(DAY, "02:59:00", 200, timezone="Asia/Shanghai") is None
    assert delivery_policy.morning_wake_settles_at(DAY, None, 420, timezone="Asia/Shanghai") is None


@pytest.mark.parametrize(("zone", "deadline", "local_deadline"), [
    ("America/New_York", "2026-03-08T05:30:00Z", "00:30"),
    # Havana springs forward at midnight: still 90 real minutes.
    ("America/Havana", "2026-03-08T05:30:00Z", "01:30"),
])
def test_evening_deadline_is_ninety_real_minutes(zone, deadline, local_deadline):
    window = delivery_policy.scheduled_report_window(date(2026, 3, 7), "evening", zone)
    end = datetime.fromisoformat(window["deadline_at"].replace("Z", "+00:00"))
    assert window["deadline_at"] == deadline
    assert end.astimezone(ZoneInfo(zone)).strftime("%H:%M") == local_deadline
    assert (end - datetime.fromisoformat(window["scheduled_for"].replace("Z", "+00:00"))).total_seconds() == 5400


def test_prior_workouts_are_not_same_day_facts():
    payload = profile()
    payload["features"] = {
        "sleep": {"status": "INSUFFICIENT_DATA"},
        "training": {
            "recent_workouts": [{"date": "2026-10-08", "duration_minutes": 45}],
            "status": "AVAILABLE",
        },
    }
    assert delivery_policy.has_same_day_facts(payload) is False
    assert prepare(payload, period="morning", now=datetime(2026, 10, 9, 10, tzinfo=UTC))["status"] == "deferred"


@pytest.mark.parametrize("steps", [0, 123])
def test_observed_zero_and_current_values_are_same_day_facts(steps):
    payload = profile()
    payload["features"] = {
        "activity": {"status": "AVAILABLE", "steps": {"value": steps}},
    }
    assert delivery_policy.has_same_day_facts(payload) is True


def test_empty_available_activity_is_not_a_fact():
    payload = profile()
    payload["features"] = {
        "sleep": {"status": "INSUFFICIENT_DATA"},
        "activity": {"status": "AVAILABLE", "steps": None},
    }
    assert delivery_policy.has_same_day_facts(payload) is False


def test_explicit_plan_expiry_cannot_extend_scheduled_deadline():
    result = prepare(profile(), now=datetime(2026, 10, 10, 0, 30, tzinfo=UTC),
                     plan_expires_at=datetime(2100, 1, 1, tzinfo=UTC))
    assert result["status"] == "deferred"
    assert result["reason"] == "stale_plan_expired"


def test_deduplication_gate_still_wins_after_deadline():
    result = prepare(profile(), now=datetime(2026, 10, 12, 12, tzinfo=UTC), already_sent=True)
    assert result == {"status": "already_sent", "date": DAY.isoformat(), "period": "evening"}
