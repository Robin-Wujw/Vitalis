from datetime import date, datetime, time, timedelta, timezone

import pytest

from vitalis.intelligence.analyzers import (
    HrvAnalyzer,
    OvernightVitalsAnalyzer,
    RecoveryAnalyzer,
    SleepAnalyzer,
    TrainingAnalyzer,
)
from vitalis.intelligence.baseline import BaselineEngine
from vitalis.intelligence.contracts import (
    Availability,
    ConfidenceBand,
    DecisionAction,
    LoadState,
    RecoveryState,
    RunningAnalysis,
    RunningSessionAnalysis,
    SleepState,
    StrengthExerciseInput,
    TrainingFeatures,
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
        "training_load": _series(
            "training_load",
            [load_today] + [30 + (i % 3) for i in range(1, 36)],
            unit="load",
        ),
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


def _with_genuinely_low_recent_load(raw):
    raw.training_by_day = {
        day: value for day, value in raw.training_by_day.items()
        if day < TARGET - timedelta(days=7)
    }
    return raw


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
        _with_genuinely_low_recent_load(
            _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
        )
    )
    assert sleep_state == SleepState.ABOVE_BASELINE
    assert training.load_state == LoadState.LOW
    assert recovery.state == RecoveryState.GOOD
    assert decision.action == DecisionAction.TRAIN_HARD


def test_empty_morning_load_does_not_make_the_recent_week_low():
    raw = _profile(
        hrv_today=62, rhr_today=50, sleep_today=510, load_today=0
    )

    training = TrainingAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    assert training.today_load == 0
    assert training.load_7d > 0
    assert training.load_state == LoadState.NORMAL
    assert training.load_7d_reference is not None


def test_rolling_training_load_includes_target_day():
    raw = _profile(load_today=100)

    training = TrainingAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    assert training.today_load == 100
    assert training.load_7d == 286
    assert training.duration_7d == 210


def test_training_status_uses_vo2max_threshold_and_pai_without_combining_scores():
    raw = _profile()
    raw.series.update({
        "vo2max": _series("vo2max", [52, 50], unit="ml/kg/min"),
        "lactate_threshold_hr": _series(
            "lactate_threshold_hr", [171, 169], unit="bpm"
        ),
        "lactate_threshold_pace": _series(
            "lactate_threshold_pace", [270, 282], unit="s/km"
        ),
        "pai_daily": _series("pai_daily", [8, 7, 6, 5, 4, 3, 2], unit="pai"),
        "pai_low_zone": _series("pai_low_zone", [1] * 7, unit="pai"),
        "pai_medium_zone": _series("pai_medium_zone", [2] * 7, unit="pai"),
        "pai_high_zone": _series("pai_high_zone", [4] * 7, unit="pai"),
    })
    # Historical reference must be at least seven days before the latest value.
    raw.series["vo2max"][1] = raw.series["vo2max"][1].__class__(
        **{**raw.series["vo2max"][1].__dict__, "day": TARGET - timedelta(days=14),
           "observed_at": TARGET - timedelta(days=14)}
    )
    raw.series["lactate_threshold_pace"][1] = raw.series["lactate_threshold_pace"][1].__class__(
        **{**raw.series["lactate_threshold_pace"][1].__dict__, "day": TARGET - timedelta(days=14),
           "observed_at": TARGET - timedelta(days=14)}
    )

    status = TrainingAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    ).training_status

    assert status.vo2max_ml_kg_min == 52
    assert status.vo2max_change_28d_percent == 4
    assert status.lactate_threshold_pace_seconds_per_km == 270
    assert status.lactate_threshold_pace_change_28d_percent == pytest.approx(-4.3)
    assert status.pai_earned_7d == 35
    assert status.dominant_pai_zone == "high"
    assert len(status.conclusions) == 3


def test_near_baseline_signals_are_interpretable_normal_recovery():
    *_, recovery, decision = _analyze(
        _profile(hrv_today=51, rhr_today=56, sleep_today=452, load_today=31)
    )

    assert recovery.state == RecoveryState.NORMAL
    assert recovery.positive_signals == []
    assert recovery.negative_signals == []
    assert decision.action == DecisionAction.TRAIN_NORMAL
    assert decision.action_plan.primary_session is not None


