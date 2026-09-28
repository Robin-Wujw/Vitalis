"""SQL adapter for the application raw-health query port."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from vitalis.application.ports import (
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
        """Read the half-open ``[start, end)`` window through the legacy repository."""
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

    def daily_metrics(
        self, user_id: str, start: date, end: date, metric: str | None = None
    ) -> tuple[HealthDailyMetricRead, ...]:
        with self._session_factory() as db:
            rows = HealthRepository(db).daily_metrics(user_id, start, end, metric)
            return tuple(
                HealthDailyMetricRead(
                    date=row.date,
                    metric=row.metric,
                    value=row.value,
                    unit=row.unit,
                    source=row.source,
                    source_scope=row.source_scope or "unknown",
                    device_id=row.device_id or None,
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

    def workouts(
        self, user_id: str, start: date, end: date, limit: int
    ) -> tuple[HealthWorkoutRead, ...]:
        with self._session_factory() as db:
            rows = HealthRepository(db).workouts(user_id, start, end, limit)
            return tuple(_workout_read(row) for row in rows)

    def workout_detail(
        self, user_id: str, workout_id: str, source: str
    ) -> tuple[HealthWorkoutRead, tuple[HealthWorkoutSampleRead, ...]] | None:
        with self._session_factory() as db:
            repository = HealthRepository(db)
            row = repository.workout(user_id, workout_id, source=source)
            if row is None:
                return None
            samples = repository.workout_metric_samples(
                user_id, workout_id, source=source
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
    )


def _workout_read(row: orm.Workout) -> HealthWorkoutRead:
    return HealthWorkoutRead(
        source=row.source,
        workout_id=row.workout_id,
        data=deepcopy(row.data) if isinstance(row.data, dict) else {},
        detail=deepcopy(row.detail) if isinstance(row.detail, dict) else None,
        detail_synced=bool(row.detail_synced),
    )


HealthReaderAdapter = SqlHealthReader
