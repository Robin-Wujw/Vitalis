from datetime import date
from types import SimpleNamespace

from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.intelligence.contracts import (
    Availability,
    ConfidenceBand,
    DecisionAction,
    LoadState,
    RecoveryState,
    SleepState,
    StrengthAnalysis,
    StrengthExerciseInput,
    StrengthSessionAnalysis,
    TrainingFeatures,
    TrainingPreferences,
)
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.strength import StrengthAnalyzer, normalize_exercise


TARGET = date(2026, 8, 28)


def test_strength_detail_accepts_json_or_list_and_only_uses_kg_weights():
    payload = {
        "data": {
            "trackid": 1_700_000_000,
            "strengthSets": [
                {"exerciseName": "卧推", "repetitions": "8", "weight": 60},
                {"exerciseName": "卧推", "reps": 8, "weight": "60", "weightUnit": "kg"},
                {"exerciseName": "卧推", "reps": 8, "weightKg": "62.5"},
            ],
        }
    }

    detail = ZeppParser.parse_workout_detail(payload)

    assert detail is not None
    assert [item.repetitions for item in detail.strength_sets] == [8, 8, 8]
    assert [item.weight_kg for item in detail.strength_sets] == [None, 60, 62.5]


def test_vendor_strength_sets_merge_identical_doses_and_preserve_variations():
    detail = ZeppParser.parse_workout_detail({
        "data": {
            "trackid": 1_700_000_000,
            "strengthSets": [
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 10, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 62.5},
            ],
        }
    })

    records = StrengthAnalyzer._vendor_exercises(
        "u", {"workout_id": "w", "detail": detail.model_dump()}
    )

    assert [(item.repetitions, item.weight_kg, item.sets) for item in records] == [
        ("8", 60, 2),
        ("10", 60, 1),
        ("8", 62.5, 1),
    ]


def test_confirmed_exercises_are_the_only_source_when_present():
    confirmed = normalize_exercise(
        "u", "w", 1, StrengthExerciseInput(exercise_name="卧推", sets=4), "PUSH"
    )
    raw = SimpleNamespace(user_id="u", feedback_by_workout={})
    workout = {
        "workout_id": "w",
        "source": "zepp",
        "local_day": TARGET,
        "confirmed_exercises": [confirmed],
        "detail": {"strength_sets": [{"exercise_name": "深蹲", "repetitions": 8}]},
        "samples": [],
        "data": {"duration": 45},
    }

    session = StrengthAnalyzer()._session(raw, workout, None)

    assert [item.exercise_name for item in session.explicit_exercises] == ["卧推"]
    assert session.total_sets == 4


def test_unresolved_split_reuses_recognizable_recent_actions():
    exercise = normalize_exercise(
        "u", "prior", 1, StrengthExerciseInput(
            exercise_name="卧推", sets=4, repetitions="8 次", weight_kg=60
        ), "PUSH"
    )
    recent = StrengthSessionAnalysis(
        workout_id="prior",
        date=TARGET,
        duration_minutes=45,
        focus="PUSH",
        focus_label="推类",
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        explicit_exercises=[exercise],
        movement_patterns=["horizontal_push"],
        movement_pattern_labels=["水平推"],
        muscle_groups=["chest", "shoulders", "triceps"],
        muscle_group_labels=["胸部", "肩部", "肱三头肌"],
        total_sets=4,
    )
    strength = StrengthAnalysis(
        status=Availability.AVAILABLE,
        status_label="数据可用",
        sessions_7d=1,
        sessions_28d=1,
        explicit_session_coverage=1,
        recent_sessions=[recent],
    )
    training = SimpleNamespace(strength=strength)

    planned = DecisionEngine()._strength(
        TrainingPreferences(user_id="u"), training, "PRIMARY", "moderate"
    )

    assert planned.title == "推类训练"
    assert planned.code == "strength_push"
    assert next(step for step in planned.steps if step.name == "卧推").sets == 4


def test_parser_to_analyzer_to_planner_preserves_four_variable_dose_sets():
    detail = ZeppParser.parse_workout_detail({
        "data": {
            "trackid": 1_700_000_000,
            "strengthSets": [
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 10, "weightKg": 60},
                {"exerciseId": "bench_press", "exerciseName": "卧推", "reps": 8, "weightKg": 62.5},
            ],
        }
    })
    raw = SimpleNamespace(user_id="u", feedback_by_workout={})
    session = StrengthAnalyzer()._session(raw, {
        "workout_id": "w",
        "source": "zepp",
        "local_day": TARGET,
        "confirmed_exercises": [],
        "detail": detail.model_dump(),
        "samples": [],
        "data": {"duration": 45},
    }, None)
    strength = StrengthAnalysis(
        status=Availability.AVAILABLE,
        status_label="数据可用",
        sessions_7d=1,
        sessions_28d=1,
        explicit_session_coverage=1,
        next_focus="PUSH",
        next_focus_label="推类",
        recent_sessions=[session],
    )

    planned = DecisionEngine()._strength(
        TrainingPreferences(user_id="u"), SimpleNamespace(strength=strength),
        "PRIMARY", "moderate"
    )
    steps = [step for step in planned.steps if step.name == "卧推"]

    assert [(step.repetitions, step.load_kg, step.sets) for step in steps] == [
        ("8", 60, 2),
        ("10", 60, 1),
        ("8", 62.5, 1),
    ]