def test_hrv_exposes_gap_aware_minute_curve_without_interpretation():
    raw = _profile(hrv_today=55)
    raw.series["hrv_rmssd"].extend([
        SeriesPoint(
            metric="hrv_rmssd", value=value, unit="ms", day=TARGET,
            observed_at=observed_at, source="zepp", source_scope="device",
            device_id="helio",
        )
        for observed_at, value in (
            (datetime(2026, 8, 27, 16, 5, 10, tzinfo=timezone.utc), 40),
            (datetime(2026, 8, 27, 16, 5, 50, tzinfo=timezone.utc), 60),
            (datetime(2026, 8, 27, 16, 35, tzinfo=timezone.utc), 70),
            (datetime(2026, 8, 28, 4, 1, tzinfo=timezone.utc), 50),
        )
    ])

    feature = HrvAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    curve = feature.daily_curve
    assert curve is not None
    assert curve.sample_count == 4
    assert curve.covered_minutes == 3
    assert curve.first_sample_time == "00:05"
    assert curve.last_sample_time == "12:01"
    assert curve.bin_minutes == 1
    assert curve.device_id == "helio"
    assert curve.selection_basis == "widest_target_day_coverage"
    assert [point.time for point in curve.points] == ["00:05", "00:35", "12:01"]
    assert curve.points[0].median_ms == 50


def test_hrv_curve_selects_coverage_independently_from_recovery_stream():
    raw = _profile(hrv_today=55)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "BALANCE": "Amazfit Balance 2",
    }
    raw.series["hrv_rmssd"].extend([
        SeriesPoint(
            metric="hrv_rmssd", value=50 + offset, unit="ms", day=TARGET,
            observed_at=datetime(2026, 8, 27, 16, offset, tzinfo=timezone.utc),
            source="zepp", source_scope="device", device_id=device,
        )
        for device, offsets in (("helio", (0, 5)), ("balance", (0, 5, 10, 15)))
        for offset in offsets
    ])

    feature = HrvAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    assert feature.preferred_device_id == "helio"
    assert feature.daily_curve is not None
    assert feature.daily_curve.device_id == "balance"
    assert feature.daily_curve.sample_count == 4


def test_sleep_hrv_drives_recovery_while_rmssd_drives_all_day_curve():
    raw = _profile(hrv_today=40)
    raw.series["sleep_hrv"] = _series(
        "sleep_hrv", [73] + [64 + (offset % 2) for offset in range(1, 22)],
        device="helio", scope="device",
    )
    raw.series["hrv_rmssd"].extend([
        SeriesPoint(
            metric="hrv_rmssd", value=value, unit="ms", day=TARGET,
            observed_at=observed_at, source="zepp", source_scope="device",
            device_id="balance",
        )
        for observed_at, value in (
            (datetime(2026, 8, 27, 18, 1, tzinfo=timezone.utc), 33),
            (datetime(2026, 8, 27, 18, 2, tzinfo=timezone.utc), 134),
        )
    ])

    feature = HrvAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    assert feature.preferred_metric == "sleep_hrv"
    assert feature.value_ms == 73
    assert feature.daily_curve is not None
    assert feature.daily_curve.metric == "hrv_rmssd"
    assert feature.daily_curve.device_id == "balance"


