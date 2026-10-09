from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from vitalis.intelligence.contracts import (
    Availability,
    ConfidenceBand,
    PersonalModel,
    RecoveryOutcome,
    ResponseMetricObservation,
    SubjectiveFeedback,
    TrainingResponse,
    TrainingResponseDay,
    TrainingResponseProfile,
    WorkoutExposure,
)
from vitalis.intelligence.personal import PersonalModelEngine, summarize_training_responses
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.training_response import TrainingResponseEngine


TARGET = date(2026, 8, 28)
AS_OF = datetime(2026, 8, 29, tzinfo=timezone.utc)


def _point(metric, day, value, unit, device=None, **changes):
    return SeriesPoint(
        metric=metric, value=value, unit=unit, day=day, observed_at=changes.pop("observed_at", day),
        source=changes.pop("source", "zepp"),
        source_scope=changes.pop("source_scope", "device" if device else "normalized_daily_record"),
        device_id=device, **changes,
    )


def _raw_response(overlap=False):
    workout_day = TARGET - timedelta(days=3)
    raw = RawDailyProfile(user_id="response-user", day=TARGET, as_of=AS_OF, timezone_name="UTC")
    raw.series = {"hrv_rmssd": [], "resting_hr": [], "sleep_duration": []}
    for offset in range(30, 0, -1):
        day = workout_day - timedelta(days=offset)
        raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, 50, "ms", "helio"))
        raw.series["resting_hr"].append(_point("resting_hr", day, 60, "bpm"))
        raw.series["sleep_duration"].append(_point("sleep_duration", day, 450, "min"))
    for offset, values in {1: (45, 64, 400), 2: (50, 60, 450), 3: (52, 59, 460)}.items():
        day = workout_day + timedelta(days=offset)
        raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, values[0], "ms", "helio"))
        raw.series["resting_hr"].append(_point("resting_hr", day, values[1], "bpm"))
        raw.series["sleep_duration"].append(_point("sleep_duration", day, values[2], "min"))
    raw.workouts = [{
        "workout_id": "primary-workout", "source": "zepp", "local_day": workout_day,
        "data": {
            "type": "running", "sport_mode": "outdoor_running", "sport_mode_label": "户外跑",
            "training_family": "aerobic", "training_family_label": "有氧训练",
            "duration": 45, "load": 70, "heart_rate_avg": 145,
        },
    }]
    if overlap:
        raw.workouts.append({
            "workout_id": "overlap-workout", "source": "zepp",
            "local_day": workout_day + timedelta(days=1),
            "data": {
                "type": "strength", "sport_mode": "strength_training", "sport_mode_label": "力量训练",
                "training_family": "strength", "training_family_label": "力量训练",
                "duration": 30, "load": 30,
            },
        })
    return raw


def _primary(raw, feedback=(), recommendations=None):
    return next(item for item in TrainingResponseEngine().build(
        "response-run", raw, list(feedback), recommendations or {},
    ) if item.exposure.workout_id == "primary-workout")


def _window(response, offset):
    return next(item for item in response.response_days if item.day_offset == offset)


def _set_values(raw, offset, values):
    day = raw.workouts[0]["local_day"] + timedelta(days=offset)
    for metric, value in zip(("hrv_rmssd", "resting_hr", "sleep_duration"), values):
        raw.series[metric] = [replace(point, value=value) if point.day == day else point
                              for point in raw.series[metric]]


def _daily(user_id="personal-user"):
    return SimpleNamespace(user_id=user_id, date=TARGET, baselines={}, trends=[], generated_at=AS_OF)


