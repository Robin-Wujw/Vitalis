"""Experimental goal-adherence scoring with fixed missing-data semantics.

Scores describe adherence to an explicit plan, not benefit or injury risk.
The caller stores these as candidates; public total_score remains unset.
"""

from math import isfinite


def range_score(value, bounds):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or not bounds:
        return None
    low, high = bounds
    if not 0 < low <= high or value < 0:
        return None
    if value < low:
        return round(100 * value / low, 2)
    if value <= high:
        return 100.0
    return round(max(0, 100 * (2 - value / high)), 2)


def candidate_grade(dimensions):
    keys = ('completion', 'dose', 'execution')
    if any(dimensions.get(k) is None for k in keys):
        return None
    if any(not isinstance(dimensions[k], (int, float)) or not isfinite(dimensions[k]) or not 0 <= dimensions[k] <= 100 for k in keys):
        return None
    return round(sum(dimensions[k] * w for k, w in zip(keys, (.4, .4, .2))), 1)


def score_running(session, workout, target):
    from .running_performance import _bins, _epoch

    goal = target['session']
    scores = {'completion': range_score(session.duration_minutes, goal.get('total_duration_minutes')),
              'dose': None, 'execution': None}
    samples = workout.get('samples') or []
    hr = _bins(samples, 'heart_rate', 'bpm')
    bounds = goal.get('target_heart_rate_bpm')
    steps = goal.get('steps') or []
    if goal.get('code') == 'threshold_intervals':
        work = [s for s in session.segments if s.kind == 'work']
        expected = [s.get('sets') for s in steps if s.get('sets')]
        if len(expected) == 1 and work:
            scores['execution'] = range_score(len(work), (expected[0], expected[0]))
        start = workout.get('started_at')
        if start and work:
            origin = _epoch(start)
            hr = {k: v for k, v in hr.items() if any(s.start_offset_seconds <= k*10-origin < s.end_offset_seconds for s in work)}
        else:
            hr = {}
    else:
        # Remove prescribed warmup/cooldown. Pauses reduce observable execution
        # coverage; slower paces and deliberate recovery do not lose points.
        start = workout.get('started_at')
        if start and len(steps) >= 3 and steps[0].get('duration_minutes') and steps[-1].get('duration_minutes'):
            warm = steps[0]['duration_minutes'][1] * 60
            cool = steps[-1]['duration_minutes'][1] * 60
            origin = _epoch(start)
            seconds = session.duration_minutes * 60 - warm - cool
            hr = {k: v for k, v in hr.items() if warm <= k*10-origin < session.duration_minutes*60-cool}
            if seconds > 0 and len(hr) * 10 / seconds >= .95:
                # Sensor gaps are a qualification issue, never a performance penalty.
                scores['execution'] = range_score(max(0, seconds-session.pause_duration_seconds), (seconds, seconds))
        else:
            hr = {}
    if bounds and len(hr) >= 48:
        scores['dose'] = round(sum(bounds[0] <= value <= bounds[1] for value in hr.values()) / len(hr) * 100, 2)
    return scores


def score_strength(session, target):
    goal = target['session']
    scores = {'completion': range_score(session.duration_minutes, goal.get('total_duration_minutes')),
              'dose': None, 'execution': None}
    exercises = session.explicit_exercises
    rir_bounds = goal.get('target_rir')
    if exercises and rir_bounds and all(x.rir is not None for x in exercises):
        # No feedback is invented: this covers only the explicit recorded scope.
        scores['dose'] = round(sum(rir_bounds[0] <= x.rir <= rir_bounds[1] for x in exercises) / len(exercises) * 100, 2)
    prescribed = [s for s in goal.get('steps') or [] if s.get('sets')]
    if len(prescribed) == len(exercises) and prescribed and all(s.get('target_repetitions') for s in prescribed):
        values = []
        for step, exercise in zip(prescribed, exercises):
            if step['name'] != exercise.exercise_name or exercise.sets is None:
                return scores
            values.extend([range_score(exercise.sets, (step['sets'], step['sets'])),
                           range_score(exercise.repetitions, step['target_repetitions'])])
        if all(v is not None for v in values):
            scores['execution'] = round(sum(values) / len(values), 2)
    return scores
