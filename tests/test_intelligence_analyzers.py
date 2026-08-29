from datetime import date, timedelta

from vitalis.intelligence.analyzers import HrvAnalyzer, RecoveryAnalyzer, SleepAnalyzer, TrainingAnalyzer
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import (
    DecisionAction,
    LoadState,
    RecoveryState,
    SleepState,
    StrengthExerciseInput,
    TrainingPreferences,
)
from vitalis.intelligence.decision import DecisionEngine
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.strength import normalize_exercise


TARGET = date(2026, 8, 28)


def _series(metric, values, *, device=None, scope="normalized_daily_record", unit="ms"):
    return [
        SeriesPoint(metric=metric, value=value, unit=unit, day=TARGET - timedelta(days=offset),
                    observed_at=TARGET - timedelta(days=offset), source="zepp",
                    source_scope=scope, device_id=device)
        for offset, value in enumerate(values)
    ]


def _profile(hrv_today=40, rhr_today=64, sleep_today=360, load_today=20):
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.training_preferences = TrainingPreferences(user_id="u")
    raw.series = {
        "hrv_rmssd": _series("hrv_rmssd", [hrv_today] + [50 + (i % 3) for i in range(1, 22)], device="helio", scope="device"),
        "resting_hr": _series("resting_hr", [rhr_today] + [56 + (i % 2) for i in range(1, 22)], unit="bpm"),
        "sleep_duration": _series("sleep_duration", [sleep_today] + [450 + (i % 3) * 5 for i in range(1, 22)], unit="min"),
        "training_load": _series("training_load", [load_today] + [30 + (i % 3) for i in range(1, 22)], unit="load"),
    }
    raw.sleep_by_day = {
        point.day: {"date": point.day, "sleep_duration": int(point.value), "source": "zepp"}
        for point in raw.series["sleep_duration"]
    }
    raw.training_by_day = {
        point.day: {"date": point.day, "total_load": point.value, "total_duration": 30, "workout_count": 1}
        for point in raw.series["training_load"]
    }
    return raw


def _analyze(raw):
    baselines = BaselineEngine().build(raw.series, raw.day)
    sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
    hrv = HrvAnalyzer().analyze(raw, baselines)
    training = TrainingAnalyzer().analyze(raw, baselines)
    recovery = RecoveryAnalyzer().analyze(raw, sleep, sleep_state, hrv, training)
    decision = DecisionEngine().decide(
        "analyzer-recommendation",
        TARGET,
        sleep_state,
        hrv,
        recovery,
        training,
        raw.training_preferences,
    )
    return sleep, sleep_state, hrv, training, recovery, decision


def test_multisignal_suppression_produces_rest_with_explanation():
    _, sleep_state, hrv, _, recovery, decision = _analyze(_profile())
    assert sleep_state == SleepState.BELOW_BASELINE
    assert hrv.deviation.direction == "below"
    assert hrv.rhr_deviation.direction == "above"
    assert recovery.state == RecoveryState.SUPPRESSED
    assert decision.action == DecisionAction.REST
    assert decision.rule_ids == ["DECISION.MULTISIGNAL_SUPPRESSION_REST"]
    assert len(decision.drivers) >= 3


def test_good_recovery_and_low_load_can_produce_hard_training():
    _, sleep_state, _, training, recovery, decision = _analyze(
        _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    )
    assert sleep_state == SleepState.ABOVE_BASELINE
    assert training.load_state == LoadState.LOW
    assert recovery.state == RecoveryState.GOOD
    assert decision.action == DecisionAction.TRAIN_HARD


def test_decision_abstains_when_baselines_are_not_interpretable():
    raw = RawDailyProfile(user_id="u", day=TARGET)
    raw.training_preferences = TrainingPreferences(user_id="u")
    raw.series = {
        "hrv_rmssd": _series("hrv_rmssd", [50], device="helio", scope="device"),
        "sleep_duration": _series("sleep_duration", [450], unit="min"),
    }
    raw.sleep_by_day = {TARGET: {"date": TARGET, "sleep_duration": 450, "source": "zepp"}}
    *_, recovery, decision = _analyze(raw)
    assert recovery.state == RecoveryState.INSUFFICIENT_DATA
    assert decision.action == DecisionAction.INSUFFICIENT_DATA
    assert decision.confidence.value == "NONE"


def test_training_preserves_recent_workout_types_and_details():
    raw = _profile()
    raw.workouts = [
        {
            "local_day": TARGET - timedelta(days=1),
            "detail_available": True,
            "data": {
                "type": "strength",
                "sport_mode": "strength_training",
                "sport_mode_label": "力量训练",
                "training_family": "strength",
                "recognition_confidence": "HIGH",
                "recognition_confidence_label": "较高",
                "recognition_source": "public_zepp_enum",
                "recognition_source_label": "公开 Zepp/Huami 运动枚举",
                "vendor_type_id": 52,
                "duration": 47,
                "load": 9,
                "heart_rate_avg": 103,
                "heart_rate_max": 142,
            },
        },
        {
            "local_day": TARGET - timedelta(days=2),
            "detail_available": True,
            "data": {
                "type": "running",
                "sport_mode": "outdoor_running",
                "sport_mode_label": "户外跑",
                "training_family": "aerobic",
                "recognition_confidence": "HIGH",
                "recognition_confidence_label": "较高",
                "recognition_source": "public_zepp_enum",
                "recognition_source_label": "公开 Zepp/Huami 运动枚举",
                "vendor_type_id": 1,
                "duration": 33,
                "load": 92,
                "heart_rate_avg": 151,
                "heart_rate_max": 173,
            },
        },
    ]

    training = TrainingAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    assert training.workout_type_counts_7d == {"running": 1, "strength": 1}
    assert training.strength_sessions_7d == 1
    assert training.aerobic_minutes_7d == 33
    assert [item.type for item in training.recent_workouts] == ["strength", "running"]
    assert training.recent_workouts[0].vendor_type_id == 52
    assert training.recent_workouts[0].sport_mode_label == "力量训练"
    assert training.recent_workouts[0].recognition_confidence_label == "较高"
    assert training.recent_workouts[1].heart_rate_max_bpm == 173