def _response(index, hrv_change, recovery_hours):
    day = TARGET - timedelta(days=index * 5)
    return TrainingResponse(
        analysis_run_id="personal-run", user_id="personal-user", as_of=AS_OF,
        exposure=WorkoutExposure(
            workout_id=f"workout-{index}", date=day, type="running",
            sport_mode="outdoor_running", sport_mode_label="户外跑",
            training_family="aerobic", training_family_label="有氧训练",
            duration_minutes=40, vendor_load=60,
        ),
        response_days=[TrainingResponseDay(
            day_offset=1, date=day + timedelta(days=1), status="available",
            observed=1, expected=1, comparable_count=1, coverage=1, as_of=AS_OF,
            observations=[ResponseMetricObservation(
                metric="hrv_rmssd", source="zepp", source_scope="device", device_id="helio",
                unit="ms", status=Availability.AVAILABLE, baseline_reference=50,
                value=50 * (1 + hrv_change / 100), deviation_percent=hrv_change,
                direction="below" if hrv_change < -5 else "near",
                observed=1, expected=1, coverage=1, confidence=ConfidenceBand.HIGH,
                observed_at=day + timedelta(days=1), as_of=AS_OF,
            )],
        )],
        recovery_status=RecoveryOutcome.RETURNED_TO_BASELINE, recovery_status_label="已回到个人基线",
        recovery_hours=recovery_hours, initial_recovery_hours=recovery_hours,
        initial_recovery_offset=recovery_hours // 24,
        confidence=ConfidenceBand.HIGH, confidence_label="较高",
    )


def test_training_response_uses_pre_workout_baseline_and_t1_t2_t3_windows():
    response = _primary(_raw_response(), recommendations={"primary-workout": "recommendation-1"})
    assert response.recommendation_id == "recommendation-1"
    assert response.recovery_status == RecoveryOutcome.RETURNED_TO_BASELINE
    assert response.recovery_hours == 48
    assert response.confidence == ConfidenceBand.HIGH
    hrv = next(item for item in _window(response, 1).observations if item.metric == "hrv_rmssd")
    assert hrv.device_id == "helio"
    assert hrv.baseline_reference == 50
    assert hrv.direction == "below"


def test_training_response_preserves_missing_and_observed_zero_loads():
    missing_raw = _raw_response()
    missing_raw.workouts[0]["data"].pop("load")
    zero_raw = _raw_response()
    zero_raw.workouts[0]["data"]["load"] = 0
    missing, zero = _primary(missing_raw), _primary(zero_raw)
    assert missing.exposure.duration_minutes == 45
    assert missing.exposure.vendor_load is None
    assert zero.exposure.duration_minutes == 45
    assert zero.exposure.vendor_load == 0


def test_overlapping_workout_marks_response_as_confounded():
    response = _primary(_raw_response(overlap=True))
    assert response.recovery_status == RecoveryOutcome.CONFOUNDED
    assert response.recovery_hours is None
    assert response.overlapping_workout_ids == ["zepp:overlap-workout"]
    assert response.confidence == ConfidenceBand.LOW


def test_personal_model_groups_robust_response_statistics_without_merging_devices():
    responses = [_response(1, -10, 48), _response(2, -8, 24),
                 _response(3, -12, 48), _response(4, -6, 24)]
    model = PersonalModelEngine().build("personal-run", _daily(), responses, [])
    family = next(item for item in model.training_response_patterns if item.group_type == "training_family")
    hrv = next(item for item in family.metrics if item.metric == "hrv_rmssd_t1_deviation_percent")
    assert family.response_count == 4
    assert family.confidence == ConfidenceBand.MODERATE
    assert hrv.device_id == "helio"
    assert hrv.median == -9
    assert hrv.mad == 2
    assert hrv.coverage_ratio == 1


def test_windows_and_per_metric_evidence_keep_sources_units_times_and_denominators():
    raw = _raw_response()
    response = _primary(raw)
    assert [item.day_offset for item in response.response_days] == [0, 1, 2, 3]
    t0, t1 = _window(response, 0), _window(response, 1)
    assert t0.status == "missing"  # A dose is not a physiological T+0 observation.
    assert t0.dose_quality.coverage == 1
    assert t1.status == "available"
    assert t1.observed == t1.expected == t1.comparable_count == 3
    observation = next(item for item in t1.observations if item.metric == "hrv_rmssd")
    assert (observation.source, observation.source_scope, observation.device_id) == ("zepp", "device", "helio")
    assert observation.unit == "ms"
    assert observation.as_of == raw.as_of
    assert observation.observed_at == raw.workouts[0]["local_day"] + timedelta(days=1)
    assert observation.fetched_at is None
    assert observation.value == 45
    assert observation.baseline_reference == 50
    assert observation.deviation_percent == -10
    assert observation.coverage == observation.observed == observation.expected == 1
    assert observation.baseline_observed_days == observation.baseline_expected_days == 28
    assert observation.confidence == ConfidenceBand.HIGH
    assert response.association_only is True