def test_hrv_exposes_canonical_daily_sleep_hrv_trend():
    raw = _profile(hrv_today=55)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "BALANCE": "Amazfit Balance 2",
    }
    raw.series["sleep_hrv"] = _series(
        "sleep_hrv", [73] + [60 + (offset % 4) for offset in range(1, 28)],
        device="balance", scope="device",
    )
    raw.series["sleep_hrv"].extend(_series(
        "sleep_hrv", [72] + [58 + (offset % 3) for offset in range(1, 11)],
        device="helio", scope="device",
    ))

    feature = HrvAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )

    trend = feature.sleep_hrv_daily_trend
    assert trend is not None
    assert trend.device_id == "balance"
    assert trend.device_label == "Amazfit Balance 2"
    assert trend.today_value_ms == 73
    assert trend.today_sample_count == 1
    assert len(trend.points) == 7


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
    raw = _with_genuinely_low_recent_load(
        _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    )
    raw.workouts = []
    *_, decision = _analyze(raw)

    assert decision.action_label == "高负荷训练"
    assert decision.confidence_label in {"中等", "较高"}
    action_plan = decision.action_plan
    assert action_plan.goal_label == "综合健康优先，跑步与力量并行"
    assert action_plan.primary_session.title == "轻松跑"
    assert action_plan.primary_session.code == "easy_run"
    assert action_plan.optional_session.session_type == "STRENGTH"
    assert action_plan.session_relationship == "ALTERNATIVE"
    assert action_plan.weekly_balance.running_due is True
    assert action_plan.weekly_balance.strength_due is True
    strength_steps = [step.name for step in action_plan.optional_session.steps]
    assert {"下蹲模式", "水平推", "水平或垂直拉", "髋伸模式", "核心稳定"} <= set(strength_steps)
    assert action_plan.primary_session.evidence
    assert action_plan.primary_session.stop_conditions
    assert action_plan.missing_input_gates


def test_nocturnal_heart_rate_and_same_device_hrv_history_are_analyzed():
    raw = _profile(hrv_today=45, rhr_today=56, sleep_today=240, load_today=20)
    raw.series["hrv_rmssd"] = _series(
        "hrv_rmssd",
        [45] * 7 + [55] * 7,
        device="helio",
        scope="device",
    )
    raw.sleep_by_day = {}
    raw.heart_rate_samples = []
    for offset in range(9):
        sleep_day = TARGET - timedelta(days=offset)
        raw.sleep_by_day[sleep_day] = {
            "date": sleep_day,
            "sleep_duration": 240,
            "bedtime": time(0, 0),
            "wake_time": time(4, 0),
            "source": "zepp",
        }
        for minute in range(240):
            value = 60
            if offset == 0 and minute >= 120:
                value = 56
            local_at = datetime.combine(
                sleep_day, time.min, tzinfo=timezone(timedelta(hours=8))
            ) + timedelta(minutes=minute)
            raw.heart_rate_samples.append(SeriesPoint(
                metric="heart_rate",
                value=value,
                unit="bpm",
                day=sleep_day,
                observed_at=local_at.astimezone(timezone.utc),
                source="zepp",
                source_scope="device",
                device_id="helio",
            ))

    hrv = HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))
    night = hrv.nocturnal_heart_rate

    assert hrv.recent_7d_median_ms == 45
    assert hrv.previous_7d_median_ms == 55
    assert hrv.recent_7d_change_percent == -18.2
    assert hrv.recent_7d_direction == "below"
    assert night.status.value == "AVAILABLE"
    assert night.coverage_ratio == 1
    assert night.low_5m_bpm == 56
    assert night.second_minus_first_bpm == -4
    assert night.baseline_nights == 8
    assert night.direction == "below"

    raw.series["hrv_rmssd"] = []
    without_hrv = HrvAnalyzer().analyze(
        raw, BaselineEngine().build(raw.series, raw.day)
    )
    assert without_hrv.status == Availability.INSUFFICIENT_DATA
    assert without_hrv.nocturnal_heart_rate.status == Availability.AVAILABLE
    assert without_hrv.rhr_deviation.direction == "below"


