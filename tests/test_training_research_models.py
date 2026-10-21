"""Synthetic evidence for independent volume, muscle dose and running ability."""

from datetime import date, datetime, timedelta, timezone

import pytest

from vitalis.adapters.zepp.catalog import LAP_62_NAMESPACE, resolve_exercise
from vitalis.domain import WorkoutMetricSample
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.strength import StrengthAnalyzer, classify_exercise, strength_session_capacity


DAY = date(2026, 10, 20)


def test_official_catalog_retains_primary_and_secondary_body_regions():
    squat = resolve_exercise(LAP_62_NAMESPACE, 3)
    assert squat.primary_regions == (14, 15)
    assert squat.secondary_regions == (26, 27)
    assert resolve_exercise(LAP_62_NAMESPACE, 999999).primary_regions == ()


def test_exercise_identity_does_not_use_broad_substrings():
    assert classify_exercise(None, "俯卧弹力绳腿弯举")[0] in {"isolation", "unknown"}
    assert "biceps" not in classify_exercise(None, "俯卧弹力绳腿弯举")[1]
    assert classify_exercise(None, "未登记的卧推变式") == ("unknown", ())
    assert classify_exercise(None, "弯举") == ("unknown", ())
    assert classify_exercise(None, "下压") == ("unknown", ())
    assert classify_exercise('barbell_hip_thrust', None)[1] != classify_exercise('barbell_deadlift', None)[1]


def test_volume_does_not_require_a_muscle_mapping_or_count_assistance():
    summary = strength_session_capacity({"observed_sets": [
        {"source": "lap_62", "order": 1, "repetitions": 8,
         "weight_value": 40, "weight_unit": "kg", "weight_basis": "total"},
        {"source": "lap_62", "order": 2, "repetitions": 8,
         "weight_value": 30, "weight_unit": "kg", "weight_basis": "assistance"},
    ]})
    assert summary["volume_by_basis"] == {"total": 320.0}
    assert summary["coverage"]["recorded_sets"] == 2
    assert summary["coverage"]["volume_sets"] == 1
    assert summary["muscle_doses"] == []


def test_catalog_reference_dose_does_not_invent_working_sets_or_confirmation():
    raw = RawDailyProfile(user_id="synthetic", day=DAY, workouts=[{
        "source": "zepp", "workout_id": "synthetic-push", "local_day": DAY,
        "data": {"type": "strength", "training_family": "strength", "duration": 20},
        "samples": [], "detail": {"strength_sets": [
            {"source": "lap_62", "order": n, "vendor_exercise_code": 6,
             "exercise_name": "卧推", "repetitions": 8}
            for n in range(1, 4)
        ]},
    }])
    analysis = StrengthAnalyzer().analyze(raw)
    session = analysis.recent_sessions[0]
    assert session.explicit_exercises == []
    assert session.dose_coverage.recorded_sets == 3
    assert session.dose_coverage.volume_sets == 0
    assert session.dose_coverage.working_sets == 0
    chest = next(x for x in session.reference_muscle_doses if x.muscle_group == "chest")
    assert chest.direct_sets == 3
    assert chest.qualification == "vendor_reference"
    assert analysis.explicit_session_coverage == 0


def test_one_rep_max_abstains_for_unknown_effort_and_high_repetitions():
    from vitalis.intelligence.strength import estimate_one_rep_max

    assert estimate_one_rep_max(40, 25, unit="kg", basis="total", rir=0) is None
    assert estimate_one_rep_max(40, 8, unit="kg", basis="total", rir=None) is None
    assert estimate_one_rep_max(40, 8, unit="kg", basis="total", rir=0) == pytest.approx(50.7)


def test_critical_speed_uses_independent_sessions_and_rejects_pauses():
    from vitalis.intelligence.running_performance import fit_critical_speed

    points = [
        {"seconds": t, "distance_m": 3 * t + 150, "workout_id": str(t), "source": "mock",
         "day": DAY, "qualification": "observed"}
        for t in (180, 420, 720)
    ]
    result = fit_critical_speed(points, as_of_day=DAY)
    assert result.critical_speed_mps == pytest.approx(3, abs=0.001)
    assert result.distance_capacity_m == pytest.approx(150, abs=0.1)
    assert result.shadow_only is True
    assert result.sample_sessions == 3
    assert fit_critical_speed([dict(p, workout_id="same") for p in points], as_of_day=DAY).critical_speed_mps is None
    assert fit_critical_speed([dict(p, paused=True) for p in points], as_of_day=DAY).critical_speed_mps is None


