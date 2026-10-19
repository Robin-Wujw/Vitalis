"""Compare a replayed projection with a user's facts, apply it, and undo it.

A parser correction is first replayed into an independent replay user
(``vitalis replay source-journal``).  ``diff`` lists how the replayed facts
differ from the source user's facts in a bounded window; ``apply`` replaces the
source user's facts in that window and keeps the replaced rows so ``undo`` can
restore them.  Both write one transaction and queue a scoped re-analysis.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import gzip
import json
from statistics import median
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.persistence.source_journal import ReplayDataset, SourceJournalRecord
from vitalis.application.replay import ReplayError
from vitalis.domain.features import DAILY_FEATURE_METRICS
from vitalis.time import local_day, local_day_utc_bounds

MAX_APPLY_DAYS = 14
# Day-keyed record tables compare their whole JSON document.
_RECORD_TABLES = (
    ("sleep", orm.SleepRecord),
    ("activity", orm.ActivityRecord),
    ("training", orm.TrainingRecord),
)


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _window(start: date, end: date, timezone_name: str) -> tuple[datetime, datetime]:
    begin, _ = local_day_utc_bounds(start, timezone_name)
    _, finish = local_day_utc_bounds(end, timezone_name)
    return _naive(begin), _naive(finish)


def _workout_ids(db: Session, user_id: str, begin: datetime, finish: datetime) -> list[str]:
    return list(db.scalars(select(orm.Workout.workout_id).where(
        orm.Workout.user_id == user_id,
        orm.Workout.started_at >= begin,
        orm.Workout.started_at < finish,
    )))


def _rows(db: Session, user_id: str, start: date, end: date, timezone_name: str) -> dict[str, list]:
    """All window facts for one user, keyed by table name."""
    begin, finish = _window(start, end, timezone_name)
    output: dict[str, list] = {}
    for name, model in _RECORD_TABLES:
        output[name] = list(db.scalars(select(model).where(
            model.user_id == user_id, model.date.between(start, end),
        )))
    output["daily_metrics"] = list(db.scalars(select(orm.DailyMetric).where(
        orm.DailyMetric.user_id == user_id, orm.DailyMetric.date.between(start, end),
    )))
    output["metric_samples"] = list(db.scalars(select(orm.MetricSample).where(
        orm.MetricSample.user_id == user_id,
        orm.MetricSample.timestamp >= begin, orm.MetricSample.timestamp < finish,
    )))
    workout_ids = _workout_ids(db, user_id, begin, finish)
    output["workouts"] = list(db.scalars(select(orm.Workout).where(
        orm.Workout.user_id == user_id, orm.Workout.workout_id.in_(workout_ids),
    )))
    output["workout_samples"] = list(db.scalars(select(orm.WorkoutMetricSample).where(
        orm.WorkoutMetricSample.user_id == user_id,
        orm.WorkoutMetricSample.workout_id.in_(workout_ids),
    )))
    return output


def _summaries(rows: dict[str, list], timezone_name: str) -> dict[str, dict]:
    """Comparable per-key values; dense samples compare by count and median."""
    output: dict[str, dict] = {}
    for name, _ in _RECORD_TABLES:
        output[name] = {row.date.isoformat(): row.data for row in rows[name]}
    output["daily_metrics"] = {
        f"{row.date}:{row.metric}:{row.source}:{row.source_scope}:{row.device_id}": [row.value, row.unit]
        for row in rows["daily_metrics"]
    }
    samples: dict[str, list[float]] = defaultdict(list)
    for row in rows["metric_samples"]:
        key = f"{local_day(row.timestamp, timezone_name)}:{row.metric}:{row.source}:{row.source_scope}:{row.device_id}"
        samples[key].append(row.value)
    output["metric_samples"] = {
        key: {"count": len(values), "median": round(float(median(values)), 4)}
        for key, values in samples.items()
    }
    output["workouts"] = {
        f"{row.source}:{row.workout_id}": {
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "data": row.data, "detail_synced": row.detail_synced,
        }
        for row in rows["workouts"]
    }
    workout_samples: dict[str, list[float]] = defaultdict(list)
    for row in rows["workout_samples"]:
        workout_samples[f"{row.source}:{row.workout_id}:{row.metric}"].append(row.value)
    output["workout_samples"] = {
        key: {"count": len(values), "median": round(float(median(values)), 4)}
        for key, values in workout_samples.items()
    }
    return output


def _json_diff(old: Any, new: Any, path: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        changes = []
        for key in sorted(set(old) | set(new)):
            changes.extend(_json_diff(old.get(key), new.get(key), f"{path}.{key}" if path else str(key)))
        return changes
    return [] if old == new else [path or "value"]


def diff(
    db: Session, source_user_id: str, replay_user_id: str, start: date, end: date,
    *, timezone_name: str,
) -> dict[str, Any]:
    """List added, removed and changed facts per table; values stay local."""
    _check_pair(db, source_user_id, replay_user_id)
    old = _summaries(_rows(db, source_user_id, start, end, timezone_name), timezone_name)
    new = _summaries(_rows(db, replay_user_id, start, end, timezone_name), timezone_name)
    tables = {}
    for table in old:
        before, after = old[table], new[table]
        changed = {
            key: {"fields": _json_diff(before[key], after[key]), "old": before[key], "new": after[key]}
            for key in sorted(set(before) & set(after)) if before[key] != after[key]
        }
        tables[table] = {
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "changed": changed,
            "unchanged": len(set(before) & set(after)) - len(changed),
        }
    return {
        "source_user_id": source_user_id, "replay_user_id": replay_user_id,
        "start": start.isoformat(), "end": end.isoformat(), "tables": tables,
        "identical": all(
            not item["added"] and not item["removed"] and not item["changed"]
            for item in tables.values()
        ),
    }


def _check_pair(db: Session, source_user_id: str, replay_user_id: str) -> None:
    source = db.get(orm.User, source_user_id)
    replay = db.get(orm.User, replay_user_id)
    dataset = db.get(ReplayDataset, replay_user_id)
    if source is None or replay is None or replay.source_mode != "replay" or dataset is None:
        raise ReplayError("diff and apply need a source user and its bound replay user")
    if dataset.origin_user_id != source_user_id:
        raise ReplayError("the replay user was not replayed from this source user")


def _columns(row) -> dict[str, Any]:
    output = {}
    for column in row.__table__.columns:
        if column.name == "id":
            continue
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = {"__datetime__": value.isoformat()}
        elif isinstance(value, date):
            value = {"__date__": value.isoformat()}
        output[column.name] = value
    return output


def _restore(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__datetime__"}:
        return datetime.fromisoformat(value["__datetime__"])
    if isinstance(value, dict) and set(value) == {"__date__"}:
        return date.fromisoformat(value["__date__"])
    return value


_MODELS = {
    "sleep": orm.SleepRecord, "activity": orm.ActivityRecord, "training": orm.TrainingRecord,
    "daily_metrics": orm.DailyMetric, "metric_samples": orm.MetricSample,
    "workouts": orm.Workout, "workout_samples": orm.WorkoutMetricSample,
}
# Children first, so a workout is never deleted before its samples.
_DELETE_ORDER = (
    "workout_samples", "workouts", "metric_samples", "daily_metrics", "training", "activity", "sleep",
)


def _pack(rows: dict[str, list]) -> str:
    material = {name: [_columns(row) for row in items] for name, items in rows.items()}
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(encoded)).decode("ascii")


def _unpack(value: str) -> dict[str, list[dict]]:
    return json.loads(gzip.decompress(base64.b64decode(value)).decode("utf-8"))


def _replace(
    db: Session, user_id: str, start: date, end: date, timezone_name: str,
    rows: dict[str, list[dict]],
) -> None:
    current = _rows(db, user_id, start, end, timezone_name)
    for name in _DELETE_ORDER:
        model = _MODELS[name]
        ids = [row.id for row in current[name]]
        for offset in range(0, len(ids), 500):
            db.execute(delete(model).where(model.id.in_(ids[offset:offset + 500])))
    db.flush()
    for name in reversed(_DELETE_ORDER):
        model = _MODELS[name]
        for values in rows.get(name, []):
            db.add(model(**{key: _restore(value) for key, value in values.items()}))
    db.flush()


def _after_write(
    db: Session, user_id: str, start: date, end: date, timezone_name: str, reason: str,
) -> None:
    repo = HealthRepository(db)
    day = start
    while day <= end:
        for metric in DAILY_FEATURE_METRICS:
            repo.refresh_daily_features(user_id, metric, day, timezone_name=timezone_name)
        day += timedelta(days=1)
    repo.bump_analysis_input_revision(user_id)
    dates = {start + timedelta(days=offset) for offset in range((end - start).days + 1)}
    repo.enqueue_analysis_job(
        user_id, end, event_type="source_sync", source="replay",
        affected_dates=dates, reason=reason,
    )


def apply(
    db: Session, source_user_id: str, replay_user_id: str, start: date, end: date,
    *, timezone_name: str,
) -> dict[str, Any]:
    """Replace the source user's window facts with the replayed facts."""
    if end < start or (end - start).days + 1 > MAX_APPLY_DAYS:
        raise ReplayError(f"apply covers 1–{MAX_APPLY_DAYS} days")
    _check_pair(db, source_user_id, replay_user_id)
    previous = _rows(db, source_user_id, start, end, timezone_name)
    replayed = _rows(db, replay_user_id, start, end, timezone_name)
    origin = {
        row.id: row.origin_journal_id
        for row in db.scalars(select(SourceJournalRecord).where(
            SourceJournalRecord.user_id == replay_user_id,
        ))
    }
    incoming: dict[str, list[dict]] = {}
    for name, items in replayed.items():
        incoming[name] = []
        for row in items:
            values = _columns(row)
            values["user_id"] = source_user_id
            if name == "metric_samples" and values.get("source_record_id"):
                values["source_record_id"] = origin.get(values["source_record_id"]) or ""
            if name == "workouts":
                rating = (values.get("data") or {}).get("session_rpe")
                if isinstance(rating, dict) and rating.get("source_scope") == "workout_summary" and rating.get("source_record_id"):
                    original_id = origin.get(rating["source_record_id"])
                    original = db.scalar(select(SourceJournalRecord.id).where(
                        SourceJournalRecord.id == original_id,
                        SourceJournalRecord.user_id == source_user_id,
                        SourceJournalRecord.source == values["source"],
                        SourceJournalRecord.stream == "workouts",
                    )) if original_id else None
                    if original is None:
                        raise ReplayError("replay workout RPE has no source journal origin")
                    values["data"] = deepcopy(values["data"])
                    values["data"]["session_rpe"]["source_record_id"] = original
            incoming[name].append(values)
    application = orm.ReplayApplication(
        id=uuid4().hex, source_user_id=source_user_id, replay_user_id=replay_user_id,
        start_date=start, end_date=end, timezone=timezone_name,
        previous_rows=_pack(previous),
        summary={name: {"previous": len(previous[name]), "applied": len(incoming[name])} for name in previous},
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    _replace(db, source_user_id, start, end, timezone_name, incoming)
    db.add(application)
    _after_write(db, source_user_id, start, end, timezone_name, "replay applied")
    db.flush()
    return {"application_id": application.id, "summary": application.summary}


def undo(db: Session, application_id: str) -> dict[str, Any]:
    """Restore the facts an application replaced."""
    application = db.get(orm.ReplayApplication, application_id)
    if application is None:
        raise ReplayError("replay application not found")
    if application.undone_at is not None:
        raise ReplayError("replay application was already undone")
    rows = _unpack(application.previous_rows)
    _replace(
        db, application.source_user_id, application.start_date, application.end_date,
        application.timezone, rows,
    )
    application.undone_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _after_write(
        db, application.source_user_id, application.start_date, application.end_date,
        application.timezone, "replay undone",
    )
    db.flush()
    return {"application_id": application.id, "restored": {name: len(items) for name, items in rows.items()}}
