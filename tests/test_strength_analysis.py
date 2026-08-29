from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import (
    StrengthExerciseInput,
    StrengthWorkoutConfirmationInput,
    SubjectiveFeedback,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.service import IntelligenceAction
from vitalis.intelligence.strength import StrengthAnalyzer, normalize_exercise
from vitalis.models import Workout, WorkoutMetricSample, WorkoutType
from vitalis.storage import HealthRepository, session_scope


TARGET = date(2026, 8, 29)
START = datetime(2026, 8, 29, 8, tzinfo=timezone.utc)


def _workout(day, workout_id, exercises=None, focus=None, samples=None, detail=None):
    confirmed = []
    for order, exercise in enumerate(exercises or [], start=1):
        confirmed.append(normalize_exercise(
            "strength-user", workout_id, order, exercise, focus
        ))
    return {
        "workout_id": workout_id,
        "local_day": day,
        "confirmed_exercises": confirmed,
        "samples": samples or [],
        "detail": detail,
        "data": {
            "type": "strength",
            "training_family": "strength",
            "duration": 60,
            "heart_rate_avg": 110,
            "heart_rate_max": 150,
        },
    }


def _raw(workouts):
    raw = RawDailyProfile(user_id="strength-user", day=TARGET, workouts=workouts)
    raw.series["lactate_threshold_hr"] = [SeriesPoint(
        metric="lactate_threshold_hr",
        value=170,
        unit="bpm",
        day=TARGET,
        observed_at=TARGET,
        source="zepp",
        source_scope="daily_metric",
    )]
    return raw


def test_user_can_replace_confirmed_strength_exercises():
    user_id = "strength-confirmation-user"
    workout_id = "strength-confirmation-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id=workout_id,
            type=WorkoutType.STRENGTH,
            training_family="strength",
            duration=50,
        ))

    records = IntelligenceAction().confirm_strength_workout(
        user_id,
        workout_id,
        StrengthWorkoutConfirmationInput(
            session_focus="PUSH",
            exercises=[
                StrengthExerciseInput(
                    exercise_name="卧推", sets=4, repetitions="8", weight_kg=60
                ),
                StrengthExerciseInput(
                    exercise_name="肩推", sets=3, repetitions="10", rir=2
                ),
            ],
        ),
    )

    assert [item.movement_pattern for item in records] == [
        "horizontal_push", "vertical_push"
    ]
    assert records[0].muscle_group_labels[0] == "胸部"
    assert {item.session_focus for item in records} == {"PUSH"}
    with session_scope() as db:
        stored = HealthRepository(db).strength_exercises_for_workouts(
            user_id, [workout_id]
        )[workout_id]
    assert [item.exercise_name for item in stored] == ["卧推", "肩推"]


def test_strength_analysis_detects_push_pull_legs_and_next_focus():
    workouts = [
        _workout(
            TARGET - timedelta(days=4),
            "push",
            [StrengthExerciseInput(exercise_name="卧推", sets=4)],
            "PUSH",
        ),
        _workout(
            TARGET - timedelta(days=2),
            "pull",
            [StrengthExerciseInput(exercise_name="划船", sets=4)],
            "PULL",
        ),
        _workout(
            TARGET,
            "legs",
            [StrengthExerciseInput(exercise_name="深蹲", sets=4)],
            "LEGS",
        ),
    ]
    raw = _raw(workouts)
    raw.feedback_by_workout["legs"] = [SubjectiveFeedback(
        id="feedback",
        user_id=raw.user_id,
        date=TARGET,
        workout_id="legs",
        session_rpe=8,
        muscle_soreness=3,
    )]

    analysis = StrengthAnalyzer().analyze(raw)

    assert analysis.detected_split == "PUSH_PULL_LEGS"
    assert analysis.next_focus == "PUSH"
    assert analysis.explicit_session_coverage == 1
    assert analysis.recent_sessions[0].focus == "LEGS"
    assert analysis.recent_sessions[0].session_rpe == 8
    quadriceps = next(
        item for item in analysis.muscle_recovery
        if item.muscle_group == "quadriceps"
    )
    assert quadriceps.days_since_last_trained == 0
    assert quadriceps.latest_soreness == 3


def test_strength_analysis_estimates_bouts_without_guessing_exercises():
    samples = []
    for second in range(12 * 60):
        phase = second % 180
        heart_rate = 145 if phase < 60 else 100
        samples.append(WorkoutMetricSample(
            workout_id="unknown-strength",
            timestamp=START + timedelta(seconds=second),
            metric="heart_rate",
            value=heart_rate,
            unit="bpm",
        ))
    workout = _workout(
        TARGET,
        "unknown-strength",
        samples=samples,
        detail={
            "laps": [
                {"index": index, "duration_seconds": 45, "distance_meters": 0}
                for index in range(4)
            ]
        },
    )

    session = StrengthAnalyzer().analyze(_raw([workout])).recent_sessions[0]

    assert session.explicit_exercises == []
    assert session.estimated_work_bouts == 4
    assert session.median_work_seconds == 45
    assert session.hypotheses[0].exercise_name is None
    assert session.hypotheses[0].movement_pattern == "unknown"
    assert "不能区分" in session.hypotheses[0].evidence[1]
    assert session.heart_rate_zones


def test_strength_analysis_supports_explicit_five_day_rotation():
    focuses = ["CHEST", "BACK", "LEGS", "SHOULDERS", "ARMS"]
    names = ["卧推", "划船", "深蹲", "侧平举", "弯举"]
    workouts = [
        _workout(
            TARGET - timedelta(days=4 - index),
            f"five-{index}",
            [StrengthExerciseInput(exercise_name=name, sets=3)],
            focus,
        )
        for index, (focus, name) in enumerate(zip(focuses, names))
    ]

    analysis = StrengthAnalyzer().analyze(_raw(workouts))

    assert analysis.detected_split == "FIVE_DAY"
    assert analysis.next_focus == "CHEST"