def test_mature_missing_partial_and_future_windows_have_distinct_denominators():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    raw.day = day + timedelta(days=2)
    raw.series = {metric: [point for point in points if point.day != day + timedelta(days=1)]
                  for metric, points in raw.series.items()}
    raw.series["hrv_rmssd"] = [point for point in raw.series["hrv_rmssd"]
                               if point.day != day + timedelta(days=2)]
    response = _primary(raw)
    t1, t2, t3 = (_window(response, offset) for offset in (1, 2, 3))
    assert t1.status == "missing" and t1.observed == 0 and t1.expected == 3
    assert t2.status == "partial" and t2.observed == 2 and t2.expected == 3
    assert t3.status == "not_due" and t3.observed == t3.expected == 0
    assert t3.coverage is None
    assert response.expected == 6 and response.observed == 2


def test_current_incomplete_local_day_is_not_due_even_when_it_contains_measurements():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"] + timedelta(days=1)
    raw.day, raw.as_of = day, datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=12)
    response = _primary(raw)
    assert _window(response, 1).status == "not_due"
    assert _window(response, 1).expected == 0
    assert response.expected == 0


def test_today_workout_is_included_but_t0_is_not_a_completed_day():
    raw = _raw_response()
    raw.day = raw.workouts[0]["local_day"]
    raw.as_of = datetime.combine(raw.day, datetime.min.time(), timezone.utc) + timedelta(hours=20)
    response = _primary(raw)
    assert all(item.status == "not_due" for item in response.response_days)
    assert response.exposure.duration_minutes == 45
    assert response.expected == response.observed == 0
    assert response.recovery_hours is None


def test_t0_physiology_requires_an_observation_after_the_recorded_workout_end():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    start = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=10)
    raw.workouts[0].update(started_at=start, ended_at=start + timedelta(minutes=45))
    for hour, value in ((9, 80), (12, 48)):
        raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, value, "ms", "helio",
                                            observed_at=start.replace(hour=hour)))
    raw.series["sleep_duration"].append(_point("sleep_duration", day, 450, "min"))
    response = _primary(raw)
    t0 = _window(response, 0)
    hrv = next(item for item in t0.observations if item.metric == "hrv_rmssd")
    assert hrv.value == 48
    sleep = next(item for item in t0.observations if item.metric == "sleep_duration")
    assert sleep.value is None and sleep.observed == 0
    assert t0.status == "partial"
    assert response.initial_recovery_offset == 2  # T+0 never establishes recovery.


def test_as_of_and_qualified_units_guard_response_and_baseline_inputs():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"] + timedelta(days=1)
    raw.series["hrv_rmssd"].extend([
        _point("hrv_rmssd", day, 1000, "ms", "helio", observed_at=AS_OF + timedelta(hours=1)),
        _point("hrv_rmssd", day, float("nan"), "ms", "helio"),
        _point("hrv_rmssd", day, -3, "ms", "helio"),
        _point("hrv_rmssd", day, 0.2, "seconds", "helio"),
    ])
    hrv = next(item for item in _window(_primary(raw), 1).observations
               if item.metric == "hrv_rmssd" and item.unit == "ms")
    assert hrv.value == 45 and hrv.sample_count == 1
    assert hrv.baseline_reference == 50


def test_new_stream_without_baseline_keeps_value_but_does_not_invent_deviation():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"] + timedelta(days=1)
    raw.series["hrv_rmssd"].append(_point("hrv_rmssd", day, 90, "ms", "new-device"))
    t1 = _window(_primary(raw), 1)
    observation = next(item for item in t1.observations if item.device_id == "new-device")
    assert observation.value == 90 and observation.observed == 1
    assert observation.baseline_reference is None and observation.deviation_percent is None
    assert observation.status == Availability.INSUFFICIENT_DATA
    assert observation.confidence == ConfidenceBand.NONE
    assert t1.status == "partial"