def test_running_performance_preserves_cohorts_and_does_not_score_rest():
    from vitalis.intelligence.running_performance import analyze_running_performance

    raw = RawDailyProfile(user_id="synthetic", day=DAY, workouts=[])
    result = analyze_running_performance(raw)
    assert result.composite_index is None
    assert result.validation_state == "unvalidated"
    assert result.cohorts == []


def test_running_cohort_keeps_observation_and_fetch_provenance_without_filling_gaps():
    from vitalis.intelligence.running_performance import analyze_running_performance

    run = _stable_run(DAY, 'provenance')
    fetched = datetime(2026, 10, 21, 1, tzinfo=timezone.utc)
    run['fetched_at'] = fetched
    raw = RawDailyProfile(user_id='synthetic', day=DAY, workouts=[run])
    cohort = analyze_running_performance(raw).cohorts[0]
    assert cohort.source == 'mock'
    assert cohort.source_scope == 'workout_detail'
    assert cohort.time_basis == 'activity_day'
    assert cohort.observed_at == run['started_at']
    assert cohort.fetched_at == fetched
    del run['fetched_at']
    cohort = analyze_running_performance(raw).cohorts[0]
    assert cohort.fetched_at is None
    assert any('获取时间未知' in item for item in cohort.limitations)


def test_canonical_crosswalk_has_exact_knowledge_and_does_not_promote_watch_confirmation():
    from vitalis.adapters.zepp.catalog import load_catalog
    from vitalis.domain.strength_knowledge import CANONICAL_FAMILIES

    canonical = [x for x in load_catalog().entries.values() if x.canonical_exercise_id]
    assert len(canonical) == 36
    assert all(x.canonical_exercise_id in CANONICAL_FAMILIES for x in canonical)
    assert all(classify_exercise(x.canonical_exercise_id, None)[0] != 'unknown' for x in canonical)


def test_composite_has_fixed_weights_and_caps_negative_drift():
    from vitalis.intelligence.running_performance import composite_candidate

    base = {'speed': 3, 'efficiency': 2.8, 'durability': 1}
    assert composite_candidate(base, base) == 100
    assert composite_candidate({**base, 'durability': 1.3}, base) == 100
    assert composite_candidate({**base, 'efficiency': None}, base) is None
    assert composite_candidate({**base, 'speed': 0}, base) is None
    assert composite_candidate({**base, 'speed': float('nan')}, base) is None


def _stable_run(day, workout_id, *, device='watch-a', hr=140, speed=3, mode='outdoor_running'):
    started = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    samples = [WorkoutMetricSample(workout_id=workout_id, timestamp=started + timedelta(seconds=t),
               metric=m, value=v, unit=u, device_id=device)
               for t in range(0, 3001, 10) for m, v, u in [('speed', speed, 'm/s'), ('heart_rate', hr, 'bpm')]]
    return {'source': 'mock', 'workout_id': workout_id, 'local_day': day, 'started_at': started,
            'samples': samples, 'data': {'type': 'running', 'sport_mode': mode, 'duration': 50}, 'detail': {}}


def test_fixed_heart_rate_protocol_survives_missing_current_coverage_and_device_changes():
    from vitalis.intelligence.running_performance import analyze_running_performance

    runs = [_stable_run(DAY-timedelta(days=15-i*3), str(i)) for i in range(6)]
    raw = RawDailyProfile(user_id='synthetic', day=DAY, workouts=runs)
    first = analyze_running_performance(raw).cohorts[0]
    assert first.reference_heart_rate_bpm == 140
    assert first.efficiency_speed_mps == 3
    raw.running_model_baselines = {first.cohort_id: first.model_dump()}
    raw.workouts = [_stable_run(DAY-timedelta(days=15-i*3), str(i), hr=150) for i in range(6)]
    second = analyze_running_performance(raw).cohorts[0]
    assert second.reference_heart_rate_bpm == 140
    assert second.baseline_version == first.baseline_version
    assert second.efficiency_speed_mps is None
    raw.workouts.append(_stable_run(DAY, 'other-device', device='watch-b'))
    assert len(analyze_running_performance(raw).cohorts) == 2