def test_nocturnal_heart_rate_prefers_complete_upper_arm_stream():
    raw = _profile(hrv_today=45, rhr_today=56, sleep_today=240, load_today=20)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "WATCH": "Amazfit Balance 2",
    }
    raw.sleep_by_day = {
        TARGET: {
            "date": TARGET,
            "sleep_duration": 240,
            "bedtime": time(0, 0),
            "wake_time": time(4, 0),
            "source": "zepp",
        }
    }
    raw.heart_rate_samples = []
    for device_id, value in (("HELIO", 52), ("WATCH", 58)):
        for minute in range(240):
            local_at = datetime.combine(
                TARGET, time.min, tzinfo=timezone(timedelta(hours=8))
            ) + timedelta(minutes=minute)
            raw.heart_rate_samples.append(SeriesPoint(
                metric="heart_rate",
                value=value,
                unit="bpm",
                day=TARGET,
                observed_at=local_at.astimezone(timezone.utc),
                source="zepp",
                source_scope="device",
                device_id=device_id,
            ))

    hrv = HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    assert hrv.nocturnal_heart_rate.device_id == "HELIO"
    assert hrv.nocturnal_heart_rate.measurement_site == "upper_arm"
    assert hrv.nocturnal_heart_rate.median_bpm == 52


def test_recent_strength_keeps_running_primary_and_strength_as_alternative():
    raw = _with_genuinely_low_recent_load(
        _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    )
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
    raw = _with_genuinely_low_recent_load(
        _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    )
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
    raw = _with_genuinely_low_recent_load(
        _profile(hrv_today=62, rhr_today=50, sleep_today=510, load_today=0)
    )
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


def test_high_cardiac_drift_changes_quality_run_to_short_recovery_run():
    latest = RunningSessionAnalysis(
        workout_id="run-high-drift",
        date=TARGET - timedelta(days=2),
        classification="EASY_RUN",
        classification_label="轻松跑",
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
        duration_minutes=48,
        average_pace_seconds_per_km=360,
        median_cadence_spm=172,
        cardiac_drift_percent=8.5,
    )
    training = TrainingFeatures(
        status=Availability.AVAILABLE,
        running=RunningAnalysis(
            status=Availability.AVAILABLE,
            status_label="可用",
            zone_method="lactate_threshold",
            lactate_threshold_bpm=170,
            sessions_7d=1,
            duration_minutes_7d=48,
            sessions_28d=1,
            duration_minutes_28d=48,
            recent_sessions=[latest],
        ),
    )

    session = DecisionEngine()._running(
        TARGET,
        TrainingPreferences(user_id="u"),
        training,
        "PRIMARY",
        True,
        "high",
    )

    assert session.code == "recovery_run"
    assert session.total_duration_minutes == (31, 35)
    assert any("本次因此缩短" in item for item in session.personalization_reasons)
    assert "沿用你最近自然步频约 172 步/分钟" in session.steps[1].instructions[1]


def test_strength_plan_reuses_confirmed_exercise_dose_and_effort():
    raw = _profile(hrv_today=55, rhr_today=55, sleep_today=450, load_today=20)
    workouts = []
    for offset, workout_id, focus, exercise in [
        (4, "push", "PUSH", StrengthExerciseInput(
            exercise_name="卧推",
            sets=4,
            repetitions="8 次",
            weight_kg=60,
            rir=3,
            rest_seconds=150,
        )),
        (2, "pull", "PULL", StrengthExerciseInput(exercise_name="划船", sets=4)),
        (1, "legs", "LEGS", StrengthExerciseInput(exercise_name="深蹲", sets=4)),
    ]:
        workouts.append({
            "workout_id": workout_id,
            "local_day": TARGET - timedelta(days=offset),
            "confirmed_exercises": [normalize_exercise("u", workout_id, 1, exercise, focus)],
            "samples": [],
            "detail": None,
            "data": {
                "type": "strength",
                "training_family": "strength",
                "duration": 45,
            },
        })
    raw.workouts = workouts
    training = TrainingAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    session = DecisionEngine()._strength(
        TrainingPreferences(user_id="u"), training, "PRIMARY", "moderate"
    )
    bench = next(step for step in session.steps if step.name == "卧推")

    assert session.title == "推类训练"
    assert bench.sets == 4
    assert bench.repetitions == "8 次"
    assert bench.load_kg == 60
    assert bench.rest_seconds == (150, 150)
    assert any("增加少量重量" in item for item in bench.instructions)
    assert "ACSM_RESISTANCE_TRAINING_2026" in session.evidence_ref_ids


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
    assert hrv.fusion_confidence.value == "MODERATE"
    assert hrv.corroboration_status == "consistent"
    assert hrv.corroborating_stream_count == 1
    assert not hrv.corroboration_affects_decision
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
    decision = DecisionEngine().decide(
        "disagreement-recommendation",
        TARGET,
        sleep_state,
        hrv,
        recovery,
        training,
        raw.training_preferences,
    )

    assert hrv.fusion_direction == "unknown"
    assert hrv.fusion_confidence.value == "LOW"
    assert hrv.corroboration_status == "conflicting"
    assert hrv.corroboration_affects_decision
    assert "multi_device_hrv_disagreement" in hrv.limitations
    assert "HRV_ABOVE_BASELINE" not in recovery.positive_signals
    assert "HRV_BELOW_BASELINE" not in recovery.negative_signals
    assert decision.confidence.value == "LOW"


