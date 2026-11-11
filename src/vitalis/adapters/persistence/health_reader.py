"""SQL adapter for the application raw-health query port."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from vitalis.application.ports import (
    HealthBodyRecordRead,
    HealthBrowserLinkRead,
    HealthDailyMetricRead,
    HealthDenseFileRead,
    HealthMetricRead,
    HealthReader,
    HealthStreamStateRead,
    HealthSyncAttemptRead,
    HealthSyncChunkRead,
    HealthTokenMetadataRead,
    HealthWorkoutRead,
    HealthWorkoutSampleRead,
)

from . import models as orm
from .database import SessionLocal
from .repositories import HealthRepository


class SqlHealthReader(HealthReader):
    """Read and detach all values needed by :class:`HealthQuery`."""

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def sleep_records(self, user_id: str, start: date, end: date, *, as_of: datetime,
                      limit: int | None = None, max_bytes: int | None = None) -> tuple[dict, ...]:
        from .band_sources import read_band_sources

        with self._session_factory() as db:
            return tuple(read_band_sources(db, user_id, start, end, as_of=as_of,
                                          include_activity_details=False, kind="sleep",limit=limit,max_bytes=max_bytes))

    def activity_records(self, user_id: str, start: date, end: date, *, as_of: datetime,
                         include_minutes: bool = False, include_hourly: bool = False,
                         limit: int | None = None, max_bytes: int | None = None) -> tuple[dict, ...]:
        from .band_sources import read_band_sources
        with self._session_factory() as db:
            return tuple(read_band_sources(db, user_id, start, end, as_of=as_of, kind="activity",
                include_minutes=include_minutes,include_hourly=include_hourly,limit=limit,max_bytes=max_bytes))

    def source_snapshot(self, user_id: str, kind: str) -> dict | None:
        with self._session_factory() as db:
            row = db.get(orm.SourceSnapshot, (user_id, "zepp", kind))
            return {"data": deepcopy(row.data), "fetched_at": row.fetched_at,
                    "source_record_id": row.source_record_id, "source": row.source} if row else None

    def nap_records(self, user_id: str, start: date, end: date, *, as_of: datetime,
                    limit: int | None = None, max_bytes: int | None = None) -> tuple[dict, ...]:
        from .band_sources import read_band_sources
        with self._session_factory() as db:
            return tuple(read_band_sources(db, user_id, start, end, as_of=as_of,
                                          include_activity_details=False, kind="naps",limit=limit,max_bytes=max_bytes))

    def raw_source_records(self, user_id: str, stream: str, start: date, end: date, limit: int) -> tuple[dict, ...]:
        from .source_journal import SourceJournalRecord as Raw, SourceJournalRepository as Journal
        from sqlalchemy.orm import load_only
        from vitalis.time import local_day_utc_bounds
        from vitalis.config import settings
        lower = local_day_utc_bounds(start, settings.timezone)[0].replace(tzinfo=None)
        upper = local_day_utc_bounds(end, settings.timezone)[1].replace(tzinfo=None)
        with self._session_factory() as db:
            rows = db.execute(select(Raw).options(load_only(*(getattr(Raw, column.key)
                for column in Raw.__table__.columns if column.key != "encrypted_payload"))).where(
                Raw.user_id == user_id, Raw.source == "zepp", Raw.stream == stream,
                Raw.observed_start < upper,
                (Raw.observed_end > lower) | (Raw.observed_end.is_(None) & (Raw.observed_start >= lower)))
                .order_by(Raw.fetched_at.desc(), Raw.id).limit(limit)).scalars()
            return tuple(Journal.metadata(row) for row in rows)

    def raw_source_record(self, user_id: str, record_id: str) -> dict | None:
        from .source_journal import SourceJournalRecord as Raw, SourceJournalRepository as Journal
        from vitalis.application.vendor_request import MAX_RAW_BYTES, public_raw_payload
        with self._session_factory() as db:
            row = db.execute(select(Raw).where(Raw.id == record_id, Raw.user_id == user_id)).scalar_one_or_none()
            if row is None:
                return None
            if row.payload_bytes > MAX_RAW_BYTES or row.payload_format != "json":
                raise ValueError("raw JSON response exceeds the public read budget")
            return {**Journal.metadata(row), "qualification": "raw_unverified",
                    "payload": public_raw_payload(Journal(db).read_payload(row.id, user_id=user_id))}

    def raw_job_result(self, user_id: str, job_id: str) -> dict | None:
        from .source_journal import SourceJournalRecord as Raw
        with self._session_factory() as db:
            attempt = db.execute(select(orm.SyncAttempt).where(orm.SyncAttempt.id == job_id,
                orm.SyncAttempt.user_id == user_id)).scalar_one_or_none()
            if attempt is None or not (attempt.options or {}).get("raw_request"):
                return None
            chunks = db.execute(select(orm.SyncChunk.id).where(orm.SyncChunk.attempt_id == job_id)).scalars().all()
            row_id = db.execute(select(Raw.id).where(Raw.user_id == user_id,
                Raw.record_id.in_([f"raw_request:{chunk}" for chunk in chunks]))
                .order_by(Raw.fetched_at.desc()).limit(1)).scalar_one_or_none()
            status = attempt.status
        return {"job_id": job_id, "status": status,
                "result": self.raw_source_record(user_id, row_id) if row_id else None}

    def sync_stream_states(self, user_id: str) -> tuple[HealthStreamStateRead, ...]:
        with self._session_factory() as db:
            rows = HealthRepository(db).sync_stream_states(user_id)
            return tuple(
                HealthStreamStateRead(
                    stream=row.stream,
                    fetch_status=row.fetch_status,
                    fetched_at=row.fetched_at,
                    parse_status=row.parse_status,
                    parsed_at=row.parsed_at,
                    write_status=row.write_status,
                    written_at=row.written_at,
                    last_sample_at=row.last_sample_at,
                    raw_records=row.raw_records,
                    records_written=row.records_written,
                    error_kind=row.error_kind,
                )
                for row in rows
            )

    def wake_days(self, user_id: str, start: date, end: date):
        from vitalis.application.wake_check import collect

        with self._session_factory() as db:
            return collect(HealthRepository(db), user_id, end, (end - start).days + 1)

    def stream_observation_dates(
        self,
        user_id: str,
        stream: str,
        *,
        start: date,
        end: date,
        timezone_name: str,
        source: str = "zepp",
    ) -> tuple[date, ...]:
        with self._session_factory() as db:
            return tuple(sorted(HealthRepository(db).stream_observation_dates(
                user_id,
                stream,
                source=source,
                window_start=start,
                window_end=end,
                timezone_name=timezone_name,
            )))

    def latest_sync_attempt(
        self, user_id: str, source: str = "zepp"
    ) -> HealthSyncAttemptRead | None:
        with self._session_factory() as db:
            rows = HealthRepository(db).sync_attempts(user_id, source=source, limit=1)
            if not rows:
                return None
            row = rows[0]
            return HealthSyncAttemptRead(
                attempt_id=row.id,
                status=row.status,
                trigger=row.trigger,
                window_start=row.window_start,
                window_end=row.window_end,
                deadline_at=row.deadline_at,
                retry_count=row.retry_count,
                next_retry_at=row.next_retry_at,
            )

    def sync_chunks(
        self, attempt_id: str, user_id: str
    ) -> tuple[HealthSyncChunkRead, ...]:
        with self._session_factory() as db:
            rows = HealthRepository(db).sync_chunks(attempt_id, user_id=user_id)
            return tuple(
                HealthSyncChunkRead(
                    stream=row.stream,
                    health_stream=row.health_stream,
                    status=row.status,
                    records_written=row.records_written or 0,
                    raw_records=row.raw_records or 0,
                )
                for row in rows
            )

    def metric_samples(
        self,
        user_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
    ) -> tuple[HealthMetricRead, ...]:
        """Read the half-open ``[start, end)`` window through the repository adapter."""
        inclusive_end = end - timedelta(microseconds=1)
        if inclusive_end < start:
            return ()
        with self._session_factory() as db:
            rows = HealthRepository(db).metric_samples(
                user_id,
                metric,
                start,
                inclusive_end,
                limit=limit,
            )
            return tuple(_metric_read(row) for row in rows)

    @contextmanager
    def metric_sample_rows(
        self, user_id: str, metric: str, start: datetime, end: datetime
    ) -> Iterator[Iterator[HealthMetricRead]]:
        """Stream detached rows while keeping the read session scoped to the caller."""
        inclusive_end = end - timedelta(microseconds=1)
        if inclusive_end < start:
            yield iter(())
            return
        with self._session_factory() as db:
            rows = HealthRepository(db).metric_sample_rows(
                user_id, metric, start, inclusive_end
            )
            yield (_metric_read(row) for row in rows)

    def body_composition_records(
        self, user_id: str, metrics: tuple[str, ...], start: datetime | None, end: datetime,
        *, limit: int, after: tuple | None = None,
    ) -> tuple[HealthBodyRecordRead, ...]:
        """Merge bounded indexed metric pages, then read complete chosen records."""
        sample = orm.MetricSample
        keys = (sample.timestamp, sample.source, sample.source_scope, sample.device_id,
                sample.source_record_id, sample.sample_ordinal)
        conditions = [sample.user_id == user_id,
            sample.timestamp < end.astimezone(timezone.utc).replace(tzinfo=None)]
        if start is not None:
            conditions.append(sample.timestamp >= start.astimezone(timezone.utc).replace(tzinfo=None))
        if after is not None:
            position = (after[0].astimezone(timezone.utc).replace(tzinfo=None), *after[1:])
            conditions.append(tuple_(*keys) < tuple_(*position))
        with self._session_factory() as db:
            candidates = set()
            # Each observation has at most one row per metric. A record outside
            # every metric's first N positions cannot be in the merged first N.
            for metric in metrics:
                candidates.update(tuple(row) for row in db.execute(select(*keys).where(
                    *conditions, sample.metric == metric,
                ).order_by(*(column.desc() for column in keys)).limit(limit)))
            selected = sorted(candidates, reverse=True)[:limit]
            if not selected:
                return ()
            grouped = {key: [] for key in selected}
            rows = db.scalars(select(sample).where(sample.user_id == user_id,
                sample.metric.in_(metrics), sample.timestamp.in_({key[0] for key in selected}),
                tuple_(*keys).in_(selected)))
            for row in rows:
                key = (row.timestamp, row.source, row.source_scope, row.device_id,
                       row.source_record_id, row.sample_ordinal)
                grouped[key].append((row.metric, _metric_read(row)))
            return tuple(HealthBodyRecordRead(*key, measurements=tuple(sorted(grouped[key])))
                         for key in selected)

    def daily_metrics(
        self, user_id: str, start: date, end: date, metric: str | None = None, *, limit: int | None = None
    ) -> tuple[HealthDailyMetricRead, ...]:
        with self._session_factory() as db:
            statement = select(orm.DailyMetric).where(orm.DailyMetric.user_id == user_id,
                orm.DailyMetric.date.between(start, end))
            if metric is not None:
                statement = statement.where(orm.DailyMetric.metric == metric)
            statement = statement.order_by(orm.DailyMetric.date, orm.DailyMetric.metric)
            if limit is not None:
                statement = statement.limit(limit)
            rows = db.scalars(statement.execution_options(yield_per=128))
            return tuple(
                HealthDailyMetricRead(
                    date=row.date,
                    metric=row.metric,
                    value=row.value,
                    unit=row.unit,
                    source=row.source,
                    source_scope=row.source_scope or "unknown",
                    device_id=row.device_id or None,
                    source_field=row.source_field, qualification=row.qualification,
                    observed_at=row.observed_at, fetched_at=row.fetched_at, revision_at=row.revision_at,
                    contributing_device_id=row.contributing_device_id,
                )
                for row in rows
            )

    def dense_files(
        self, user_id: str, stream: str, start: date, end: date, limit: int
    ) -> tuple[HealthDenseFileRead, ...]:
        with self._session_factory() as db:
            rows = HealthRepository(db).dense_data_files(
                user_id, stream, start, end, limit
            )
            return tuple(
                HealthDenseFileRead(
                    file_type=row.file_type,
                    date=row.date,
                    start_utc=row.start_utc,
                    end_utc=row.end_utc,
                    source_scope=row.source_scope or "unknown",
                    device_id=row.device_id or None,
                    parse_status=row.parse_status,
                    sample_count=row.sample_count,
                )
                for row in rows
            )

    def strength_review_workouts(
        self, user_id: str, start: date, end: date, limit: int,
        *, timezone_name: str,
    ) -> tuple[HealthWorkoutRead, ...]:
        """Read bounded saved workouts and their confirmations, never samples."""
        with self._session_factory() as db:
            repository = HealthRepository(db)
            rows = repository.workouts(
                user_id, start, end, limit, timezone_name=timezone_name,
            )
            confirmations = repository.strength_exercises_for_workout_keys(
                user_id, [(row.source, row.workout_id) for row in rows],
            )
            return tuple(
                HealthWorkoutRead(
                    source=row.source, workout_id=row.workout_id,
                    data=deepcopy(row.data) if isinstance(row.data, dict) else {},
                    detail=deepcopy(row.detail) if isinstance(row.detail, dict) else None,
                    detail_synced=bool(row.detail_synced),
                    confirmed_exercises=tuple(
                        item.model_dump(mode="json")
                        for item in confirmations.get((row.source, row.workout_id), [])
                    ),
                )
                for row in rows
            )

    def workout_statistics_workouts(
        self, user_id: str, start: date, end: date, limit: int,
        *, timezone_name: str,
    ) -> tuple[HealthWorkoutRead, ...]:
        """Project summaries and small set fields; never load GPS or sample streams."""
        from vitalis.time import local_day_utc_bounds

        start_at, _ = local_day_utc_bounds(start, timezone_name)
        _, end_at = local_day_utc_bounds(end, timezone_name)
        with self._session_factory() as db:
            rows = db.execute(select(
                orm.Workout.source, orm.Workout.workout_id, orm.Workout.data, orm.Workout.detail_synced,
                orm.Workout.detail["strength_sets"].label("strength_sets"),
                orm.Workout.detail["fetched_at"].label("detail_fetched_at"),
                orm.Workout.detail["schema_version"].label("detail_schema_version"),
            ).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= start_at.replace(tzinfo=None),
                orm.Workout.started_at < end_at.replace(tzinfo=None),
            ).order_by(orm.Workout.started_at.desc(), orm.Workout.source, orm.Workout.workout_id).limit(limit))
            return tuple(HealthWorkoutRead(
                source=row.source, workout_id=row.workout_id, data=deepcopy(row.data),
                detail={"strength_sets": row.strength_sets or [], "fetched_at": row.detail_fetched_at,
                        "schema_version": row.detail_schema_version} if row.detail_synced else None,
                detail_synced=bool(row.detail_synced),
            ) for row in rows)

    def workout_detail(
        self, user_id: str, workout_id: str, source: str,
        *, include_samples: bool = False,
    ) -> tuple[HealthWorkoutRead, tuple[HealthWorkoutSampleRead, ...]] | None:
        with self._session_factory() as db:
            repository = HealthRepository(db)
            row = repository.workout(user_id, workout_id, source=source)
            if row is None:
                return None
            samples = (
                repository.workout_metric_samples(user_id, workout_id, source=source)
                if include_samples else ()
            )
            return (
                _workout_read(row),
                tuple(
                    HealthWorkoutSampleRead(
                        timestamp=sample.timestamp,
                        metric=sample.metric,
                        value=sample.value,
                        unit=sample.unit,
                        source_scope=sample.source_scope or "unknown",
                        device_id=sample.device_id or None,
                    )
                    for sample in samples
                ),
            )

    def token_metadata(
        self, user_id: str, source: str = "zepp"
    ) -> HealthTokenMetadataRead | None:
        """Read token metadata without touching encrypted credential columns."""
        with self._session_factory() as db:
            row = db.execute(
                select(orm.AuthToken, orm.SourceAccount)
                .join(
                    orm.SourceAccount,
                    orm.SourceAccount.id == orm.AuthToken.source_account_id,
                )
                .where(
                    orm.SourceAccount.user_id == user_id,
                    orm.SourceAccount.source == source,
                    orm.SourceAccount.status == "active",
                )
            ).one_or_none()
            if row is None:
                return None
            token, account = row
            return HealthTokenMetadataRead(
                vendor_user_id=account.vendor_id,
                region_host=token.region_host or "",
                expires_at=token.expires_at,
            )

    def workout_detail_coverage(self, user_id: str, source: str = "zepp") -> dict:
        with self._session_factory() as db:
            return HealthRepository(db).workout_detail_coverage(user_id, source=source)

    def latest_browser_link(self, user_id: str) -> HealthBrowserLinkRead | None:
        with self._session_factory() as db:
            row = HealthRepository(db).latest_browser_link(user_id)
            if row is None:
                return None
            return HealthBrowserLinkRead(
                status=row.status,
                message=row.message,
                last_verified_at=row.last_verified_at,
                last_sync_at=row.last_sync_at,
            )


def _metric_read(row: orm.MetricSample) -> HealthMetricRead:
    return HealthMetricRead(
        timestamp=row.timestamp,
        value=row.value,
        unit=row.unit,
        source=row.source,
        source_scope=row.source_scope or "unknown",
        device_id=row.device_id or None,
        source_record_id=row.source_record_id or None,
        sample_ordinal=row.sample_ordinal if row.source_record_id else None,
        source_field=row.source_field, qualification=row.qualification,
        fetched_at=row.fetched_at, revision_at=row.revision_at,
        contributing_device_id=row.contributing_device_id,
    )


def _workout_read(row: orm.Workout) -> HealthWorkoutRead:
    return HealthWorkoutRead(
        source=row.source,
        workout_id=row.workout_id,
        data=deepcopy(row.data) if isinstance(row.data, dict) else {},
        detail=deepcopy(row.detail) if isinstance(row.detail, dict) else None,
        detail_synced=bool(row.detail_synced),
    )
