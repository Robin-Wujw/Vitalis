from datetime import date

import pytest

from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.intelligence.analyzers import TrainingAnalyzer
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.period_activity import period_training_details
from vitalis.intelligence.profile import RawDailyProfile


@pytest.mark.parametrize("groups", [12, "12", 12.0])
def test_strength_history_preserves_explicit_vendor_group_count(groups):
    workout = ZeppParser().parse_sport_history({"items": [{
        "type": 52, "trackid": "synthetic-strength", "total_group": groups,
        "work_value": 987.6, "calorie": "240",
    }]})[0]
    assert workout.vendor_reported_sets == 12
    assert "vendor_reported_sets" in workout.observed_fields
    assert workout.calories == 240
    assert not hasattr(workout, "weight_kg")


@pytest.mark.parametrize("groups", [None, 0, -1, True, 3.5, "unknown"])
def test_unavailable_or_invalid_group_counts_remain_missing(groups):
    workout = ZeppParser().parse_sport_history({"items": [{"type": 52, "total_group": groups}]})[0]
    assert workout.vendor_reported_sets is None
    assert "vendor_reported_sets" not in workout.observed_fields


def test_non_strength_records_do_not_acquire_strength_group_counts():
    workout = ZeppParser().parse_sport_history({"items": [{"type": 1, "total_group": 12}]})[0]
    assert workout.vendor_reported_sets is None


def test_vendor_count_is_not_explicit_exercise_detail_and_supersedes_estimated_bouts():
    day = date(2026, 8, 28)
    raw = RawDailyProfile(user_id="synthetic-vendor-groups", day=day)
    raw.workouts = [{
        "workout_id": "summary-only", "source": "zepp", "local_day": day,
        "detail_available": True, "detail": {"strength_sets": []}, "samples": [],
        "data": {"type": "strength", "training_family": "strength", "duration": 45,
                 "vendor_reported_sets": 12, "sport_mode_label": "力量训练"},
    }]
    training = TrainingAnalyzer().analyze(raw, {})
    session = training.strength.recent_sessions[0]
    assert session.vendor_reported_sets == 12
    assert session.total_sets is None
    assert session.explicit_exercises == []
    session.estimated_work_bouts = 21
    payload = {"date": day.isoformat(), "features": {"training": training.model_dump(mode="json")}}
    report = EveningBriefingEngine().build(payload)
    text = "\n".join(value for section in report.sections for value in section.facts)
    assert "设备记录 12 组" in text
    assert "21 个工作段" not in text
    assert "逐组动作" in text and "尚未取得" in text
    period = period_training_details(raw, day, day)
    assert period["vendor_reported_sets"] == 12
    assert period["vendor_sets_sessions"] == 1
    assert period["strength_sets"] is None