def test_hrv_near_and_above_are_not_treated_as_opposite_directions():
    raw = _profile(hrv_today=62)
    raw.series["hrv_rmssd"] += _series(
        "hrv_rmssd",
        [50] * 22,
        device="balance",
        scope="device",
    )

    hrv = HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    assert hrv.fusion_direction == "above"
    assert hrv.corroboration_status == "consistent"
    assert not hrv.corroboration_affects_decision


def test_overnight_vitals_gate_oxygen_and_detect_repeated_odi_elevation():
    raw = _profile(sleep_today=450)
    raw.series.update({
        "spo2_odi": _series(
            "spo2_odi", [3, 7, 7] + [3] * 19,
            device="balance", scope="device", unit="events/h",
        ),
        "spo2_odi_events": _series(
            "spo2_odi_events", [22, 52, 51] + [22] * 19,
            device="balance", scope="device", unit="count",
        ),
        "spo2_measured_minutes": _series(
            "spo2_measured_minutes", [440] * 22,
            device="balance", scope="device", unit="min",
        ),
        "respiratory_rate": _series(
            "respiratory_rate", [14] + [13.5] * 21,
            device="balance", scope="device", unit="breaths/min",
        ),
        "skin_temp_delta": _series(
            "skin_temp_delta", [0.2] + [0] * 21,
            device="balance", scope="device", unit="celsius",
        ),
    })
    baselines = BaselineEngine().build(raw.series, raw.day)
    sleep, _ = SleepAnalyzer().analyze(raw, baselines)

    vitals = OvernightVitalsAnalyzer().analyze(raw, baselines, sleep)

    assert vitals.status == Availability.AVAILABLE
    assert vitals.respiratory_rate == 14
    assert vitals.oxygen.status == Availability.AVAILABLE
    assert vitals.oxygen.coverage_ratio == pytest.approx(440 / 450, abs=0.001)
    assert vitals.oxygen.odi_events_per_hour == 7
    assert vitals.oxygen.odi_events == 52
    assert vitals.oxygen.odi_baseline == 3
    assert vitals.oxygen.repeated_elevation is True
    assert vitals.oxygen.interpretation == "repeated_elevation"
    assert "夜间血氧下降指数连续偏高" in vitals.outlier_labels


def test_hrv_prefers_longer_baseline_over_wearing_position():
    raw = _profile(hrv_today=62)
    raw.device_models = {
        "HELIO": "Amazfit Helio Strap",
        "BALANCE": "Amazfit Balance 2",
    }
    raw.series["hrv_rmssd"] = _series(
        "hrv_rmssd",
        [62] + [50 + (i % 3) for i in range(1, 16)],
        device="helio",
        scope="device",
    ) + _series(
        "hrv_rmssd",
        [58] + [48 + (i % 3) for i in range(1, 24)],
        device="balance",
        scope="device",
    )

    hrv = HrvAnalyzer().analyze(raw, BaselineEngine().build(raw.series, raw.day))

    assert hrv.preferred_device_label == "Amazfit Balance 2"
    assert next(stream for stream in hrv.streams if stream.selected).baseline_distinct_days == 23
