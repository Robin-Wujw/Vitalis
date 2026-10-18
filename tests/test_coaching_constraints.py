"""Coaching plans respect their actual focus, time budget, and public report scope."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

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
from vitalis.intelligence.report_presentation import daily_coaching_sections
from vitalis.intelligence.strength import normalize_exercise

DAY = date(2026, 10, 7)


def _strength_session(focus, name, days_ago):
    exercise = normalize_exercise(
        "constraint-user", focus, 1,
        StrengthExerciseInput(exercise_name=name, sets=3, repetitions=8), focus,
    )
    return StrengthSessionAnalysis(
        workout_id=focus, date=DAY - timedelta(days=days_ago), duration_minutes=40,
        focus=focus, focus_label=focus, confidence=ConfidenceBand.HIGH,
        confidence_label="较高", explicit_exercises=[exercise],
        muscle_groups=exercise.muscle_groups,
    )


def _rotating_training():
    return TrainingFeatures(
        status=Availability.AVAILABLE,
        strength=StrengthAnalysis(
            status=Availability.AVAILABLE, status_label="可用", sessions_7d=2,
            sessions_28d=6, explicit_session_coverage=1,
            next_focus="PUSH", next_focus_label="推类",
            recent_sessions=[
                _strength_session("PUSH", "卧推", 1),
                _strength_session("LEGS", "深蹲", 4),
            ],
            muscle_recovery=[
                MuscleRecoveryStatus(
                    muscle_group="chest", muscle_group_label="胸部",
                    days_since_last_trained=1,
                ),
                MuscleRecoveryStatus(
                    muscle_group="quadriceps", muscle_group_label="股四头肌",
                    days_since_last_trained=4,
                ),
            ],
        ),
    )


@pytest.mark.parametrize("primary_type", ["STRENGTH", "RUNNING"])
def test_relationship_uses_actual_leg_focus_after_muscle_rotation(primary_type):
    engine = DecisionEngine()
    training = _rotating_training()
    balance = SimpleNamespace(
        running_due=True, strength_due=True,
        running_completed_7d=0, running_target_7d=3,
        strength_completed_7d=0, strength_target_7d=3,
        running_completed_28d=0, running_target_28d=12,
        strength_completed_28d=0, strength_target_28d=12,
    )

    primary, optional, relationship = engine._sessions(
        DAY, primary_type, "moderate", training,
        TrainingPreferences(user_id="constraint-user"), balance,
        engine._conflicts(DAY, training),
    )

    strength = primary if primary.session_type == "STRENGTH" else optional
    assert strength.code == "strength_legs"
    assert relationship == "ALTERNATIVE"


def test_twenty_minute_run_steps_fit_the_complete_session_duration():
    session = DecisionEngine()._running(
        DAY, TrainingPreferences(user_id="constraint-user", max_session_minutes=20),
        TrainingFeatures(status=Availability.AVAILABLE), "PRIMARY", False, "low",
    )

    step_bounds = tuple(
        sum(step.duration_minutes[bound] for step in session.steps if step.duration_minutes)
        for bound in (0, 1)
    )
    assert step_bounds == session.total_duration_minutes
    assert step_bounds[1] <= 20


def test_twenty_minute_strength_plan_reduces_work_and_leaves_time_beyond_rest():
    engine = DecisionEngine()
    training = TrainingFeatures(status=Availability.AVAILABLE)
    session = engine._strength(
        TrainingPreferences(user_id="constraint-user", max_session_minutes=20),
        training, "PRIMARY", "moderate",
    )
    unrestricted = engine._strength(
        TrainingPreferences(user_id="constraint-user", max_session_minutes=60),
        training, "PRIMARY", "moderate",
    )
    working_sets = sum(step.sets or 0 for step in session.steps)
    unrestricted_sets = sum(step.sets or 0 for step in unrestricted.steps)
    warmup_minutes = sum(step.duration_minutes[0] for step in session.steps if step.duration_minutes)
    rest_minutes = sum(
        max((step.sets or 1) - 1, 0) * step.rest_seconds[0] / 60
        for step in session.steps if step.rest_seconds
    )

    assert 0 < working_sets < unrestricted_sets
    assert warmup_minutes + rest_minutes < session.total_duration_minutes[1] <= 20


def test_strength_work_fits_declared_budget_when_more_than_an_hour_is_available():
    training = _rotating_training()
    exercise = training.strength.recent_sessions[1].explicit_exercises[0]
    exercise.sets, exercise.rest_seconds = 20, 150
    training.strength.next_focus = "LEGS"
    session = DecisionEngine()._strength(
        TrainingPreferences(user_id="constraint-user", max_session_minutes=180),
        training, "PRIMARY", "moderate",
    )
    seconds = sum((step.duration_minutes or (0, 0))[1] * 60 for step in session.steps)
    seconds += sum(
        (step.sets or 0) * 45 + max((step.sets or 1) - 1, 0) * (step.rest_seconds or (0, 0))[1]
        for step in session.steps
    )
    assert seconds <= session.total_duration_minutes[1] * 60


def test_long_run_does_not_double_the_established_twenty_minute_duration():
    runs = [RunningSessionAnalysis(
        workout_id=f"easy-{index}", date=DAY - timedelta(days=4 + index * 2),
        classification="EASY_RUN", classification_label="轻松跑",
        duration_minutes=20, confidence=ConfidenceBand.HIGH, confidence_label="较高",
    ) for index in range(4)]
    training = TrainingFeatures(
        status=Availability.AVAILABLE,
        running=RunningAnalysis(
            status=Availability.AVAILABLE, status_label="可用", lactate_threshold_bpm=170,
            sessions_7d=2, sessions_28d=4, duration_minutes_7d=40,
            duration_minutes_28d=80, recent_sessions=runs,
        ),
    )

    session = DecisionEngine()._running(
        DAY, TrainingPreferences(
            user_id="constraint-user", running_experience="INTERMEDIATE",
            pain_or_injury_status="NONE",
        ), training, "PRIMARY", False, "moderate",
    )

    assert session.code == "long_easy_run"
    assert session.total_duration_minutes[1] <= 26


@pytest.mark.parametrize("reason", [
    "新的一周从近 4 周周均负荷 200 起步，目标 210。",
    "新的一周从近 28 天周均负荷 200 起步，目标 210。",
])
def test_monday_coaching_does_not_repeat_internal_load_reference_windows(reason):
    payload = {
        "date": "2026-10-12", "decision": {"action": "TRAIN_NORMAL", "action_plan": {
            "primary_session": {
                "session_type": "RUNNING", "focus": "轻松跑", "steps": [],
                "personalization_reasons": [reason],
            },
        }},
    }

    sections = daily_coaching_sections(payload)

    assert sections
    public_text = " ".join(str(section) for section in sections)
    assert "近 4 周" not in public_text
    assert "近 28 天" not in public_text
