from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import AnalysisJob, DeliveryAlertEvent, WorkerHeartbeat
from vitalis.config import settings
from vitalis.intelligence.contracts import SubjectiveFeedbackInput
from vitalis.intelligence.open_health.readiness import compute_composite_readiness


def test_alert_check_is_idempotent_and_does_not_touch_the_heartbeat(monkeypatch):
    from vitalis.scheduler.alerts import check_delivery_health

    now = datetime(2026, 10, 13, 15, tzinfo=timezone.utc)
    user_id = "internal-alert-scan-owner"
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)
        row = db.get(WorkerHeartbeat, "primary")
        if row is None:
            db.add(WorkerHeartbeat(name="primary", last_seen_at=now.replace(tzinfo=None) - timedelta(hours=1)))
        else:
            row.last_seen_at = now.replace(tzinfo=None) - timedelta(hours=1)
    monkeypatch.setattr(settings, "push_user", user_id)
    monkeypatch.setattr(settings, "pushplus_token", "offline-token")
    monkeypatch.setattr(settings, "timezone", "Asia/Shanghai")
    assert check_delivery_health(now=now)["new_alerts"] >= 2
    assert check_delivery_health(now=now + timedelta(minutes=1))["new_alerts"] == 0
    with session_scope() as db:
        rows = list(db.scalars(select(DeliveryAlertEvent).where(DeliveryAlertEvent.user_id == user_id)))
        assert {row.reason for row in rows} == {"delivery_target_missed", "worker_heartbeat_stale"}
        assert db.get(WorkerHeartbeat, "primary").last_seen_at == now.replace(tzinfo=None) - timedelta(hours=1)


def test_feedback_tags_are_scoped_and_reject_unknown_body_parts():
    item = SubjectiveFeedbackInput(feeling_score=4, behavior_tags=["travel"], soreness_parts=["calves"])
    assert item.feeling_score == 4
    for changes in ({"feeling_score": True}, {"soreness_parts": ["x" * 10000]}, {"soreness_parts": ["shoulders", "shoulders"]}, {"behavior_tags": ["travel", "travel"]}):
        with pytest.raises(ValidationError):
            SubjectiveFeedbackInput(**changes)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, -1, 101, "80"])
def test_composite_rejects_invalid_component_instead_of_fabricating_score(value):
    with pytest.raises(ValueError):
        compute_composite_readiness({"hrv": value}, target_date=date(2026, 10, 13))


def test_composite_weight_redistribution_keeps_missing_components_explicit():
    output = compute_composite_readiness({"sleep": 60, "rhr": 80}, target_date=date(2026, 10, 13))
    assert output.shadow_only is True
    assert output.payload.composite_score == 69
    assert sum(output.payload.applied_weights.values()) == pytest.approx(1)
    assert output.payload.missing_components == ["hrv", "load"]


def test_composite_single_hrv_deviation_does_not_promote_warning():
    output = compute_composite_readiness(
        {"hrv": 30}, target_date=date(2026, 10, 13), hrv_value=30,
        hrv_normal_range=(40, 70), hrv_out_of_range_streak=1,
    )
    assert output.shadow_only is True
    assert output.payload.state != "suppressed"


def test_body_warning_does_not_treat_good_directions_or_partial_data_as_illness():
    from vitalis.intelligence.events import detect_body_signal_warning

    common = {"status": "AVAILABLE", "percent": 30, "direction": "above"}
    assert detect_body_signal_warning("synthetic", date(2026, 10, 13), {
        "hrv_rmssd": common,
        "resting_hr": {**common, "direction": "below", "percent": -30},
    }) is None
    assert detect_body_signal_warning("synthetic", date(2026, 10, 13), {
        "respiratory_rate": {**common, "status": "PARTIAL"},
        "resting_hr": common,
    }) is None


def test_analysis_budget_counts_memory_added_by_the_run():
    from vitalis.application.intelligence_service import analysis_cost

    assert analysis_cost(8.0, 230.0, 110.0)["budget_exceeded"] is None
    assert analysis_cost(8.0, 300.0, 110.0)["budget_exceeded"] == "memory>150MB"
    assert analysis_cost(31.0, None)["budget_exceeded"] == "time>30s"