def test_pause_gaps_and_future_observations_do_not_form_ability_evidence():
    from vitalis.intelligence.running_performance import analyze_running_performance

    runs = [_stable_run(DAY-timedelta(days=i), str(i)) for i in range(3)]
    for run in runs:
        run['detail']['pauses'] = [{'duration_seconds': 60}]
    raw = RawDailyProfile(user_id='synthetic', day=DAY, workouts=runs)
    cohort = analyze_running_performance(raw).cohorts[0]
    assert cohort.speed.critical_speed_mps is None
    assert cohort.durability_sessions == 0
    raw.workouts = [_stable_run(DAY+timedelta(days=1), 'future')]
    assert analyze_running_performance(raw).cohorts == []
    raw.running_model_samples_complete = False
    assert analyze_running_performance(raw).cohorts == []


def test_warmups_duplicate_regions_and_bodyweight_have_independent_coverage():
    summary = strength_session_capacity({'observed_sets': [
        {'source': 'lap_62', 'order': 1, 'vendor_exercise_code': 6, 'repetitions': 10, 'set_kind': 'warmup'},
        {'source': 'lap_62', 'order': 2, 'vendor_exercise_code': 6, 'repetitions': 8, 'set_kind': 'working'},
        {'source': 'lap_62', 'order': 2, 'vendor_exercise_code': 6, 'repetitions': 8, 'set_kind': 'working'},
    ]})
    assert summary['coverage']['recorded_sets'] == 2
    assert summary['coverage']['working_sets'] == 1
    chest = next(x for x in summary['reference_muscle_doses'] if x['muscle_group'] == 'chest')
    assert chest['direct_sets'] == 1
    assert chest['working_sets_known'] is True


def test_single_side_dose_does_not_become_six_sets_per_arm():
    summary = strength_session_capacity({'observed_sets': [
        {'source': 'strength_sets', 'order': i, 'exercise_name': '二头肌弯举', 'side': side, 'set_kind': 'working'}
        for i, side in enumerate(['left']*3 + ['right']*3, start=1)
    ]})
    assert {(d['side'], d['direct_sets']) for d in summary['muscle_doses']} == {('left', 3), ('right', 3)}


def test_known_zero_external_load_is_not_missing_or_bodyweight():
    summary = strength_session_capacity({'observed_sets': [{
        'source': 'strength_sets', 'order': 1, 'weight_value': 0, 'weight_unit': 'kg',
        'weight_basis': 'total', 'repetitions': 10}]})
    assert summary['volume_by_basis'] == {'total': 0}
    assert summary['coverage']['volume_sets'] == 1


def test_unknown_week_does_not_mark_unseen_muscles_as_untrained():
    from vitalis.intelligence.strength_recommendations import recommend_focus

    raw = RawDailyProfile(user_id='synthetic', day=DAY, workouts=[{
        'source': 'mock', 'workout_id': 'push', 'local_day': DAY,
        'data': {'type': 'strength', 'training_family': 'strength', 'duration': 20},
        'detail': {'strength_sets': [{'source': 'lap_62', 'order': 1, 'vendor_exercise_code': 6}]},
    }])
    analysis = StrengthAnalyzer().analyze(raw)
    assert analysis.next_focus is None
    raw.training_history_coverage = {'verified_days': [(DAY-timedelta(days=i)).isoformat() for i in range(7)]}
    # Verified activity days do not establish complete per-set detail.
    assert StrengthAnalyzer().analyze(raw).next_focus is None
    raw.workouts[0]['data']['vendor_reported_sets'] = 1
    analysis = StrengthAnalyzer().analyze(raw)
    first, _, reasons, _ = recommend_focus(raw, analysis.recent_sessions)
    assert first in {'PULL', 'LEGS'}
    assert any('完整核实' in reason for reason in reasons)