def test_missing_baselines_never_hide_current_observations_or_mark_them_available():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    raw.series = {metric: [point for point in points if point.day > day]
                  for metric, points in raw.series.items()}
    response = _primary(raw)
    assert _window(response, 1).status == "partial"
    assert all(item.baseline_reference is None and item.deviation_percent is None
               for item in _window(response, 1).observations)
    assert response.recovery_status == RecoveryOutcome.INSUFFICIENT_DATA
    assert response.confidence == ConfidenceBand.NONE


def test_empty_physiology_has_missing_windows_with_explicit_expected_core_signals():
    raw = _raw_response()
    raw.series = {}
    response = _primary(raw)
    assert _window(response, 1).status == "missing"
    assert _window(response, 1).expected == 3
    assert all(item.value is None for item in _window(response, 1).observations)


def test_initial_return_and_sustained_return_are_independent():
    raw = _raw_response()
    _set_values(raw, 1, (50, 60, 450))
    _set_values(raw, 2, (42, 64, 450))
    response = _primary(raw)
    assert response.initial_recovery_offset == 1 and response.initial_recovery_hours == 24
    assert response.recovery_status == RecoveryOutcome.RETURNED_TO_BASELINE
    assert response.sustained_recovery_offset is None
    assert response.sustained_recovery_status == "not_sustained"


def test_recovered_t2_and_t3_confirm_sustained_recovery_through_t3():
    response = _primary(_raw_response())
    assert response.initial_recovery_offset == response.sustained_recovery_offset == 2
    assert response.sustained_recovery_hours == 48
    assert response.sustained_through_offset == 3
    assert response.sustained_recovery_status == "sustained"
    assert response.recovery_time_basis == "calendar_day_offset"


def test_missing_middle_window_does_not_count_as_consecutive_recovery():
    raw = _raw_response()
    _set_values(raw, 1, (50, 60, 450))
    middle = raw.workouts[0]["local_day"] + timedelta(days=2)
    raw.series = {metric: [point for point in points if point.day != middle]
                  for metric, points in raw.series.items()}
    response = _primary(raw)
    assert response.initial_recovery_offset == 1
    assert response.sustained_recovery_offset is None
    assert response.sustained_recovery_status == "insufficient_data"


def test_first_return_without_mature_followup_is_not_sustained():
    raw = _raw_response()
    raw.day = raw.workouts[0]["local_day"] + timedelta(days=2)
    response = _primary(raw)
    assert response.initial_recovery_offset == 2
    assert response.sustained_recovery_status == "not_due"
    assert response.sustained_recovery_hours is None


def test_recovery_does_not_cherry_pick_a_favorable_device():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    raw.series["hrv_rmssd"].extend(
        _point("hrv_rmssd", point.day, 30 if point.day > day else 50, "ms", "other-device")
        for point in list(raw.series["hrv_rmssd"])
    )
    response = _primary(raw)
    assert response.initial_recovery_offset is None
    assert response.sustained_recovery_status == "not_sustained"


def test_same_day_and_today_training_are_included_in_overlap_checks_and_carried_forward():
    raw = _raw_response()
    primary = raw.workouts[0]
    raw.workouts.extend([
        {"workout_id": "same-day", "source": "other-provider", "local_day": primary["local_day"], "data": {}},
        {"workout_id": "today", "source": "zepp", "local_day": TARGET, "data": {}},
    ])
    response = _primary(raw)
    assert set(response.overlapping_workout_ids) == {"other-provider:same-day", "zepp:today"}
    assert "other-provider:same-day" in _window(response, 2).overlapping_workout_ids
    assert _window(response, 3).status == "confounded"


def test_known_elevated_activity_is_a_sourced_confounder_not_a_causal_statement():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    raw.series["steps"] = [_point("steps", day - timedelta(days=offset), 4000, "steps")
                           for offset in range(1, 29)]
    raw.series["steps"].append(_point("steps", day + timedelta(days=1), 16000, "steps"))
    response = _primary(raw)
    assert _window(response, 1).status == "confounded"
    assert any("steps" in reason and "zepp" in reason for reason in response.confounding_reasons)
    assert response.sustained_recovery_status == "confounded"
    assert response.association_only is True


