"""Synthetic regressions for usable dose and chronological recommendations."""
from datetime import date, datetime, timedelta, timezone
from vitalis.intelligence.profile import RawDailyProfile
from vitalis.intelligence.strength import StrengthAnalyzer
from vitalis.intelligence.report_presentation import _strength_display_section

DAY = date(2026, 8, 20)


def workout(offset, codes, *, missing=False):
    day = DAY - timedelta(days=offset)
    rows = [{'source': 'lap_62', 'order': i, 'vendor_exercise_code': code, 'repetitions': 10}
            for i, code in enumerate(codes, 1)]
    return {'source': 'mock', 'workout_id': f'synthetic-{offset}', 'local_day': day,
            'started_at': datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
            'data': {'type': 'strength', 'training_family': 'strength', 'duration': 40,
                     'vendor_reported_sets': len(rows)},
            'detail': {'strength_sets': [] if missing else rows}}


def profile(workouts):
    return RawDailyProfile(user_id='synthetic', day=DAY, workouts=workouts,
        training_history_coverage={'verified_days': [(DAY-timedelta(days=i)).isoformat() for i in range(180)]})


def test_incidental_push_does_not_reset_main_push_date_or_choose_legs():
    raw = profile([workout(3, [6]*8), workout(2, [3]*10+[6]*2), workout(0, [18]*10)])
    result = StrengthAnalyzer().analyze(raw)
    assert result.next_focus == 'PUSH'
    push = next(h for h in result.focus_history if h.focus == 'PUSH')
    assert push.last_main_day == DAY-timedelta(days=3)
    assert push.last_contact_day == DAY-timedelta(days=2)
    assert push.recent_direct_sets == 10
    assert any(str(DAY-timedelta(days=3)) in r for r in result.next_focus_reasons)


def test_tied_directions_are_joint_candidates_not_alphabetic_winner():
    raw = profile([workout(4, [6]*6+[3]*6), workout(0, [18]*8)])
    result = StrengthAnalyzer().analyze(raw)
    assert result.next_focus is None
    assert set(result.next_focus_candidates) == {'PUSH', 'LEGS'}


def test_missing_weight_still_displays_quantified_muscle_training():
    raw = profile([workout(0, [6]*5)])
    result = StrengthAnalyzer().analyze(raw)
    session = result.recent_sessions[0]
    assert session.capacity_volume_kg is None
    assert session.focus_doses[0].direct_sets == 5
    card = _strength_display_section({'date': str(DAY), 'features': {'training': {'strength': result.model_dump(mode='json')}}})
    text = ' '.join(card['facts'])
    assert '胸部 5 组' in text
    assert '0 组可计算' not in text


def test_partial_history_keeps_older_usable_sessions_and_individual_weeks():
    raw = profile([workout(i, [6]*6) for i in (0, 8, 15, 22, 36, 43, 50, 57)] + [workout(29, [6]*6, missing=True)])
    result = StrengthAnalyzer().analyze(raw)
    h = next(x for x in result.focus_history if x.focus == 'PUSH')
    assert result.history_sessions == 9
    assert result.sessions_28d == 4
    assert h.baseline_sessions >= 6
    assert h.baseline_complete_weeks >= 3
    assert h.typical_session_sets == 6
    assert h.baseline_missing_sessions == 1
    assert h.weekly_ratio is not None


def test_future_sessions_cannot_change_historical_recommendation():
    raw = profile([workout(5, [6]*6), workout(3, [3]*6), workout(0, [18]*6)])
    before = StrengthAnalyzer().analyze(raw)
    raw.workouts.append(workout(-1, [6]*20))
    after = StrengthAnalyzer().analyze(raw)
    assert before.focus_history == after.focus_history
    assert before.next_focus_candidates == after.next_focus_candidates


def test_multi_muscle_action_counts_once_per_direction():
    result = StrengthAnalyzer().analyze(profile([workout(0, [6]*6)]))
    dose = next(x for x in result.recent_sessions[0].focus_doses if x.focus == 'PUSH')
    assert dose.direct_sets == 6
    assert dose.indirect_sets == 0


def test_recent_main_leg_work_cannot_win_due_to_low_weekly_dose():
    raw = profile([workout(4, [6]*8), workout(0, [3]*6), workout(2, [18]*8)])
    result = StrengthAnalyzer().analyze(raw)
    assert 'LEGS' not in result.next_focus_candidates
    assert result.next_focus == 'PUSH'


def test_muscle_history_preserves_samples_without_weight_or_rir():
    result = StrengthAnalyzer().analyze(profile([workout(i, [6]*6) for i in (0,8,15,36,43)]))
    chest = next(h for h in result.muscle_history if h.muscle_group == 'chest')
    assert chest.direct_sets_7d == 6
    assert chest.baseline_sessions == 4
    assert chest.typical_session_sets == 6


def test_old_soreness_does_not_block_a_direction_forever():
    from vitalis.intelligence.strength_recommendations import recommend_focus
    raw = profile([workout(5, [6]*6),workout(3,[3]*6),workout(0,[18]*6)])
    result = StrengthAnalyzer().analyze(raw)
    sessions = [s.model_copy(update={'muscle_soreness':5}) if s.date == DAY-timedelta(days=5) else s for s in result.recent_sessions]
    assert recommend_focus(raw,sessions).candidates == ['PUSH']
    sessions = [s.model_copy(update={'date':DAY-timedelta(days=2),'muscle_soreness':5}) if s.date == DAY-timedelta(days=5) else s for s in sessions]
    assert 'PUSH' not in recommend_focus(raw,sessions).candidates


def test_missing_days_preserve_observed_sets_but_not_weekly_deficit():
    raw = profile([workout(i,[6]*6) for i in (0,8,15,22,36,43)])
    raw.training_history_coverage = {}
    h = next(x for x in StrengthAnalyzer().analyze(raw).focus_history if x.focus == 'PUSH')
    assert h.recent_direct_sets == 6
    assert h.typical_session_sets == 6
    assert h.baseline_sessions == 5
    assert h.weekly_ratio is None
    assert h.typical_interval_days is None


def test_joint_candidate_decision_does_not_repeat_latest_session():
    from types import SimpleNamespace
    from vitalis.intelligence.decision import DecisionEngine
    from vitalis.intelligence.contracts import TrainingPreferences
    analysis = StrengthAnalyzer().analyze(profile([workout(4,[6]*6+[3]*6),workout(0,[18]*6)]))
    plan = DecisionEngine()._strength(TrainingPreferences(user_id='synthetic'),SimpleNamespace(strength=analysis),'PRIMARY','moderate')
    assert plan.code == 'strength_joint_candidates'
    assert '背部' not in plan.focus


def test_recent_lower_back_work_is_accounted_for_before_leg_recommendation():
    from vitalis.intelligence.contracts import MuscleTrainingDose
    from vitalis.intelligence.strength_recommendations import recommend_focus
    raw = profile([workout(4,[6]*6),workout(8,[3]*6),workout(0,[18]*6)])
    result = StrengthAnalyzer().analyze(raw)
    sessions = list(result.recent_sessions)
    sessions[0] = sessions[0].model_copy(update={'reference_muscle_doses': [*sessions[0].reference_muscle_doses,
        MuscleTrainingDose(muscle_group='lower_back',muscle_group_label='下背部',direct_sets=4,
                           equivalent_sets=4,qualification='vendor_reference')]})
    recommendation = recommend_focus(raw,sessions)
    assert recommendation.candidates == ['PUSH']
    assert any('腰背' in r for r in recommendation.reasons)
