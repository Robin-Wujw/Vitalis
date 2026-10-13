"""Personal response model: gated on feedback days and on beating the naive forecast."""

from datetime import date, timedelta
from math import log

from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.response_model import DayRow, evaluate

START = date(2026, 6, 1)


def _rows(days: int, *, feedback: bool) -> list[DayRow]:
    rows = []
    hrv = log(50)
    for offset in range(days):
        load = 150.0 if offset % 3 == 0 else 0.0
        rows.append(DayRow(
            day=START + timedelta(days=offset), load=load, sleep_minutes=450.0,
            hrv=hrv, resting_hr=55.0, feeling=3.0 if feedback else None, tags=frozenset(),
        ))
        # A hard day lowers the next night's HRV; rest days recover it.
        hrv = log(42) if load else log(52)
    return rows


def test_model_stays_off_until_sixty_feedback_days():
    result = evaluate(_rows(90, feedback=False))
    assert result["enabled"] is False and result["feedback_days"] == 0
    assert all(not item["usable"] for item in result["targets"].values())


def test_model_is_used_only_where_it_beats_tomorrow_equals_today():
    result = evaluate(_rows(90, feedback=True))
    hrv = result["targets"]["hrv"]
    assert result["enabled"] is True
    assert hrv["model_mae"] < hrv["naive_mae"] and hrv["usable"] is True
    # Feeling never changes, so the naive forecast is exact and wins.
    assert result["targets"]["feeling"]["usable"] is False


def test_monthly_report_says_the_model_is_still_accumulating():
    section = MonthlyBriefingEngine()._prediction_quality({"report_context": {"prediction_quality": {
        "response_model": {"enabled": False, "feedback_days": 12, "required_feedback_days": 60},
        "tracking": {"plan_execution_rate_7d": 0.5},
    }}})
    assert "还在积累" in section["facts"][0] and "12" in section["facts"][0]
    assert any("计划执行率 50%" in fact for fact in section["facts"])
