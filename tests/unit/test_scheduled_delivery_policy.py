"""Pure finite report deadlines, partial fallback, and DST elapsed time."""

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from vitalis.application import delivery_policy


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
    now = now or datetime(2026, 10, 10, 0, 30, tzinfo=UTC)
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
    assert metadata["delivered_as_of"] == "2026-10-10T00:30:00Z"
    assert metadata["deadline_at"] == "2026-10-10T03:30:00Z"
    assert metadata["allowed_delay_seconds"] == 21600
    assert "decision" not in outcome["payload"]
    assert outcome["payload"]["report_context"]["delivery_metadata"] == metadata
    assert payload == original


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 10, 3, 30, tzinfo=UTC),
    datetime(2026, 10, 11, 0, 0, tzinfo=UTC),
])
def test_scheduled_reports_stop_at_finite_deadline(now):
    result = prepare(profile(), now=now)
    assert result["status"] == "deferred"
    assert result["reason"] == "stale_plan_expired"
    assert result["delivery_metadata"]["failure_code"] == "delivery_deadline_expired"
    assert result["delivery_metadata"]["deadline_at"] == "2026-10-10T03:30:00Z"


def test_scheduled_evening_cannot_send_without_any_observed_facts():
    payload = profile()
    payload["features"] = {}
    payload["report_context"]["training_history"]["status"] = "UNKNOWN"
    result = prepare(payload)
    assert result["status"] == "deferred"
    assert result["reason"] == "stored_data_incomplete"


def test_scheduled_morning_has_bounded_partial_fallback():
    payload = profile(period="morning")
    payload["report_context"]["as_of"] = "2026-10-09T10:00:00Z"
    result = prepare(payload, period="morning", now=datetime(2026, 10, 9, 10, 5, tzinfo=UTC))
    assert result["status"] == "ready"
    assert result["facts_only"] is True
    assert "sleep_complete" in result["payload"]["delivery_metadata"]["missing_signals"]
    assert "decision" not in result["payload"]
    assert prepare(payload, period="morning", now=datetime(2026, 10, 9, 12, 30, tzinfo=UTC))["reason"] == "stale_plan_expired"


@pytest.mark.parametrize(("target", "deadline"), [
    (date(2026, 3, 7), "2026-03-08T08:30:00Z"),
    (date(2026, 10, 31), "2026-11-01T07:30:00Z"),
])
def test_evening_delay_uses_six_real_hours_across_dst(target, deadline):
    window = delivery_policy.scheduled_report_window(target, "evening", "America/New_York")
    assert window["deadline_at"] == deadline
    assert (
        datetime.fromisoformat(window["deadline_at"].replace("Z", "+00:00"))
        - datetime.fromisoformat(window["scheduled_for"].replace("Z", "+00:00"))
    ).total_seconds() == 21600


def test_explicit_plan_expiry_cannot_extend_scheduled_deadline():
    result = prepare(profile(), now=datetime(2026, 10, 10, 5, tzinfo=UTC),
                     plan_expires_at=datetime(2100, 1, 1, tzinfo=UTC))
    assert result["status"] == "deferred"
    assert result["reason"] == "stale_plan_expired"


def test_deduplication_gate_still_wins_after_deadline():
    result = prepare(profile(), now=datetime(2026, 10, 12, 12, tzinfo=UTC), already_sent=True)
    assert result == {"status": "already_sent", "date": DAY.isoformat(), "period": "evening"}