def test_lower_soreness_restricts_full_body_strength_dose_and_focus():
    exercise = normalize_exercise(
        "u", "prior", 1, StrengthExerciseInput(exercise_name="深蹲", sets=4), "FULL_BODY"
    )
    recent = StrengthSessionAnalysis(
        workout_id="prior",
        date=TARGET,
        duration_minutes=45,
        focus="FULL_BODY",
        focus_label="全身",
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        explicit_exercises=[exercise],
    )
    strength = StrengthAnalysis(
        status=Availability.AVAILABLE,
        status_label="数据可用",
        sessions_7d=1,
        sessions_28d=1,
        explicit_session_coverage=1,
        recent_sessions=[recent],
    )

    planned = DecisionEngine()._strength(
        TrainingPreferences(user_id="u"), SimpleNamespace(strength=strength),
        "PRIMARY", "moderate", ["已记录下肢明显酸痛，跑步与腿部力量都降低剂量。"]
    )

    assert planned.intensity == "low"
    assert {step.name for step in planned.steps} == {"动态热身", "水平推", "水平拉", "垂直推或拉"}
    assert [step.sets for step in planned.steps[1:]] == [2, 2, 1]


def test_primary_type_uses_valid_long_term_tie_breaker_after_short_term_tie():
    training = SimpleNamespace(
        running=SimpleNamespace(sessions_7d=3, sessions_28d=9),
        strength=SimpleNamespace(sessions_7d=3, sessions_28d=3),
        recent_workouts=[],
        history_coverage={"status": "COMPLETE", "prior_7d_verified": True},
    )
    preferences = TrainingPreferences(
        user_id="u", weekly_running_target=3, weekly_strength_target=3
    )
    balance = DecisionEngine._balance(training, preferences)

    assert balance.running_due is False
    assert balance.strength_due is False
    assert DecisionEngine._primary_type(training, balance, preferences) == "STRENGTH"


def test_explicitly_incomplete_training_history_blocks_prescription_only():
    training = TrainingFeatures.model_construct(
        status=Availability.AVAILABLE,
        status_label="数据可用",
        running=None,
        strength=None,
        limitations=[],
        load_state=LoadState.NORMAL,
        history_coverage={"status": "PARTIAL", "prior_7d_verified": False},
    )
    sleep = SimpleNamespace(limitations=[], duration_minutes=None, duration_deviation=None)
    hrv = SimpleNamespace(
        limitations=[], value_ms=None, deviation=None, fusion_direction="unknown",
        rhr_bpm=None, rhr_deviation=None, recent_7d_median_ms=None,
        previous_7d_median_ms=None, recent_7d_change_percent=None,
        recent_7d_direction="unknown",
    )
    recovery = SimpleNamespace(
        state=RecoveryState.NORMAL, limitations=[], positive_signals=[], negative_signals=[]
    )

    decision = DecisionEngine().decide(
        "coverage-gate", TARGET, sleep, SleepState.NEAR_BASELINE,
        hrv, recovery, training, TrainingPreferences(user_id="u")
    )

    assert decision.action == DecisionAction.INSUFFICIENT_DATA
    assert decision.action_plan.primary_session is None
    assert decision.rule_ids == ["DECISION.TRAINING_HISTORY_COVERAGE_INSUFFICIENT"]
    assert decision.evidence.gates[0].expected_condition == "prior_7d_verified=True"
    assert any("此前 7 天训练记录" in item for item in decision.limitations)


def test_low_intensity_history_reuse_does_not_suggest_progression():
    exercise = normalize_exercise(
        "u", "prior", 1,
        StrengthExerciseInput(exercise_name="卧推", sets=4, repetitions="8", rir=4),
        "PUSH",
    )
    prior = SimpleNamespace(focus_label="推类", explicit_exercises=[exercise])
    _, _, steps = DecisionEngine._strength_from_prior(prior, "low")
    assert steps[1].sets == 2
    assert not any("增加" in line for step in steps for line in step.instructions)


def test_known_next_focus_is_not_replaced_by_unrelated_history():
    exercise = normalize_exercise(
        "u", "prior", 1, StrengthExerciseInput(exercise_name="卧推", sets=4), "PUSH"
    )
    prior = StrengthSessionAnalysis(
        workout_id="prior", date=TARGET, duration_minutes=45,
        focus="PUSH", focus_label="推类", confidence=ConfidenceBand.HIGH,
        confidence_label="较高", explicit_exercises=[exercise],
    )
    strength = StrengthAnalysis(
        status=Availability.AVAILABLE, status_label="可用", sessions_7d=1,
        sessions_28d=1, explicit_session_coverage=1,
        next_focus="PULL", next_focus_label="拉类", recent_sessions=[prior],
    )
    planned = DecisionEngine()._strength(
        TrainingPreferences(user_id="u"), SimpleNamespace(strength=strength),
        "PRIMARY", "moderate",
    )
    assert planned.code == "strength_pull"
    assert not any(step.name == "卧推" for step in planned.steps)


def test_empty_cloud_sets_do_not_turn_assessment_ids_into_exercises():
    detail = ZeppParser.parse_workout_detail({"data": {
        "trackid": 1_700_000_000,
        "strengthSets": "[]",
        "strengthAssess": '[{"idx":0,"eq":[{"bcId":[1,2,3],"bcSce":[20,30,40]}]}]',
        "lap": "0,59,-1,0,0;1,45,-1,0,0",
    }})
    session = StrengthAnalyzer()._session(
        SimpleNamespace(user_id="u", feedback_by_workout={}),
        {
            "workout_id": "cloud-empty", "local_day": TARGET,
            "detail_available": True, "detail": detail.model_dump(),
            "samples": [], "data": {"duration": 45},
        },
        None,
    )
    assert session.explicit_exercises == []
    assert session.total_sets is None
    assert session.focus == "UNKNOWN"
    assert any("App 中的修正内容" in value for value in session.limitations)
