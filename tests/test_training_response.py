from datetime import date, timedelta
from types import SimpleNamespace

from vitalis.intelligence.contracts import (
    Availability,
    ConfidenceBand,
    RecoveryOutcome,
    ResponseMetricObservation,
    TrainingResponse,
    TrainingResponseDay,
    WorkoutExposure,
)
from vitalis.intelligence.personal import PersonalModelEngine
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.training_response import TrainingResponseEngine


TARGET = date(2026, 8, 28)


def _point(metric, day, value, unit, device=None):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=day,
        source="zepp",
        source_scope="device" if device else "normalized_daily_record",
        device_id=device,
    )


def _raw_response(overlap=False):
    workout_day = TARGET - timedelta(days=3)
    raw = RawDailyProfile(user_id="response-user", day=TARGET)
    raw.series = {"hrv_rmssd": [], "resting_hr": [], "sleep_duration": []}
    for offset in range(30, 0, -1):
        day = workout_day - timedelta(days=offset)
        raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, 50, "ms", "helio"))
        raw.series["resting_hr"].append(_point("resting_hr", day, 60, "bpm"))
        raw.series["sleep_duration"].append(_point("sleep_duration", day, 450, "min"))
    for offset, values in {
        1: (45, 64, 400),
        2: (50, 60, 450),
        3: (52, 59, 460),
    }.items():
        day = workout_day + timedelta(days=offset)
        raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, values[0], "ms", "helio"))
        raw.series["resting_hr"].append(_point("resting_hr", day, values[1], "bpm"))
        raw.series["sleep_duration"].append(_point("sleep_duration", day, values[2], "min"))
    raw.workouts = [{
        "workout_id": "primary-workout",
        "local_day": workout_day,
        "data": {
            "type": "running",
            "sport_mode": "outdoor_running",
            "sport_mode_label": "户外跑",
            "training_family": "aerobic",
            "training_family_label": "有氧训练",
            "duration": 45,
            "load": 70,
            "heart_rate_avg": 145,
        },
    }]
    if overlap:
        raw.workouts.append({
            "workout_id": "overlap-workout",
            "local_day": workout_day + timedelta(days=1),
            "data": {
                "type": "strength",
                "sport_mode": "strength_training",
                "sport_mode_label": "力量训练",
                "training_family": "strength",
                "training_family_label": "力量训练",
                "duration": 30,
                "load": 30,
            },
        })
    return raw


def test_training_response_uses_pre_workout_baseline_and_t1_t2_t3_windows():
    responses = TrainingResponseEngine().build(
        "response-run", _raw_response(), [], {"primary-workout": "recommendation-1"}
    )
    response = next(item for item in responses if item.exposure.workout_id == "primary-workout")

    assert response.recommendation_id == "recommendation-1"
    assert response.recovery_status == RecoveryOutcome.RETURNED_TO_BASELINE
    assert response.recovery_hours == 48
    assert response.confidence == ConfidenceBand.HIGH
    t1_hrv = next(
        item for item in response.response_days[0].observations
        if item.metric == "hrv_rmssd"
    )
    assert t1_hrv.device_id == "helio"
    assert t1_hrv.baseline_reference == 50
    assert t1_hrv.direction == "below"


def test_overlapping_workout_marks_response_as_confounded():
    response = next(
        item for item in TrainingResponseEngine().build(
            "response-run", _raw_response(overlap=True), [], {}
        )
        if item.exposure.workout_id == "primary-workout"
    )

    assert response.recovery_status == RecoveryOutcome.CONFOUNDED
    assert response.recovery_hours is None
    assert response.overlapping_workout_ids == ["overlap-workout"]
    assert response.confidence == ConfidenceBand.LOW


def _response(index, hrv_change, recovery_hours):
    return TrainingResponse(
        analysis_run_id="personal-run",
        user_id="personal-user",
        exposure=WorkoutExposure(
            workout_id=f"workout-{index}",
            date=TARGET - timedelta(days=index * 5),
            type="running",
            sport_mode="outdoor_running",
            sport_mode_label="户外跑",
            training_family="aerobic",
            training_family_label="有氧训练",
            duration_minutes=40,
            vendor_load=60,
        ),
        response_days=[TrainingResponseDay(
            day_offset=1,
            date=TARGET - timedelta(days=index * 5 - 1),
            observations=[ResponseMetricObservation(
                metric="hrv_rmssd",
                device_id="helio",
                unit="ms",
                status=Availability.AVAILABLE,
                baseline_reference=50,
                value=50 * (1 + hrv_change / 100),
                deviation_percent=hrv_change,
                direction="below" if hrv_change < -5 else "near",
            )],
        )],
        recovery_status=RecoveryOutcome.RETURNED_TO_BASELINE,
        recovery_status_label="已回到个人基线",
        recovery_hours=recovery_hours,
        confidence=ConfidenceBand.HIGH,
        confidence_label="较高",
    )


def test_personal_model_groups_robust_response_statistics_without_merging_devices():
    responses = [
        _response(1, -10, 48),
        _response(2, -8, 24),
        _response(3, -12, 48),
        _response(4, -6, 24),
    ]
    daily = SimpleNamespace(
        user_id="personal-user",
        date=TARGET,
        baselines={},
        trends=[],
    )

    model = PersonalModelEngine().build("personal-run", daily, responses)
    family = next(
        item for item in model.training_response_patterns
        if item.group_type == "training_family" and item.group_key == "aerobic"
    )
    hrv = next(item for item in family.metrics if item.metric.startswith("hrv_rmssd"))

    assert family.response_count == 4
    assert family.confidence == ConfidenceBand.MODERATE
    assert hrv.device_id == "helio"
    assert hrv.median == -9
    assert hrv.mad == 2
    assert hrv.coverage_ratio == 1