@pytest.mark.parametrize("value", [None, -1, True, float("nan"), 4.2])
def test_invalid_or_missing_duration_is_never_coerced_to_zero(value):
    raw = _raw_response()
    raw.workouts[0]["data"]["duration"] = value
    response = _primary(raw)
    assert response.exposure.duration_minutes is None
    assert "duration_minutes" not in response.exposure.dose_quality.observed_fields


def test_duration_zero_requires_evidence_and_unknown_workout_family_is_not_inferred():
    raw = _raw_response()
    raw.workouts[0]["data"] = {"duration": 0}
    unknown = _primary(raw)
    assert unknown.exposure.duration_minutes is None
    assert unknown.exposure.training_family == "unknown"
    raw.workouts[0]["data"]["observed_fields"] = ["duration"]
    assert _primary(raw).exposure.duration_minutes == 0


def test_strength_dose_preserves_actual_repetitions_and_does_not_infer_from_hr_bouts():
    raw = _raw_response()
    raw.workouts[0]["data"].update(type="strength", training_family="strength")
    raw.workouts[0]["detail"] = {"strength_sets": [
        {"source": "strength_sets", "order": index, "repetitions": reps}
        for index, reps in enumerate((12, 10, 8), 1)
    ]}
    exposure = _primary(raw).exposure
    assert exposure.sets == 3
    assert exposure.repetitions_total == 30
    assert exposure.repetitions_by_set == [12, 10, 8]
    assert next(item for item in exposure.dose_quality.observations
                if item.metric == "repetitions_total").unit == "repetitions"
    raw.workouts[0]["detail"] = {"strength_sets": []}
    raw.workouts[0]["data"]["estimated_work_bouts"] = 8
    assert _primary(raw).exposure.sets is None


def test_strength_partial_repetitions_and_summary_sets_are_distinct_evidence():
    raw = _raw_response()
    raw.workouts[0]["data"].update(type="strength", training_family="strength", vendor_reported_sets=4)
    raw.workouts[0]["detail"] = {"strength_sets": [
        {"source": "strength_sets", "order": 1, "repetitions": 12},
        {"source": "strength_sets", "order": 2},
    ]}
    exposure = _primary(raw).exposure
    assert exposure.vendor_reported_sets == 4
    assert exposure.repetitions_by_set == [12, None]
    assert exposure.repetitions_total is None
    reps = next(item for item in exposure.dose_quality.observations if item.metric == "repetitions_total")
    assert reps.status == "partial" and reps.observed == 1 and reps.expected == 4
    assert exposure.dose_quality.status == "partial"


def test_feedback_keeps_explicit_workout_source_user_date_and_creation_cutoff():
    raw = _raw_response()
    day = raw.workouts[0]["local_day"]
    good = SubjectiveFeedback(
        id="good", user_id=raw.user_id, date=day + timedelta(days=1), workout_source="zepp",
        workout_id="primary-workout", physical_fatigue=3, created_at=AS_OF - timedelta(hours=1),
    )
    items = [good, good.model_copy(update={"id": "foreign-user", "user_id": "another"}),
             good.model_copy(update={"id": "foreign-source", "workout_source": "another"}),
             good.model_copy(update={"id": "future-write", "created_at": AS_OF + timedelta(hours=1)}),
             good.model_copy(update={"id": "before-workout", "date": day - timedelta(days=1)})]
    response = _primary(raw, items)
    assert [item.id for item in response.feedback] == ["good"]
    assert [item.id for item in _window(response, 1).feedback] == ["good"]


def test_duplicate_workout_ids_from_different_sources_do_not_cross_link_recommendations():
    raw = _raw_response()
    raw.workouts.append({**raw.workouts[0], "source": "other"})
    responses = TrainingResponseEngine().build("sources", raw, [], {"primary-workout": "ambiguous"})
    assert all(item.recommendation_id is None for item in responses)
    assert len(responses) == 2


