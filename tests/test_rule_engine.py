"""Rule Engine 确定性规则测试。"""

from datetime import date

import pytest

from vitalis.analysis import RuleEngine
from vitalis.models import (
    ActivityRecord,
    DailyHealth,
    RecoveryLevel,
    SleepRecord,
    StressLevel,
    TrainingReadiness,
    TrainingRecord,
)


def make_daily(*, sleep_min=None, sleep_score=None, deep=None, load=0, hrv_trend=None) -> DailyHealth:
    sleep = (
        SleepRecord(
            user_id="u1", date=date(2026, 8, 25),
            sleep_duration=sleep_min, deep_sleep=deep or 0,
            rem_sleep=0, light_sleep=0, awake=0, sleep_score=sleep_score,
        )
        if sleep_min is not None
        else None
    )
    training = TrainingRecord(user_id="u1", date=date(2026, 8, 25),
                              total_load=load, workout_count=1 if load else 0)
    return DailyHealth(user_id="u1", date=date(2026, 8, 25),
                       sleep=sleep, training=training, hrv_trend_pct=hrv_trend)


def test_insufficient_sleep_lowers_recovery():
    decision = RuleEngine().evaluate(make_daily(sleep_min=300, load=0, hrv_trend=0))
    assert decision.overall_score < 60
    assert decision.recovery_level in (RecoveryLevel.LOW, RecoveryLevel.OVERREACH)
    assert any("睡眠不足" in item for item in decision.items)


def test_good_sleep_ready():
    decision = RuleEngine().evaluate(make_daily(sleep_min=500, sleep_score=90, load=0, hrv_trend=5))
    assert decision.overall_score >= 80
    assert decision.recovery_level == RecoveryLevel.READY
    assert decision.training_readiness == TrainingReadiness.FULL


def test_high_load_poor_sleep_high_stress():
    decision = RuleEngine().evaluate(make_daily(sleep_min=310, load=90, hrv_trend=-10))
    assert decision.stress_level == StressLevel.HIGH
    assert decision.training_readiness == TrainingReadiness.NOT_READY


def test_hrv_negative_trend_penalizes():
    good = RuleEngine().evaluate(make_daily(sleep_min=500, load=30, hrv_trend=2))
    bad = RuleEngine().evaluate(make_daily(sleep_min=500, load=30, hrv_trend=-8))
    assert good.overall_score > bad.overall_score
    assert any("HRV" in item for item in bad.items)


def test_no_data_lowest_score():
    decision = RuleEngine().evaluate(DailyHealth(user_id="u1", date=date(2026, 8, 25)))
    assert 0 <= decision.overall_score <= 100
    assert decision.recovery_level in (RecoveryLevel.LOW, RecoveryLevel.OVERREACH)


def test_thresholds_configurable():
    engine = RuleEngine()
    engine.sleep_thresholds["low"] = 540  # 从严：9h 以下都算不足
    decision = engine.evaluate(make_daily(sleep_min=500))
    assert any("睡眠不足" in item for item in decision.items)

    # 确定性：同输入两次结果一致
    engine2 = RuleEngine()
    d1 = engine2.evaluate(make_daily(sleep_min=400, load=40, hrv_trend=1))
    d2 = engine2.evaluate(make_daily(sleep_min=400, load=40, hrv_trend=1))
    assert d1 == d2


def test_activity_not_required():
    decision = RuleEngine().evaluate(make_daily(sleep_min=490, load=20))
    assert decision.overall_score > 60