"""Chunked, transaction-safe persistence for streamed workout details."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from vitalis.domain import WorkoutMetricSample

from . import models as orm

SAMPLE_BATCH_SIZE = 256


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _sample_key(sample: WorkoutMetricSample) -> tuple[Any, ...]:
    timestamp = sample.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (
        timestamp.astimezone(UTC).isoformat(),
        sample.metric,
        float(sample.value),
        sample.unit,
        sample.source_scope or "workout_detail",
        sample.device_id or "",
        int(sample.sample_ordinal or 0),
    )


def _hash_keys(
    keys: Iterator[tuple[Any, ...]], check: Callable[[], None] | None = None
) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for key in keys:
        if check is not None and count % 256 == 0:
            check()
        digest.update(
            json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _existing_keys(
    db: Session, user_id: str, workout_id: str, source: str
) -> Iterator[tuple[Any, ...]]:
    rows = db.execute(
        select(
            orm.WorkoutMetricSample.timestamp,
            orm.WorkoutMetricSample.metric,
            orm.WorkoutMetricSample.value,
            orm.WorkoutMetricSample.unit,
            orm.WorkoutMetricSample.source_scope,
            orm.WorkoutMetricSample.device_id,
            orm.WorkoutMetricSample.sample_ordinal,
        )
        .where(
            orm.WorkoutMetricSample.user_id == user_id,
            orm.WorkoutMetricSample.source == source,
            orm.WorkoutMetricSample.workout_id == workout_id,
        )
        .order_by(
            orm.WorkoutMetricSample.timestamp,
            orm.WorkoutMetricSample.metric,
            orm.WorkoutMetricSample.source_scope,
            orm.WorkoutMetricSample.device_id,
            orm.WorkoutMetricSample.sample_ordinal,
        )
        .execution_options(yield_per=SAMPLE_BATCH_SIZE)
    )
    for row in rows:
        timestamp = (
            row.timestamp.replace(tzinfo=UTC)
            if row.timestamp.tzinfo is None
            else row.timestamp
        )
        yield (
            timestamp.astimezone(UTC).isoformat(),
            row.metric,
            float(row.value),
            row.unit,
            row.source_scope or "workout_detail",
            row.device_id or "",
            int(row.sample_ordinal or 0),
        )


def _write_samples(
    db: Session,
    samples: Iterator[WorkoutMetricSample],
    *,
    user_id: str,
    workout_id: str,
    source: str,
    check: Callable[[], None] | None = None,
) -> int:
    written = 0
    batch: list[dict[str, Any]] = []
    for sample in samples:
        if check is not None and written % SAMPLE_BATCH_SIZE == 0:
            check()
        batch.append(
            {
                "user_id": user_id,
                "source": source,
                "workout_id": workout_id,
                "timestamp": _naive_utc(sample.timestamp),
                "metric": sample.metric,
                "value": float(sample.value),
                "unit": sample.unit,
                "source_scope": sample.source_scope or "workout_detail",
                "device_id": sample.device_id or "",
                "sample_ordinal": int(sample.sample_ordinal or 0),
            }
        )
        if len(batch) >= SAMPLE_BATCH_SIZE:
            db.execute(orm.WorkoutMetricSample.__table__.insert(), batch)
            written += len(batch)
            batch.clear()
    if batch:
        db.execute(orm.WorkoutMetricSample.__table__.insert(), batch)
        written += len(batch)
    db.flush()
    return written


def write_streamed_workout_detail(
    repo,
    user_id: str,
    workout_id: str,
    detail: dict,
    sample_source,
    *,
    source: str = "zepp",
    fetched_at: datetime | None = None,
    check: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Publish metadata and staged samples atomically in a nested transaction.

    ``sample_source`` must be replayable (the built-in ``SampleSpool`` is).
    The source is scanned once for idempotency and once only when replacement
    is needed.  Any parse/cancel/write exception rolls back every sample and
    leaves ``detail_synced`` unchanged.
    """
    db = getattr(repo, "db", None)
    if not isinstance(db, Session):
        raise TypeError("streamed workout detail requires a SQLAlchemy repository")
    row = db.execute(
        select(orm.Workout).where(
            orm.Workout.user_id == user_id,
            orm.Workout.source == source,
            orm.Workout.workout_id == workout_id,
        )
    ).scalar_one_or_none()
    if row is None:
        return 0, 0

    fetched = fetched_at or datetime.now(UTC)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    payload = dict(detail or {})
    payload["fetched_at"] = (
        fetched.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    previous = dict(row.detail or {}) if isinstance(row.detail, dict) else {}
    previous.pop("fetched_at", None)
    incoming = dict(payload)
    incoming.pop("fetched_at", None)
    detail_changed = previous != incoming or not bool(row.detail_synced)

    existing_hash, existing_count = _hash_keys(
        _existing_keys(db, user_id, workout_id, source), check
    )
    incoming_hash, incoming_count = _hash_keys(
        (_sample_key(sample) for sample in sample_source.iter_samples(workout_id)),
        check,
    )
    samples_changed = existing_hash != incoming_hash or existing_count != incoming_count

    with db.begin_nested():
        if samples_changed:
            db.execute(
                delete(orm.WorkoutMetricSample).where(
                    orm.WorkoutMetricSample.user_id == user_id,
                    orm.WorkoutMetricSample.source == source,
                    orm.WorkoutMetricSample.workout_id == workout_id,
                )
            )
            _write_samples(
                db,
                sample_source.iter_samples(workout_id),
                user_id=user_id,
                workout_id=workout_id,
                source=source,
                check=check,
            )
        row.detail = payload
        row.detail_synced = True
        db.flush()

    if detail_changed or samples_changed:
        repo._bump_existing_input_revisions({user_id})
        if row.started_at is not None:
            from vitalis.time import local_day

            affected_day = local_day(row.started_at)
            repo.enqueue_analysis_job(
                user_id,
                affected_day,
                event_type="source_sync",
                source=source,
                affected_dates={affected_day},
                affected_streams={"workout_detail"},
                reason="workout detail changed",
            )
    return 1, incoming_count


__all__ = ["SAMPLE_BATCH_SIZE", "write_streamed_workout_detail"]
