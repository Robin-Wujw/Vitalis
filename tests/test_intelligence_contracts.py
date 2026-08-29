from datetime import date

import pytest
from pydantic import ValidationError

from vitalis.intelligence.contracts import (
    Availability,
    BaselineStats,
    ConfidenceBand,
    DataQuality,
    DecisionAction,
    DeviceValidity,
    QualityStatus,
    ActionPlan,
    ConcurrentWeeklyBalance,
    TrainingDecision,
)


def test_baseline_contract_requires_explicit_availability():
    baseline = BaselineStats(
        status=Availability.INSUFFICIENT_DATA,
        metric="hrv_rmssd",
        source="zepp",
        source_scope="device",
        device_id="helio",
        unit="ms",
        window_days=28,
        transform="natural_log",
        sample_count=8,
        distinct_days=8,
        minimum_days=14,
        coverage_ratio=8 / 28,
    )
    assert baseline.median is None
    assert baseline.status == Availability.INSUFFICIENT_DATA


def test_quality_contract_separates_device_validity_from_data_quality():
    quality = DataQuality(
        status=QualityStatus.PARTIAL,
        required_signals=["sleep", "hrv"],
        missing_required_signals=["hrv"],
        device_validity=[DeviceValidity(device_id="helio")],
    )
    payload = quality.model_dump(mode="json")
    assert payload["device_validity"][0]["status"] == "UNKNOWN"
    assert "measurement_quality" not in payload


def test_decision_contract_has_no_implicit_fallback_score():
    decision = TrainingDecision(
        recommendation_id="contract-recommendation",
        action=DecisionAction.INSUFFICIENT_DATA,
        confidence=ConfidenceBand.NONE,
        limitations=["missing recovery signals"],
        action_plan=ActionPlan(
            valid_for_date=date(2026, 8, 29),
            expires_at="2026-08-30T00:00:00+08:00",
            safety_status="UNKNOWN",
            safety_status_label="伤病与疼痛状态未确认",
            weekly_balance=ConcurrentWeeklyBalance(
                running_completed_7d=0,
                running_target_7d=3,
                running_completed_28d=0,
                running_target_28d=12,
                strength_completed_7d=0,
                strength_target_7d=3,
                strength_completed_28d=0,
                strength_target_28d=12,
                running_due=True,
                strength_due=True,
            ),
            session_relationship_label="没有附加训练",
        ),
    )
    assert "score" not in decision.model_dump()
    with pytest.raises(ValidationError):
        TrainingDecision(
            recommendation_id="contract-recommendation",
            action="MAYBE",
            confidence=ConfidenceBand.LOW,
            action_plan=decision.action_plan,
        )
