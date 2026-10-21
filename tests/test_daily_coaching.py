"""Coaching stays grounded in saved plans and observed muscle recovery."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from tests.test_report_content import synthetic_daily_fixture
from vitalis.intelligence.contracts import (
    Availability,
    ConfidenceBand,
    MuscleRecoveryStatus,
    RunningAnalysis,
    RunningSessionAnalysis,
    StrengthAnalysis,
    StrengthExerciseInput,
    StrengthSessionAnalysis,
    TrainingFeatures,
    TrainingPreferences,
)
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.report_presentation import (
    period_presentation,
)
from vitalis.intelligence.report_rendering import render_report, visible_text
from vitalis.intelligence.strength import normalize_exercise


def _session(focus, name, muscles):
    exercise = normalize_exercise(
        "u", focus, 1, StrengthExerciseInput(exercise_name=name, sets=3, repetitions=8), focus,
    )
    return StrengthSessionAnalysis(
        workout_id=focus, date=date(2026, 10, 7), duration_minutes=45,
        focus=focus, focus_label=focus, confidence=ConfidenceBand.HIGH,
        confidence_label="较高", explicit_exercises=[exercise], muscle_groups=muscles,
    )


def _training(recovery, sessions):
    return SimpleNamespace(strength=StrengthAnalysis(
        status=Availability.AVAILABLE, status_label="可用", sessions_7d=2,
        sessions_28d=6, explicit_session_coverage=1, next_focus="PUSH",
        next_focus_label="推类", recent_sessions=sessions, muscle_recovery=recovery,
    ))


@pytest.mark.parametrize("days,soreness", [(1, None), (4, 4)])
def test_muscle_rotation_skips_recent_or_sore_muscles(days, soreness):
    training = _training([MuscleRecoveryStatus(
        muscle_group="chest", muscle_group_label="胸部",
        days_since_last_trained=days, latest_soreness=soreness,
    )], [_session("PUSH", "卧推", ["chest", "shoulders", "triceps"]),
        _session("PULL", "划船", ["back", "biceps"])])
    plan = DecisionEngine()._strength(TrainingPreferences(user_id="u"), training, "PRIMARY", "moderate")
    assert plan.code == "strength_pull"
    assert "卧推" not in [step.name for step in plan.steps]
    assert any("胸部" in reason and "调整" in reason for reason in plan.personalization_reasons)


def test_no_eligible_recorded_muscles_uses_recovery():
    training = _training([MuscleRecoveryStatus(
        muscle_group="chest", muscle_group_label="胸部", days_since_last_trained=1,
    )], [_session("PUSH", "卧推", ["chest"])])
    plan = DecisionEngine()._strength(TrainingPreferences(user_id="u"), training, "PRIMARY", "moderate")
    assert plan.session_type == "RECOVERY"


def _coaching_payload():
    payload = synthetic_daily_fixture()
    primary = payload["decision"]["action_plan"]["primary_session"]
    primary.update({
        "session_type": "STRENGTH", "session_type_label": "力量训练",
        "title": "拉类力量训练", "focus": "背部和肱二头肌", "code": "strength_pull",
        "steps": [
            {"order": 1, "name": "动态热身", "duration_minutes": [6, 8]},
            {"order": 2, "name": "划船", "sets": 3, "repetitions": "8–12 次",
             "rest_seconds": [90, 120], "intensity": "每组保留 2 次余力"},
        ],
        "personalization_reasons": ["按已识别的训练轮换，下一项是拉类。"],
        "progression": ["所有工作组达到次数上限且保留余力时，下次再加重。"],
    })
    return payload


@pytest.mark.parametrize("template", ["markdown", "html"])
def test_morning_delivers_saved_muscle_focus_dose_and_progression(template):
    payload = _coaching_payload()
    report = MorningBriefingEngine().build_payload(payload)
    content = visible_text(render_report(report, template).content, template)
    for text in ("背部和肱二头肌", "划船", "3 组", "8–12 次", "90–120 秒", "保留 2 次余力", "下次再加重"):
        assert text in content
    assert "至少间隔 6 小时" in content


def test_running_steps_and_talk_test_reach_morning():
    payload = synthetic_daily_fixture()
    primary = payload["decision"]["action_plan"]["primary_session"]
    primary["steps"].append({"order": 2, "name": "轻松跑主体", "duration_minutes": [20, 25],
                             "intensity": "能连续说完整句子"})
    report = MorningBriefingEngine().build_payload(payload)
    text = visible_text(render_report(report, "markdown").content, "markdown")
    assert "轻松跑主体" in text
    assert "20–25 分钟" in text
    assert "能连续说完整句子" in text


@pytest.mark.parametrize("metadata", [{"facts_only": True}, {"retrospective": True}])
def test_facts_only_and_retrospective_mornings_do_not_leak_coaching(metadata):
    payload = _coaching_payload()
    report = MorningBriefingEngine().build_payload(payload, metadata)
    assert not report["suggestions"]


def test_period_coaching_shows_saved_action_without_dumping_audit_reasons():
    payload = {"actions": {"recommendations": [
        {"code": "SLEEP", "action": "先保证睡眠"},
        {"code": "LOAD", "action": "控制加量"},
        {"code": "WEEKLY_COACH_STRENGTH", "title": "力量分化", "action": "上肢／下肢交替",
         "reasons": ["已识别胸部、背部各 1 天。"]},
        {"code": "WEEKLY_COACH_RUNNING", "title": "跑步结构", "action": "以轻松跑为主"},
    ]}}
    text = "\n".join(period_presentation(payload, "weekly")["suggestions"])
    assert "先保证睡眠" in text and "上肢／下肢交替" in text and "以轻松跑为主" in text
    assert "已识别胸部、背部各 1 天" not in text


@pytest.mark.parametrize("experience", [None, "BEGINNER"])
def test_unknown_or_beginner_experience_does_not_get_quality_work(experience):
    day = date(2026, 10, 7)
    runs = [RunningSessionAnalysis(
        workout_id=f"run-{index}", date=day - timedelta(days=4 + index * 2),
        classification="EASY_RUN", classification_label="轻松跑", duration_minutes=30,
        confidence=ConfidenceBand.HIGH, confidence_label="较高",
    ) for index in range(4)]
    training = TrainingFeatures(status=Availability.AVAILABLE, running=RunningAnalysis(
        status=Availability.AVAILABLE, status_label="可用", zone_method="lactate_threshold",
        lactate_threshold_bpm=170, sessions_7d=2, sessions_28d=4,
        duration_minutes_7d=60, duration_minutes_28d=120, recent_sessions=runs,
    ))
    session = DecisionEngine()._running(day, TrainingPreferences(
        user_id="u", running_experience=experience, pain_or_injury_status="NONE",
    ), training, "PRIMARY", True, "high")
    assert session.code == "easy_run"


def test_evening_muscle_recap_uses_known_muscles_without_weight_inference():
    from vitalis.intelligence.evening_briefing import EveningBriefingEngine
    payload = synthetic_daily_fixture()
    strength = payload["features"]["training"]["strength"]
    strength.update({"detected_split": "PUSH_PULL_LEGS", "detected_split_label": "推、拉、腿三分化",
                     "next_focus": "PULL", "next_focus_label": "拉类",
                     "next_focus_reasons": ["下次候选：拉类；根据已记录肌群训练顺序。"]})
    strength["recent_sessions"] = [{
        "date": payload["date"], "focus_label": "推类", "muscle_group_labels": ["胸部", "肩部", "肱三头肌"],
        "explicit_exercises": [{"exercise_name": "卧推", "muscle_groups": ["chest", "shoulders", "triceps"]}],
    }]
    report = EveningBriefingEngine().build_payload(payload)
    text = visible_text(render_report(report, "html").content, "html")
    assert "肌群分化回顾" in text and "胸部、肩部、肱三头肌" in text
    assert "推、拉、腿三分化" in text and "拉类" in text
