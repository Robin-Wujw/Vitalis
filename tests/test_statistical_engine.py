"""Statistical Engine 趋势统计测试。"""

from datetime import date, timedelta

from vitalis.analysis import StatisticalEngine
from vitalis.models import ActivityRecord, DailyHealth, SleepRecord, TrainingRecord

import pytest


def _daily(day: date, sleep_min: int = 460, resting_hr: int = 60, load: int = 0) -> DailyHealth:
    return DailyHealth(
        user_id="u1",
        date=day,
        sleep=SleepRecord(user_id="u1", date=day, sleep_duration=sleep_min),
        activity=ActivityRecord(user_id="u1", date=day, resting_hr=resting_hr),
        training=TrainingRecord(user_id="u1", date=day, total_load=load, workout_count=1 if load else 0),
    )


def test_hrv_trend_uses_resting_hr():
    # 过去 7 天 resting_hr 稳定 60，今天更低(=恢复更好): hrv 趋势应为正
    base = date(2026, 8, 18)
    history = [_daily(base + timedelta(days=i), resting_hr=60) for i in range(7)]
    target = _daily(base + timedelta(days=7), resting_hr=55)
    stats = StatisticalEngine().compute(history, target)
    assert stats["hrv_trend_pct"] is not None
    assert stats["hrv_trend_pct"] > 5  # (55-60)/60 = -8.3% -> 转正


def test_hrv_trend_insufficient_samples():
    history = [_daily(date(2026, 8, 24))]  # 仅 1 天
    target = _daily(date(2026, 8, 25))
    stats = StatisticalEngine().compute(history, target)
    assert stats["hrv_trend_pct"] is None


def test_load_30d_window():
    base = date(2026, 7, 20)
    days = []
    # 30 天窗口内：负荷 10 x 20 天
    for i in range(20):
        days.append(_daily(base + timedelta(days=i), load=10))
    # 窗口外（40 天前）：负荷 100，不应计入
    days.append(_daily(base - timedelta(days=10), load=100))
    target = _daily(base + timedelta(days=25))
    stats = StatisticalEngine().compute(days, target)
    assert stats["load_30d"] <= 200  # 只统计窗口内


def test_sleep_avg_7d():
    base = date(2026, 8, 18)
    history = [_daily(base + timedelta(days=i), sleep_min=400 + i * 10) for i in range(7)]
    target = _daily(base + timedelta(days=7), sleep_min=500)
    stats = StatisticalEngine().compute(history, target)
    # 均值只算 target 之前 7 天：400..460 -> (400+410+420+430+440+450+460)/7 = 430
    assert stats["sleep_avg_7d_min"] == pytest.approx(430.0)