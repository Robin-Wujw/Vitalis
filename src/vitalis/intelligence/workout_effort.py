"""Resolve reported session effort without creating feedback or inventing ratings."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from statistics import mean

from pydantic import ValidationError

from vitalis.domain.models import WorkoutRpe

from .contracts import SessionRpeRecord, SessionRpeSummary


def _timestamp(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def workout_rpe(raw, workout: dict) -> WorkoutRpe | None:
    """Explicit Vitalis feedback wins; a later note does not erase a rating."""
    source, identity = workout.get('source'), workout.get('workout_id')
    if not source or not identity:
        return None
    cutoff = _timestamp(raw.as_of)
    started = _timestamp(workout.get('started_at'))
    data = workout.get('data') or {}
    ended = _timestamp(data.get('ended_at'))
    if ended is None and started is not None and data.get('duration') is not None:
        ended = started + timedelta(minutes=data['duration'])
    if cutoff is not None and any(value is not None and value > cutoff for value in (started, ended)):
        return None
    feedback = [
        item for item in raw.feedback_by_workout.get((source, identity), [])
        if getattr(item, 'session_rpe', None) is not None
        and _timestamp(item.created_at) is not None
        and (cutoff is None or _timestamp(item.created_at) <= cutoff)
    ]
    if feedback:
        latest = max(feedback, key=lambda item: (_timestamp(item.created_at), str(getattr(item, 'id', ''))))
        try:
            return WorkoutRpe(
                value=latest.session_rpe, source='vitalis', source_scope='subjective_feedback',
                source_field='session_rpe', qualification='user_reported',
                observed_at=started, fetched_at=_timestamp(latest.created_at),
                source_record_id=getattr(latest, 'id', None),
            )
        except ValidationError:
            return None
    value = data.get('session_rpe')
    if not isinstance(value, (dict, WorkoutRpe)):
        return None
    try:
        rating = WorkoutRpe.model_validate(value)
    except ValidationError:
        return None
    if rating.source != source or rating.source_scope != 'workout_summary' or rating.source_field != 'rpe':
        return None
    if cutoff is not None and any(
        moment is not None and moment > cutoff
        for moment in (_timestamp(rating.observed_at), _timestamp(rating.fetched_at))
    ):
        return None
    return rating


def session_rpe_summary(raw, start: date, end: date) -> SessionRpeSummary:
    workouts = {}
    for index, item in enumerate(raw.workouts):
        day = item.get('local_day')
        if isinstance(day, date) and start <= day <= end:
            key = (item.get('source'), item.get('workout_id') or f'missing-identity-{index}')
            workouts[key] = item
    ratings = [
        SessionRpeRecord(
            workout_id=item['workout_id'], workout_source=item['source'],
            date=item['local_day'], rating=rating,
        )
        for item in workouts.values() if (rating := workout_rpe(raw, item)) is not None
    ]
    ratings.sort(key=lambda item: (item.date, item.workout_source, item.workout_id))
    return SessionRpeSummary(
        period_start=start, period_end=end, workout_count=len(workouts),
        rated_workouts=len(ratings),
        average_rpe=round(mean(item.rating.value for item in ratings), 1) if ratings else None,
        high_effort_workouts=sum(item.rating.value >= 8 for item in ratings), ratings=ratings,
        source_counts=dict(Counter(item.rating.source for item in ratings)),
    )