def test_personal_model_aggregates_t0_t1_t2_t3_independently():
    response = _response(1, -10, 48)
    first = response.response_days[0]
    response.response_days = [first.model_copy(update={
        "day_offset": offset, "date": response.exposure.date + timedelta(days=offset),
        "observations": [first.observations[0].model_copy(update={"deviation_percent": -float(offset)})],
    }) for offset in (0, 1, 2, 3)]
    model = PersonalModelEngine().build("windows", _daily(), [response], [])
    family = next(item for item in model.training_response_patterns if item.group_type == "training_family")
    metrics = [item for item in family.metrics if item.metric.endswith("deviation_percent")]
    assert {item.day_offset for item in metrics} == {0, 1, 2, 3}
    assert {item.median for item in metrics} == {0, -1, -2, -3}
    assert {item.day_offset for item in model.response_window_summaries} == {0, 1, 2, 3}


def test_personal_model_keeps_sources_scopes_devices_and_units_separate():
    responses = [_response(index, -10 * index, 48) for index in range(1, 5)]
    for response, changes in zip(responses[1:], (
        {"source": "other"}, {"source_scope": "user_fused"}, {"unit": "another-unit"},
    )):
        response.response_days[0].observations[0] = response.response_days[0].observations[0].model_copy(update=changes)
    model = PersonalModelEngine().build("streams", _daily(), responses, [])
    family = next(item for item in model.training_response_patterns if item.group_type == "training_family")
    metrics = [item for item in family.metrics if item.metric == "hrv_rmssd_t1_deviation_percent"]
    assert len(metrics) == 4
    assert all(item.sample_count == item.eligible_count == 1 for item in metrics)
    assert len({(item.source, item.source_scope, item.device_id, item.input_unit) for item in metrics}) == 4


def test_personal_model_excludes_confounded_values_and_excludes_not_due_from_denominator():
    responses = [_response(index, -10 * index, 48) for index in range(1, 5)]
    responses[1].response_days[0] = responses[1].response_days[0].model_copy(update={"status": "confounded"})
    responses[2].response_days[0] = responses[2].response_days[0].model_copy(update={
        "status": "missing", "observed": 0, "comparable_count": 0,
        "observations": [responses[2].response_days[0].observations[0].model_copy(update={
            "status": Availability.INSUFFICIENT_DATA, "value": None,
            "deviation_percent": None, "observed": 0,
        })],
    })
    responses[3].response_days[0] = responses[3].response_days[0].model_copy(update={
        "status": "not_due", "observed": 0, "expected": 0, "comparable_count": 0,
        "observations": [responses[3].response_days[0].observations[0].model_copy(update={"expected": 0, "observed": 0})],
    })
    model = PersonalModelEngine().build("eligibility", _daily(), responses, [])
    family = next(item for item in model.training_response_patterns if item.group_type == "training_family")
    hrv = next(item for item in family.metrics if item.metric == "hrv_rmssd_t1_deviation_percent")
    assert hrv.sample_count == 1 and hrv.eligible_count == 3
    assert hrv.confounded_count == 1 and hrv.not_due_count == 1
    assert hrv.coverage_ratio == pytest.approx(1 / 3, abs=0.0001)
    assert hrv.median == -10
    assert family.confounded_response_count == 1


def test_personal_feedback_scales_have_independent_denominators_and_distributions():
    responses = [_response(index, -5, 48) for index in (1, 2)]
    for index, response in enumerate(responses, 1):
        response.feedback = [SubjectiveFeedback(
            id=f"feedback-{index}", user_id="personal-user", date=response.exposure.date,
            workout_source="zepp", workout_id=response.exposure.workout_id,
            session_rpe=5 + index, physical_fatigue=index,
            mental_state=5 - index, muscle_soreness=3 if index == 1 else None,
            created_at=datetime.combine(response.exposure.date, datetime.min.time(), timezone.utc),
        )]
    model = PersonalModelEngine().build("feedback", _daily(), responses, [])
    distributions = {item.metric: item for item in model.subjective_feedback_distributions}
    assert set(distributions) == {"session_rpe", "physical_fatigue", "mental_state", "muscle_soreness"}
    assert distributions["session_rpe"].median == 6.5
    assert distributions["physical_fatigue"].median == 1.5
    assert distributions["mental_state"].median == 3.5
    assert distributions["muscle_soreness"].median == 3
    assert distributions["muscle_soreness"].sample_count == 1
    assert distributions["session_rpe"].distribution == {"6": 1, "7": 1}
    assert distributions["session_rpe"].unit == "score_1_10"
    assert distributions["mental_state"].unit == "score_1_5"


