"""A recorded session rating is attributed, deduplicated and bounded by time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_weekly_coaching import END, _build, _raw
from vitalis.intelligence.analyzers import TrainingAnalyzer
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.report_rendering import render_report, visible_text
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine


def _rated_week(values=(8, 9, None, None)):
    raw = _raw()
    for workout, value in zip(raw.workouts, values, strict=True):
        started = datetime.combine(workout['local_day'], datetime.min.time(), UTC)
        workout['started_at'] = started
        workout['data']['session_rpe'] = None if value is None else {
            'value': value, 'unit': 'score_1_10', 'source': 'zepp',
            'source_scope': 'workout_summary', 'source_field': 'rpe',
            'qualification': 'user_reported', 'time_basis': 'activity_day',
            'observed_at': started.isoformat(),
            'fetched_at': (started + timedelta(hours=2)).isoformat(),
        }
    return raw


@pytest.mark.parametrize('template', ['markdown', 'html'])
def test_watch_rpe_reaches_the_completed_workout_without_faking_feedback(template):
    from tests.test_report_content import synthetic_daily_fixture

    raw = _rated_week((7, None, None, None))
    raw.day = raw.workouts[0]['local_day']
    training = TrainingAnalyzer().analyze(raw, {})
    workout = next(item for item in training.recent_workouts if item.workout_id == 'strength-5')
    assert workout.session_rpe.value == 7
    assert workout.session_rpe.source_scope == 'workout_summary'
    assert workout.session_rpe.fetched_at is not None
    assert 'session_rpe_unavailable' not in training.limitations
    payload = synthetic_daily_fixture()
    payload['date'] = raw.day.isoformat()
    payload['features']['training'] = training.model_dump(mode='json')
    report = EveningBriefingEngine().build(payload)
    content = visible_text(render_report(report, template).content, template)
    assert 'RPE 7/10' in content and 'Zepp' in content
    assert '主观用力' in content


def test_weekly_rpe_uses_only_rated_sessions_and_existing_effort_safety_rule():
    profile = _build(_rated_week())
    effort = profile.facts.training.session_rpe_summary
    assert effort.workout_count == 4
    assert effort.rated_workouts == 2
    assert effort.average_rpe == 8.5
    assert effort.high_effort_workouts == 2
    assert effort.source_counts == {'zepp': 2}
    assert effort.period_start == END - timedelta(days=6) and effort.period_end == END
    assert profile.facts.feedback.response_count == 0
    assert profile.facts.feedback.average_session_rpe is None
    assert '本周尚未记录主观训练反馈。' not in profile.inferences.limitations
    codes = {item.code for item in profile.actions.recommendations}
    assert 'REDUCE_SUBJECTIVE_LOAD' in codes
    text = '\n'.join(WeeklyBriefingEngine().build(profile).suggestions)
    assert '2 次' in text and '8.5/10' in text
    assert '上肢→' not in text
    visible = visible_text(render_report(WeeklyBriefingEngine().build(profile), 'html').content, 'html')
    assert '4 次训练中，2 次填了 RPE（Zepp）' in visible


def test_explicit_feedback_overrides_watch_rating_once_without_source_collision():
    raw = _rated_week((9, None, None, None))
    workout = raw.workouts[0]
    raw.feedback_by_workout[('zepp', workout['workout_id'])] = [
        SimpleNamespace(id='explicit-rpe', created_at=raw.as_of - timedelta(hours=2), session_rpe=6, muscle_soreness=None),
        SimpleNamespace(id='later-note', created_at=raw.as_of - timedelta(hours=1), session_rpe=None, muscle_soreness=None),
        SimpleNamespace(id='future-rating', created_at=raw.as_of + timedelta(hours=1), session_rpe=10, muscle_soreness=None),
    ]
    raw.feedback_by_workout[('other-source', workout['workout_id'])] = [
        SimpleNamespace(id='other-rating', created_at=raw.as_of, session_rpe=1, muscle_soreness=None),
    ]
    effort = _build(raw).facts.training.session_rpe_summary
    assert effort.rated_workouts == 1 and effort.average_rpe == 6
    rating = effort.ratings[0].rating
    assert rating.source_scope == 'subjective_feedback'
    assert rating.source_record_id == 'explicit-rpe'
    assert effort.source_counts == {'vitalis': 1}
    assert raw.workouts[0]['data']['session_rpe']['value'] == 9


def test_late_or_wrong_source_watch_rpe_remains_unavailable():
    raw = _rated_week((8, 9, 7, None))
    raw.workouts[0]['data']['session_rpe']['fetched_at'] = (raw.as_of + timedelta(days=1)).isoformat()
    raw.workouts[1]['data']['session_rpe']['source'] = 'other-source'
    raw.workouts[2]['local_day'] = END - timedelta(days=7)
    effort = _build(raw).facts.training.session_rpe_summary
    assert effort.rated_workouts == 0 and effort.average_rpe is None
    assert effort.ratings == []


def test_session_effort_is_registered_without_fabricating_a_daily_baseline():
    from vitalis.intelligence.signals import SIGNAL_REGISTRY, build_signal_states, build_period_signals

    definition = SIGNAL_REGISTRY['session_rpe']
    assert definition.aggregation_basis == 'workout'
    raw = _rated_week()
    assert 'session_rpe' not in build_signal_states(raw, {})
    assert 'session_rpe' not in build_period_signals(raw, END - timedelta(days=6), END)


def test_current_week_does_not_use_old_workout_rpe_submitted_this_week():
    raw = _rated_week((9, None, None, None))
    workout = raw.workouts[0]
    workout['local_day'] = END - timedelta(days=8)
    raw.feedback_by_workout[('zepp', workout['workout_id'])] = [SimpleNamespace(
        id='late-feedback', created_at=raw.as_of, session_rpe=10, muscle_soreness=None,
    )]
    profile = _build(raw, feedback=[{'session_rpe': 10}])
    assert profile.facts.training.session_rpe_summary.rated_workouts == 0
    assert all(item.code != 'REDUCE_SUBJECTIVE_LOAD' for item in profile.actions.recommendations)


def test_public_weekly_facts_use_workout_window_not_feedback_submission_mean():
    from vitalis.intelligence.public_reports import to_public_report_view

    profile = _build(_rated_week((6, None, None, None)), feedback=[{'session_rpe': 10}])
    view = to_public_report_view(profile, 'weekly')
    facts = [fact for block in view.blocks for fact in block.facts]
    assert all(fact['metric'] != 'feedback_session_rpe' for fact in facts)
    effort = next(fact for fact in facts if fact['metric'] == 'session_rpe')
    assert effort['value'] == 6 and effort['sample_count'] == 1
    assert effort['unit'] == 'score_1_10'
    assert effort['coverage'] == {'rated_workouts': 1, 'recorded_workouts': 4}
    assert effort['source_counts'] == {'zepp': 1}
    assert effort['calendar_semantics'] == 'activity_day'
    assert effort['period_start'] == (END - timedelta(days=6)).isoformat()
    assert effort['ratings'][0]['rating']['source_scope'] == 'workout_summary'


def test_future_or_unfinished_workout_cannot_supply_rpe():
    raw = _rated_week((8, 9, None, None))
    raw.workouts[0]['started_at'] = raw.as_of + timedelta(minutes=1)
    raw.workouts[1]['started_at'] = raw.as_of - timedelta(minutes=1)
    raw.workouts[1]['data']['ended_at'] = (raw.as_of + timedelta(minutes=10)).isoformat()
    assert _build(raw).facts.training.session_rpe_summary.rated_workouts == 0


def test_summary_counts_every_recorded_session_once_beyond_recent_card_limit():
    from copy import deepcopy

    raw = _rated_week((7, None, None, None))
    for index in range(10):
        workout = deepcopy(raw.workouts[0])
        workout['workout_id'] = f'extra-session-{index}'
        raw.workouts.append(workout)
    raw.workouts.append(deepcopy(raw.workouts[0]))
    summary = _build(raw).facts.training.session_rpe_summary
    assert summary.workout_count == 14 and summary.rated_workouts == 11
    assert summary.average_rpe == 7


def test_summary_rpe_survives_parser_repository_loader_and_report():
    from tests.test_workout_rpe import START, _journal, _payload, _workout
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.intelligence.profile import ProfileLoader
    from tests.test_report_content import synthetic_daily_fixture
    from vitalis.time import local_day

    user = 'rpe-whole-pipeline'
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        payload = _payload(6)
        record = _journal(db, user, payload)
        db.info['active_source_record_id'] = record.id
        repo.save_workout(_workout(user, payload))
        db.info.pop('active_source_record_id')
        raw = ProfileLoader(repo).load(user, local_day(START), as_of=START + timedelta(hours=3))
        training = TrainingAnalyzer().analyze(raw, {})
        rating = training.recent_workouts[0].session_rpe
        assert rating.value == 6 and rating.source_record_id == record.id
        assert rating.fetched_at == START + timedelta(hours=1)
        assert raw.feedback_by_workout == {}
        assert training.strength.recent_sessions[0].session_rpe == 6
    payload = synthetic_daily_fixture()
    payload['date'] = raw.day.isoformat()
    payload['features']['training'] = training.model_dump(mode='json')
    report = EveningBriefingEngine().build(payload)
    assert '主观用力 RPE 6/10（Zepp 记录' in render_report(report, 'markdown').content
