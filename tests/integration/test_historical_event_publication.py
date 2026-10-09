"""Recomputing an older report cannot move the live event aggregate backwards."""

from datetime import date, timedelta

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.bootstrap import get_intelligence_command
from vitalis.intelligence.contracts import ConfidenceBand, EventSeverity, HealthEvent


def test_historical_analysis_preserves_newer_live_event_state():
    user_id = "historical-event-publish"
    current_day = date(2026, 9, 20)
    command = get_intelligence_command()
    command.analyze(user_id, current_day)
    event = HealthEvent(
        id="synthetic-current-event", type="SLEEP_DEFICIT", type_label="睡眠不足",
        severity=EventSeverity.MODERATE, severity_label="中等",
        metric="sleep_duration", metric_label="睡眠时长",
        start_date=current_day - timedelta(days=2), end_date=current_day,
        duration_days=3, confidence=ConfidenceBand.HIGH, confidence_label="较高",
        summary="合成事件", last_observed_date=current_day, last_evaluated_date=current_day,
    )
    with session_scope() as db:
        HealthRepository(db).save_health_event(user_id, event)
    with session_scope() as db:
        before = HealthRepository(db).health_event(user_id, event.id)
    historical = command.analyze(user_id, current_day - timedelta(days=7))
    with session_scope() as db:
        after = HealthRepository(db).health_event(user_id, event.id)
    assert after == before
    assert historical.daily.date == current_day - timedelta(days=7)
    assert all(item.id != event.id for item in historical.daily.events)