def test_subjective_missing_fields_stay_unknown_and_unlinked_day_feedback_is_retained():
    feedback = SubjectiveFeedback(
        id="day-feedback", user_id="personal-user", date=TARGET, physical_fatigue=4,
        notes="合成备注，不从文本推断其他分数", created_at=AS_OF - timedelta(hours=1),
    )
    model = PersonalModelEngine().build("day-feedback", _daily(), [], [], feedback=[feedback])
    distributions = {item.metric: item for item in model.subjective_feedback_distributions}
    assert distributions["physical_fatigue"].median == 4
    assert distributions["session_rpe"].median is None
    assert distributions["mental_state"].median is None
    assert distributions["muscle_soreness"].median is None
    assert distributions["mental_state"].sample_count == 0


def test_feedback_and_responses_are_deduplicated_without_overweighting_a_session():
    response = _response(1, -10, 48)
    feedback = SubjectiveFeedback(
        id="deduplicated", user_id="personal-user", date=response.exposure.date,
        workout_source="zepp", workout_id=response.exposure.workout_id, session_rpe=6,
        created_at=AS_OF - timedelta(hours=1),
    )
    response.feedback = [feedback, feedback]
    model = PersonalModelEngine().build("duplicates", _daily(), [response, response], [], feedback=[feedback])
    assert all(item.response_count == 1 for item in model.training_response_patterns)
    rpe = next(item for item in model.subjective_feedback_distributions if item.metric == "session_rpe")
    assert rpe.sample_count == rpe.eligible_count == 1


def test_personal_model_keeps_first_and_sustained_recovery_distributions_separate():
    response = _response(1, -5, 24)
    response.sustained_recovery_status = "sustained"
    response.sustained_recovery_hours = 48
    model = PersonalModelEngine().build("recoveries", _daily(), [response], [])
    family = next(item for item in model.training_response_patterns if item.group_type == "training_family")
    metrics = {item.metric: item for item in family.metrics}
    assert metrics["initial_recovery_hours"].median == 24
    assert metrics["sustained_recovery_hours"].median == 48


def test_domain_schema_versions_are_literals_and_roundtrip_preserves_date_precision():
    response = _primary(_raw_response())
    profile = TrainingResponseProfile(
        analysis_run_id="versions", user_id="response-user", date=TARGET,
        generated_at=AS_OF, responses=[response],
    )
    assert profile.schema_version == "2.0"
    decoded = TrainingResponseProfile.model_validate_json(profile.model_dump_json())
    assert isinstance(_window(decoded.responses[0], 1).observations[0].observed_at, date)
    assert not isinstance(_window(decoded.responses[0], 1).observations[0].observed_at, datetime)
    model = PersonalModelEngine().build("versions", _daily("response-user"), [response], [])
    assert model.schema_version == "3.0"
    with pytest.raises(ValidationError):
        PersonalModel.model_validate({**model.model_dump(), "schema_version": "2.0"})
    with pytest.raises(ValidationError):
        TrainingResponseDay(day_offset=1, date=TARGET, status="not_due", expected=1)


def test_public_response_summary_is_period_scoped_and_json_ready():
    first = _response(1, -10, 24)
    first.sustained_recovery_status = "sustained"
    second = _response(2, -5, 48)
    second.confounding_reasons = ["overlapping_training:zepp:other"]
    feedback = [SubjectiveFeedback(
        id="period-summary-feedback", user_id="personal-user", date=first.exposure.date,
        workout_source="zepp", workout_id=first.exposure.workout_id, session_rpe=7,
        created_at=AS_OF - timedelta(hours=1),
    )]

    summary = summarize_training_responses([first, second], feedback, as_of=AS_OF)

    assert summary["response_count"] == 2
    assert summary["observed"] == summary["expected"] == 2
    assert summary["coverage"] == 1
    assert summary["initial_recovery_returned_count"] == 2
    assert summary["sustained_recovery_count"] == 1
    assert summary["confounded_response_count"] == 1
    assert summary["as_of"] == AS_OF.isoformat()
    assert summary["window_summaries"][0]["day_offset"] == 0
    assert next(item for item in summary["feedback_distributions"] if item["metric"] == "session_rpe")["median"] == 7
