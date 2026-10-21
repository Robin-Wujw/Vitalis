"""Explain strength source limitations without calling synced names a failure."""

import re
from datetime import UTC, date, datetime, timedelta

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    EventSeverity,
    HealthEvent,
    TrainingPreferences,
)
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.report_presentation import period_presentation
from vitalis.intelligence.weekly import WeeklyProfileEngine

END = date(2026, 10, 18)


def _raw_week(*, reference_names: bool):
    raw = RawDailyProfile(
        user_id="synthetic-readable-coaching", day=END,
        as_of=datetime(2026, 10, 19, tzinfo=UTC),
        training_history_coverage={
            "verified_days": [(END - timedelta(days=n)).isoformat() for n in range(7)],
        },
    )
    raw.sleep_by_day = {
        END - timedelta(days=n): {"sleep_duration": 450} for n in range(7)
    }
    raw.workouts = [{
        "workout_id": f"synthetic-strength-{offset}", "source": "zepp",
        "local_day": END - timedelta(days=offset),
        "confirmed_exercises": [], "detail_available": reference_names,
        "data": {"type": "strength", "training_family": "strength", "duration": 40},
        "detail": {"strength_sets": [{
            "source": "lap_62", "order": 1, "vendor_exercise_code": 64,
            "exercise_name": "引体向上", "exercise_id": None,
            "repetitions": 8, "limitations": ["exercise_name_reference_mapping"],
        }]} if reference_names else None,
    } for offset in (1, 3, 5)]
    return raw


def _week(*, reference_names: bool):
    raw = _raw_week(reference_names=reference_names)
    profile = WeeklyProfileEngine().build(
        "synthetic-readable-week", raw, [], [], period_mode="rolling",
    )
    recommendation = next(
        item for item in profile.actions.recommendations
        if item.code == "WEEKLY_COACH_STRENGTH"
    )
    return recommendation, " ".join([recommendation.action, *recommendation.reasons])


def _assert_no_unsupported_split(recommendation):
    for sequence in ("全身 A/B", "上肢→", "下肢→", "推→拉→腿", "胸→背→腿"):
        assert sequence not in recommendation.action


def test_mapped_watch_names_are_explained_as_reference_without_zero_coverage_jargon():
    recommendation, text = _week(reference_names=True)

    assert "根据 Zepp 动作目录" in text
    assert "主要练到" in text
    assert "厂商部位参考" not in text
    assert "直接 3 组" not in text
    assert "不补课或自动加量" in text
    assert "动作可识别肌群" not in text
    assert not re.search(r"0\s*/\s*3", text)
    assert not any(claim in text for claim in ("未同步", "没同步", "没有动作明细"))
    assert "没有可用于判断部位的动作明细" not in text
    _assert_no_unsupported_split(recommendation)


def test_absent_action_details_are_distinguished_from_mapped_watch_names():
    recommendation, text = _week(reference_names=False)

    assert "没有可用于判断部位的动作明细" in text
    assert "手表记录的动作名称目前仅供参考" not in text
    assert "动作可识别肌群" not in text
    assert not re.search(r"0\s*/\s*3", text)
    _assert_no_unsupported_split(recommendation)


def test_running_coaching_does_not_remove_the_uncovered_strength_goal():
    raw = _raw_week(reference_names=False)
    raw.training_preferences = TrainingPreferences(
        user_id=raw.user_id, weekly_strength_target=3, pain_or_injury_status="NONE",
    )
    raw.workouts = [{
        "workout_id": f"synthetic-run-{offset}", "source": "zepp",
        "local_day": END - timedelta(days=offset),
        "data": {"type": "running", "training_family": "aerobic", "duration": 30},
    } for offset in (1, 4)]

    profile = WeeklyProfileEngine().build("synthetic-running-only", raw, [], [], period_mode="rolling")

    codes = {item.code for item in profile.actions.recommendations}
    assert profile.facts.training.strength_sessions == 0
    assert "WEEKLY_COACH_RUNNING" in codes
    assert "MAINTAIN_STRENGTH_RHYTHM" in codes
    assert "WEEKLY_COACH_STRENGTH" not in codes


def test_load_spike_advice_survives_a_separate_sleep_recommendation():
    raw = _raw_week(reference_names=True)
    raw.sleep_by_day = {day: {"sleep_duration": 360} for day in raw.sleep_by_day}
    event = HealthEvent(
        id="synthetic-load-spike", type="TRAINING_LOAD_SPIKE", type_label="训练负荷增加",
        severity=EventSeverity.MODERATE, severity_label="中等", start_date=END,
        end_date=END, duration_days=1, confidence=ConfidenceBand.HIGH,
        confidence_label="较高", summary="合成训练负荷增加事件",
    )

    profile = WeeklyProfileEngine().build(
        "synthetic-spike-and-sleep", raw, [], [event], period_mode="rolling",
    )
    suggestions = period_presentation(profile.model_dump(mode="json"), "weekly")["suggestions"]
    text = " ".join(suggestions)

    assert "IMPROVE_SLEEP_DURATION" in {item.code for item in profile.actions.recommendations}
    assert "训练量增加较快" in text
    assert "不加训练量" in text
    assert "记录还没核实" not in text