def test_identical_doses_separated_by_another_exercise_keep_their_order():
    from vitalis.intelligence.contracts import StrengthExerciseInput
    from vitalis.intelligence.strength import normalize_exercise

    records = [normalize_exercise('synthetic', 'ordered', i, StrengthExerciseInput(
        exercise_name=name, sets=1, repetitions=8, weight_kg=40, weight_basis='total'))
        for i, name in enumerate(['卧推', '划船', '卧推'], start=1)]
    raw = RawDailyProfile(user_id='synthetic', day=DAY, workouts=[{
        'source':'mock', 'workout_id':'ordered', 'local_day':DAY, 'confirmed_exercises':records,
        'data':{'type':'strength','training_family':'strength','duration':30}, 'detail':{},
    }])
    result = StrengthAnalyzer().analyze(raw).recent_sessions[0]
    assert [x.exercise_name for x in result.explicit_exercises] == ['卧推', '划船', '卧推']
    assert result.dose_coverage.recorded_sets == 3
    assert sum(x.volume_value for x in result.explicit_exercises) == result.capacity_volume_kg == 960


def test_validation_excludes_leaked_and_duplicate_outcomes():
    from vitalis.intelligence.running_validation import validate_race_predictions

    rows = [{'subject': 'synthetic', 'outcome_id': str(i), 'feature_date': '2026-09-01',
             'outcome_date': '2026-10-01', 'fit_workout_ids': [], 'distance_m': 5000,
             'actual_seconds': 1500, 'predicted_seconds': 1510, 'baseline_seconds': 1550} for i in range(20)]
    result = validate_race_predictions(rows + [rows[0], {**rows[1], 'outcome_id':'leak', 'fit_workout_ids':['leak']}])
    assert result['sample_count'] == 20
    assert result['rejected_count'] == 2
    assert result['eligible_for_publish'] is True
    assert validate_race_predictions(rows[:2])['eligible_for_publish'] is False


def test_goal_adherence_rewards_controlled_easy_running_and_abstains_for_missing_hr():
    from vitalis.intelligence.running import RunningAnalyzer
    from vitalis.intelligence.training_evaluation import review_running

    target = {'session': {'code': 'easy_run', 'total_duration_minutes': (45, 55),
              'target_heart_rate_bpm': (130, 145), 'steps': [
                  {'duration_minutes': (5, 5)}, {}, {'duration_minutes': (5, 5)}]}}
    raw = RawDailyProfile(user_id='synthetic', day=DAY)
    easy = _stable_run(DAY, 'easy', speed=2.5)
    raw.workouts = [easy]
    session = RunningAnalyzer().analyze(raw).recent_sessions[0]
    review = review_running(session, target=target, workout=easy)
    assert review.candidate_score == 100
    assert review.total_score is None
    fast = _stable_run(DAY, 'fast', speed=3.5, hr=170)
    assert review_running(session, target=target, workout=fast).candidate_score < review.candidate_score
    missing = {**easy, 'samples': [x for x in easy['samples'] if x.metric != 'heart_rate']}
    assert review_running(session, target=target, workout=missing).candidate_score is None


def test_relative_index_validation_purges_overlapping_outcomes():
    from vitalis.intelligence.running_validation import validate_relative_indices

    rows = []
    for i in range(70):
        before = date(2025, 1, 1) + timedelta(days=i*7)
        ratio = 1 + (i % 7) * .01
        rows.append({'subject': 'synthetic', 'outcome_id': str(i),
                     'feature_date': before.isoformat(), 'outcome_date': (before+timedelta(days=28)).isoformat(),
                     'rpi_ratio': ratio, 'cs_ratio': 1 + (i % 3) * .01,
                     'actual_speed_ratio': ratio, 'fit_workout_ids': []})
    result = validate_relative_indices(rows)
    assert result['validation_state'] == 'passed'
    assert result['purged_count'] > 0
    assert result['composite_mean_absolute_error_percent'] < result['cs_mean_absolute_error_percent']
    assert validate_relative_indices(rows[:5])['validation_state'] == 'insufficient_data'