def test_decision_returns_primary_running_and_optional_strength_plan():
    raw = _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    raw.workouts = []
    *_, decision = _analyze(raw)

    assert decision.action_label == "高负荷训练"
    assert decision.confidence_label in {"中等", "较高"}
    action_plan = decision.action_plan
    assert action_plan.goal_label == "综合健康优先，跑步与力量并行"
    assert action_plan.primary_session.title == "阈值间歇跑"
    assert action_plan.optional_session.session_type == "STRENGTH"
    assert action_plan.session_relationship == "ALTERNATIVE"
    assert action_plan.weekly_balance.running_due is True
    assert action_plan.weekly_balance.strength_due is True
    strength_steps = [step.name for step in action_plan.optional_session.steps]
    assert {"下蹲模式", "水平推", "水平或垂直拉", "髋伸模式", "核心稳定"} <= set(strength_steps)
    assert action_plan.primary_session.evidence
    assert action_plan.primary_session.stop_conditions
    assert action_plan.missing_input_gates


def test_recent_strength_keeps_running_primary_and_strength_as_alternative():
    raw = _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    raw.workouts = [{
        "local_day": TARGET - timedelta(days=1),
        "data": {
            "type": "strength",
            "sport_mode": "strength_training",
            "sport_mode_label": "力量训练",
            "training_family": "strength",
            "duration": 40,
            "load": 20,
        },
    }]
    *_, decision = _analyze(raw)

    assert decision.action_plan.primary_session.session_type == "RUNNING"
    assert decision.action_plan.optional_session.session_type == "STRENGTH"
    assert decision.action_plan.session_relationship == "ALTERNATIVE"


def test_recorded_pain_blocks_planned_training():
    raw = _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    raw.training_preferences = TrainingPreferences(
        user_id="u",
        pain_or_injury_status="PRESENT",
        pain_or_injury_notes="膝部疼痛",
    )

    *_, decision = _analyze(raw)

    assert decision.action == DecisionAction.REST
    assert decision.rule_ids == ["DECISION.PAIN_OR_INJURY_SAFETY_GATE"]
    assert decision.action_plan.safety_status == "LIMITED"
    assert decision.action_plan.primary_session.session_type == "REST"
    assert decision.action_plan.optional_session is None


def test_recent_leg_session_suppresses_quality_run_conflict():
    raw = _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    workout_id = "recent-legs"
    raw.workouts = [{
        "workout_id": workout_id,
        "local_day": TARGET - timedelta(days=1),
        "confirmed_exercises": [normalize_exercise(
            "u",
            workout_id,
            1,
            StrengthExerciseInput(exercise_name="深蹲", sets=4),
            "LEGS",
        )],
        "samples": [],
        "detail": None,
        "data": {
            "type": "strength",
            "sport_mode": "strength_training",
            "sport_mode_label": "力量训练",
            "training_family": "strength",
            "duration": 50,
            "load": 30,
        },
    }]

    *_, decision = _analyze(raw)

    assert decision.action == DecisionAction.TRAIN_HARD
    assert decision.action_plan.primary_session.session_type == "RUNNING"
    assert decision.action_plan.primary_session.title == "轻松跑"
    assert "近 48 小时已有腿部力量训练，今天不安排高强度跑。" in (
        decision.action_plan.conflict_checks
    )


def test_hrv_exposes_all_device_streams_without_merging_them():
    raw = _profile(hrv_today=62)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "BALANCE": "Amazfit Balance 2",
    }
    raw.series["hrv_rmssd"] += _series(
        "hrv_rmssd",
        [58] + [48 + (i % 3) for i in range(1, 22)],
        device="balance",
        scope="device",
    )

    hrv = HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    assert {stream.device_id for stream in hrv.streams} == {"helio", "balance"}
    assert {stream.value_ms for stream in hrv.streams} == {58, 62}
    assert sum(stream.selected for stream in hrv.streams) == 1
    assert hrv.preferred_device_label == "Amazfit Helio Strap"
    assert hrv.fusion_direction == "above"
    assert hrv.fusion_confidence.value == "HIGH"
    assert hrv.fused_device_count == 2
    assert "方向一致" in hrv.fusion_summary
    assert not hrv.limitations


def test_hrv_device_disagreement_is_not_averaged_into_a_recovery_signal():
    raw = _profile(hrv_today=62)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "BALANCE": "Amazfit Balance 2",
    }
    raw.series["hrv_rmssd"] += _series(
        "hrv_rmssd",
        [40] + [60 + (i % 3) for i in range(1, 22)],
        device="balance",
        scope="device",
    )

    baselines = BaselineEngine().build(raw.series, raw.day)
    sleep, sleep_state = SleepAnalyzer().analyze(raw, baselines)
    hrv = HrvAnalyzer().analyze(raw, baselines)
    training = TrainingAnalyzer().analyze(raw, baselines)
    recovery = RecoveryAnalyzer().analyze(
        raw, sleep, sleep_state, hrv, training
    )

    assert hrv.fusion_direction == "mixed"
    assert hrv.fusion_confidence.value == "LOW"
    assert "multi_device_hrv_disagreement" in hrv.limitations
    assert "HRV_ABOVE_BASELINE" not in recovery.positive_signals
    assert "HRV_BELOW_BASELINE" not in recovery.negative_signals
