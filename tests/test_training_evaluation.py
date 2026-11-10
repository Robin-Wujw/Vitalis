"""Saved exercise coaching uses session evidence and preserves recovery limits."""
from datetime import date

import pytest

from vitalis.intelligence.contracts import RunningSessionAnalysis, StrengthSessionAnalysis
from vitalis.intelligence.training_evaluation import review_running, review_strength


def _run(**updates):
    return RunningSessionAnalysis(
        workout_id="synthetic-run", date=date(2026, 10, 7),
        classification="UNCLASSIFIED", classification_label="未分类",
        confidence="LOW", confidence_label="低", duration_minutes=32, **updates,
    )


def _strength(**updates):
    return StrengthSessionAnalysis(
        workout_id="synthetic-strength", date=date(2026, 10, 7), duration_minutes=20,
        focus="UNKNOWN", focus_label="未知", confidence="LOW", confidence_label="低", **updates,
    )


def test_faster_run_with_higher_hr_gets_actionable_advice_without_empty_commentary():
    session = _run(comparable_baseline={
        "sample_count": 3, "workout_sources": ["mock"] * 3, "workout_ids": ["a", "b", "c"],
        "median_pace_seconds_per_km": 360, "pace_difference_percent": -10,
        "median_heart_rate_bpm": 145, "heart_rate_difference_bpm": 12,
    })
    review = review_running(session)
    assert any("交谈" in item and "若" in item for item in review.suggestions)
    assert not any("效率提升" in item for item in review.suggestions)
    assert review.total_score is None


def test_unknown_hr_does_not_invent_efficiency_or_hr_target():
    review = review_running(_run())
    assert any("交谈" in item for item in review.suggestions)
    assert not any("bpm" in item or "最大心率" in item for item in review.suggestions)


def test_strength_summary_only_still_gets_conditional_practical_coaching():
    review = review_strength(_strength(vendor_reported_sets=2))
    text = " ".join(review.suggestions)
    assert "力竭" in text and "动作" in text and "恢复" in text
    assert "逐步" in text
    assert review.total_score is None


@pytest.mark.parametrize("evidence", [{"muscle_soreness": 4}, {"session_rpe": 9}])
def test_strength_restrictions_take_priority_over_progression(evidence):
    review = review_strength(_strength(**evidence))
    assert len(review.suggestions) == 1
    assert "增加次数或重量" not in review.suggestions[0]
