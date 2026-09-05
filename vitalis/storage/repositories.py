"""仓储层：封装对 ORM 的读写，业务层只依赖仓储接口。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import Integer, case, cast, delete, func, or_, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vitalis.models import (
    AuthToken,
    Device,
    NormalizedDaily,
    DailyMetric,
    DenseDataFile,
    MetricSample,
    TrainingRecord,
    Workout,
    WorkoutMetricSample,
)
from vitalis.intelligence.contracts import (
    HealthEvent,
    HealthEventObservation,
    RecommendationInstance,
    RecommendationStatus,
    StrengthExerciseRecord,
    SubjectiveFeedback,
    TrainingPreferenceInput,
    TrainingPreferencePatch,
    TrainingPreferences,
    DAILY_SCHEMA_VERSION,
    WEEKLY_SCHEMA_VERSION,
    MONTHLY_SCHEMA_VERSION,
    INTELLIGENCE_VERSION,
    DECISION_POLICY_VERSION,
    EVIDENCE_VERSION,
    ConfidenceBand,
    ProfileRevisionConflict,
    ProfileSource,
    ProfileField,
    Sex,
    UserProfile,
    UserProfilePatch,
)

from .sync_types import SyncAttemptAggregate, SyncLease
from vitalis.time import local_day, local_day_utc_bounds

from . import models as orm


class SourceIdentityConflict(ValueError):
    """A vendor identity is already owned by another local user."""


@dataclass(frozen=True)
class WorkoutAnalysisSample:
    source: str
    workout_id: str
    timestamp: datetime
    metric: str
    value: float
    unit: str
    source_scope: str
    device_id: str | None


_CURRENT_SNAPSHOT_SCHEMAS = {
    "daily": DAILY_SCHEMA_VERSION,
    "weekly": WEEKLY_SCHEMA_VERSION,
    "monthly": MONTHLY_SCHEMA_VERSION,
    "training_responses": "1.0",
    "personal_model": "2.0",
    "personal_associations": "1.0",
}


class HealthRepository:
    """健康数据仓储：负责 vitalis.models（schema）<-> ORM（表）的映射。"""

    def __init__(self, db: Session):
        self.db = db

    # ---- 用户 ----
    def upsert_user(self, user_id: str, name: str = "", source: str = "zepp", source_user_id: str | None = None) -> orm.User:
        normalized_source_user_id = (
            source_user_id.strip() if isinstance(source_user_id, str) else source_user_id
        )
        normalized_source_user_id = normalized_source_user_id or None
        u = self.db.get(orm.User, user_id)
        if u is None:
            u = orm.User(
                id=user_id,
                name=name,
                source=source,
                source_user_id=normalized_source_user_id,
            )
            self.db.add(u)
        else:
            u.name = name or u.name
            if source_user_id is not None:
                u.source = source
                u.source_user_id = normalized_source_user_id
        return u

    def identity_context(self, user_id: str) -> dict:
        """Describe local/vendor identity mapping without merging any records."""
        user = self.db.get(orm.User, user_id)
        source_user_id = user.source_user_id if user else None
        source = user.source if user else "zepp"
        if source_user_id is None:
            token = self.db.execute(select(orm.AuthToken).where(
                orm.AuthToken.user_id == user_id,
                orm.AuthToken.source == source,
            )).scalar_one_or_none()
            source_user_id = token.source_user_id if token else None
        local_user_ids = {user_id}
        if source_user_id:
            local_user_ids.update(self.db.execute(select(orm.User.id).where(
                orm.User.source == source,
                orm.User.source_user_id == source_user_id,
            )).scalars().all())
            local_user_ids.update(self.db.execute(select(orm.AuthToken.user_id).where(
                orm.AuthToken.source == source,
                orm.AuthToken.source_user_id == source_user_id,
            )).scalars().all())
        return {
            "local_user_id": user_id,
            "source": source,
            "source_user_id_present": source_user_id is not None,
            "shared_local_user_count": len(local_user_ids),
        }

    def source_identity_owned_by_other(
        self, user_id: str, source: str, source_user_id: str
    ) -> bool:
        source_user_id = source_user_id.strip()
        token_owner = self.db.execute(
            select(orm.AuthToken.id).where(
                orm.AuthToken.source == source,
                orm.AuthToken.source_user_id == source_user_id,
                orm.AuthToken.user_id != user_id,
            ).limit(1)
        ).scalar_one_or_none()
        if token_owner is not None or source != "zepp":
            return token_owner is not None
        user_owner = self.db.execute(
            select(orm.User.id).where(
                orm.User.source == source,
                orm.User.source_user_id == source_user_id,
                orm.User.id != user_id,
            ).limit(1)
        ).scalar_one_or_none()
        return user_owner is not None

    # ---- 设备 ----
    def upsert_device(self, device: Device) -> orm.Device:
        row = self.db.execute(select(orm.Device).where(
            orm.Device.user_id == device.user_id,
            orm.Device.source == device.source,
            orm.Device.device_id == device.device_id,
        )).scalar_one_or_none()
        if row is None:
            stable_id = hashlib.sha256(
                f"{device.user_id}:{device.source}:{device.device_id}".encode("utf-8")
            ).hexdigest()
            row = orm.Device(
                id=stable_id,
                user_id=device.user_id,
                source=device.source,
                model=device.model,
                device_id=device.device_id,
            )
            self.db.add(row)
        else:
            row.model = device.model or row.model
        self.db.flush()
        return row

    def devices(self, user_id: str) -> list[orm.Device]:
        return list(self.db.execute(
            select(orm.Device).where(orm.Device.user_id == user_id).order_by(
                orm.Device.model, orm.Device.device_id
            )
        ).scalars().all())

    # ---- 每日健康 ----
    def save_daily(self, daily: NormalizedDaily) -> None:
        """Persist normalized sleep/activity/training records for one local day."""
        if daily.sleep:
            self._upsert(orm.SleepRecord, daily.user_id, daily.date,
                         daily.sleep.model_dump(mode="json", exclude_none=True))
        if daily.activity:
            self._upsert(orm.ActivityRecord, daily.user_id, daily.date,
                         daily.activity.model_dump(mode="json", exclude_none=True))
        if daily.training:
            self._upsert(orm.TrainingRecord, daily.user_id, daily.date,
                         daily.training.model_dump(mode="json", exclude_none=True))
        if daily.metric_samples:
            for sample in daily.metric_samples:
                sample.user_id = daily.user_id
            self.save_metric_samples(daily.metric_samples)

        self.db.flush()

    def _upsert(self, model, user_id: str, day, data: dict) -> None:
        """Upsert one current-contract daily row by ``(user_id, date)``."""
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(model).values(
                user_id=user_id,
                date=day,
                data=data,
            )
            self.db.execute(statement.on_conflict_do_update(
                index_elements=["user_id", "date"],
                set_={"data": statement.excluded.data},
            ))
            return

        existing = self.db.execute(select(model).where(
            model.user_id == user_id,
            model.date == day,
        )).scalar_one_or_none()
        if existing is None:
            self.db.add(model(user_id=user_id, date=day, data=data))
        else:
            existing.data = data

    # ---- 查询 ----
    def get_sleep(self, user_id: str, day: date) -> dict | None:
        row = self.db.execute(
            select(orm.SleepRecord).where(orm.SleepRecord.user_id == user_id, orm.SleepRecord.date == day)
        ).scalar_one_or_none()
        return row.data if row else None

    def sleep_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.SleepRecord).where(
                orm.SleepRecord.user_id == user_id,
                orm.SleepRecord.date.between(start, end),
            ).order_by(orm.SleepRecord.date)
        ).scalars().all()
        return [r.data for r in rows]


    def activity_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.ActivityRecord).where(
                orm.ActivityRecord.user_id == user_id,
                orm.ActivityRecord.date.between(start, end),
            ).order_by(orm.ActivityRecord.date)
        ).scalars().all()
        return [r.data for r in rows]

    def training_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.TrainingRecord).where(
                orm.TrainingRecord.user_id == user_id,
                orm.TrainingRecord.date.between(start, end),
            ).order_by(orm.TrainingRecord.date)
        ).scalars().all()
        return [r.data for r in rows]

    # ---- 通用指标时序 ----

    def save_metric_samples(self, samples: list[MetricSample]) -> int:
        """Idempotently upsert timestamped measurements."""
        deduplicated: dict[
            tuple[str, str, str, datetime, str, str], MetricSample
        ] = {}
        for sample in samples:
            key = (
                sample.user_id,
                sample.source,
                sample.metric,
                _naive_utc(sample.timestamp),
                sample.source_scope or "unknown",
                sample.device_id or "",
            )
            deduplicated[key] = sample

        if not deduplicated:
            return 0

        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert

            rows = [
                {
                    "user_id": user_id,
                    "source": source,
                    "metric": metric,
                    "timestamp": timestamp,
                    "source_scope": source_scope,
                    "device_id": device_id,
                    "value": sample.value,
                    "unit": sample.unit,
                }
                for (
                    user_id, source, metric, timestamp, source_scope, device_id
                ), sample in deduplicated.items()
            ]
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.MetricSample).values(rows[offset:offset + 500])
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "user_id", "source", "metric", "timestamp", "source_scope",
                        "device_id",
                    ],
                    set_={
                        "value": statement.excluded.value,
                        "unit": statement.excluded.unit,
                    },
                )
                self.db.execute(statement)
            self.db.flush()
            return len(rows)

        written = 0
        for (
            user_id, source, metric, timestamp, source_scope, device_id
        ), sample in deduplicated.items():
            row = self.db.execute(
                select(orm.MetricSample).where(
                    orm.MetricSample.user_id == user_id,
                    orm.MetricSample.source == source,
                    orm.MetricSample.metric == metric,
                    orm.MetricSample.timestamp == timestamp,
                    orm.MetricSample.source_scope == source_scope,
                    orm.MetricSample.device_id == device_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = orm.MetricSample(
                    user_id=user_id,
                    source=source,
                    metric=metric,
                    timestamp=timestamp,
                    source_scope=source_scope,
                    device_id=device_id,
                )
                self.db.add(row)
            row.value = sample.value
            row.unit = sample.unit
            row.source_scope = source_scope
            row.device_id = device_id
            written += 1
        self.db.flush()
        return written

    def metric_samples(
        self, user_id: str, metric: str, start: datetime, end: datetime, limit: int = 50_000
    ) -> list[orm.MetricSample]:
        return list(self.metric_sample_rows(user_id, metric, start, end, limit=limit))

    def metric_sample_rows(
        self,
        user_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ):
        statement = select(orm.MetricSample).where(
            orm.MetricSample.user_id == user_id,
            orm.MetricSample.metric == metric,
            orm.MetricSample.timestamp.between(_naive_utc(start), _naive_utc(end)),
        ).order_by(orm.MetricSample.timestamp)
        if limit is not None:
            statement = statement.limit(limit)
        return self.db.execute(
            statement.execution_options(yield_per=10_000)
        ).scalars()

    def heart_rate_minute_medians(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float, str, str, str | None, str]]:
        """Aggregate a dense heart-rate window before rows leave the database."""
        params = {
            "user_id": user_id,
            "start": _naive_utc(start),
            "end": _naive_utc(end),
        }
        if self.db.get_bind().dialect.name == "postgresql":
            statement = text("""
                SELECT date_trunc('minute', timestamp) AS minute,
                       source, source_scope, device_id, unit,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS median_value
                FROM metric_samples
                WHERE user_id = :user_id
                  AND metric = 'heart_rate'
                  AND timestamp BETWEEN :start AND :end
                  AND value BETWEEN 25 AND 240
                GROUP BY minute, source, source_scope, device_id, unit
                ORDER BY minute, source, source_scope, device_id, unit
            """)
        else:
            statement = text("""
                WITH ranked AS (
                    SELECT strftime('%Y-%m-%d %H:%M:00', timestamp) AS minute,
                           source, source_scope, device_id, unit, value,
                           row_number() OVER (
                               PARTITION BY strftime('%Y-%m-%d %H:%M:00', timestamp),
                                            source, source_scope, device_id, unit
                               ORDER BY value
                           ) AS sample_rank,
                           count(*) OVER (
                               PARTITION BY strftime('%Y-%m-%d %H:%M:00', timestamp),
                                            source, source_scope, device_id, unit
                           ) AS sample_count
                    FROM metric_samples
                    WHERE user_id = :user_id
                      AND metric = 'heart_rate'
                      AND timestamp BETWEEN :start AND :end
                      AND value BETWEEN 25 AND 240
                )
                SELECT minute, source, source_scope, device_id, unit,
                       avg(value) AS median_value
                FROM ranked
                WHERE sample_rank IN (
                    (sample_count + 1) / 2,
                    (sample_count + 2) / 2
                )
                GROUP BY minute, source, source_scope, device_id, unit
                ORDER BY minute, source, source_scope, device_id, unit
            """)
        output = []
        for row in self.db.execute(statement, params).mappings():
            minute = row["minute"]
            if isinstance(minute, str):
                minute = datetime.fromisoformat(minute)
            output.append((
                minute,
                float(row["median_value"]),
                row["source"],
                row["source_scope"],
                row["device_id"] or None,
                row["unit"],
            ))
        return output

    def save_daily_metrics(self, metrics: list[DailyMetric]) -> int:
        """Idempotently upsert sparse daily vendor metrics by source stream."""
        deduplicated: dict[
            tuple[str, str, date, str, str, str], DailyMetric
        ] = {}
        for metric in metrics:
            key = (
                metric.user_id,
                metric.source,
                metric.date,
                metric.metric,
                metric.source_scope or "unknown",
                metric.device_id or "",
            )
            deduplicated[key] = metric

        if not deduplicated:
            return 0

        rows = [
            {
                "user_id": user_id,
                "source": source,
                "date": day,
                "metric": metric_name,
                "source_scope": source_scope,
                "device_id": device_id,
                "value": metric.value,
                "unit": metric.unit,
            }
            for (
                user_id, source, day, metric_name, source_scope, device_id
            ), metric in deduplicated.items()
        ]
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.DailyMetric).values(
                    rows[offset:offset + 500]
                )
                self.db.execute(statement.on_conflict_do_update(
                    index_elements=[
                        "user_id", "source", "date", "metric", "source_scope",
                        "device_id",
                    ],
                    set_={
                        "value": statement.excluded.value,
                        "unit": statement.excluded.unit,
                    },
                ))
            self.db.flush()
            return len(rows)

        for values in rows:
            row = self.db.execute(select(orm.DailyMetric).where(
                orm.DailyMetric.user_id == values["user_id"],
                orm.DailyMetric.source == values["source"],
                orm.DailyMetric.date == values["date"],
                orm.DailyMetric.metric == values["metric"],
                orm.DailyMetric.source_scope == values["source_scope"],
                orm.DailyMetric.device_id == values["device_id"],
            )).scalar_one_or_none()
            if row is None:
                self.db.add(orm.DailyMetric(**values))
            else:
                row.value = values["value"]
                row.unit = values["unit"]
        self.db.flush()
        return len(rows)

    def daily_metrics(self, user_id: str, start: date, end: date, metric: str | None = None) -> list[orm.DailyMetric]:
        stmt = select(orm.DailyMetric).where(
            orm.DailyMetric.user_id == user_id,
            orm.DailyMetric.date.between(start, end),
        )
        if metric:
            stmt = stmt.where(orm.DailyMetric.metric == metric)
        return list(self.db.execute(stmt.order_by(orm.DailyMetric.date, orm.DailyMetric.metric)).scalars().all())

    # ---- 密集数据文件索引 ----

    def save_dense_data_files(self, files: list[DenseDataFile]) -> int:
        """Idempotently persist dense-file indexes and their decode status."""
        deduplicated = {
            (
                item.user_id,
                item.source,
                item.stream,
                item.file_id,
                _naive_utc(item.start_utc) if item.start_utc else None,
                item.device_id or "",
            ): item
            for item in files
            if item.file_id
        }
        if not deduplicated:
            return 0

        dialect = self.db.get_bind().dialect.name
        rows = [
            {
                "user_id": user_id,
                "source": source,
                "stream": stream,
                "file_id": file_id,
                "file_type": item.file_type,
                "date": item.date,
                "start_utc": _naive_utc(item.start_utc) if item.start_utc else None,
                "end_utc": _naive_utc(item.end_utc) if item.end_utc else None,
                "source_scope": item.source_scope,
                "device_id": item.device_id or "",
                "parse_status": item.parse_status,
                "sample_count": item.sample_count,
            }
            for (user_id, source, stream, file_id, start_utc, device_id), item in deduplicated.items()
        ]
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.DenseDataFile).values(rows[offset:offset + 500])
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "user_id", "source", "stream", "file_id", "start_utc", "device_id"
                    ],
                    set_={
                        "file_type": statement.excluded.file_type,
                        "date": statement.excluded.date,
                        "start_utc": statement.excluded.start_utc,
                        "end_utc": statement.excluded.end_utc,
                        "source_scope": statement.excluded.source_scope,
                        "device_id": statement.excluded.device_id,
                        "parse_status": statement.excluded.parse_status,
                        "sample_count": statement.excluded.sample_count,
                    },
                )
                self.db.execute(statement)
            self.db.flush()
            return len(rows)

        for values in rows:
            row = self.db.execute(select(orm.DenseDataFile).where(
                orm.DenseDataFile.user_id == values["user_id"],
                orm.DenseDataFile.source == values["source"],
                orm.DenseDataFile.stream == values["stream"],
                orm.DenseDataFile.file_id == values["file_id"],
                orm.DenseDataFile.start_utc == values["start_utc"],
                orm.DenseDataFile.device_id == values["device_id"],
            )).scalar_one_or_none()
            if row is None:
                row = orm.DenseDataFile(**values)
                self.db.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        self.db.flush()
        return len(rows)

    def dense_data_files(
        self, user_id: str, stream: str, start: date, end: date, limit: int = 5000
    ) -> list[orm.DenseDataFile]:
        return list(self.db.execute(
            select(orm.DenseDataFile).where(
                orm.DenseDataFile.user_id == user_id,
                orm.DenseDataFile.stream == stream,
                orm.DenseDataFile.date.between(start, end),
            ).order_by(orm.DenseDataFile.start_utc, orm.DenseDataFile.id).limit(limit)
        ).scalars().all())

    def dense_data_file_group(
        self,
        user_id: str,
        stream: str,
        file_id: str,
        source: str | None = None,
    ) -> list[orm.DenseDataFile]:
        statement = select(orm.DenseDataFile).where(
            orm.DenseDataFile.user_id == user_id,
            orm.DenseDataFile.stream == stream,
            orm.DenseDataFile.file_id == file_id,
        )
        if source is not None:
            statement = statement.where(orm.DenseDataFile.source == source)
        return list(self.db.execute(
            statement.order_by(
                orm.DenseDataFile.start_utc, orm.DenseDataFile.id
            )
        ).scalars().all())

    # ---- 单次运动 ----

    def save_workout(self, workout: Workout) -> set[date]:
        """Upsert a canonical workout and return its affected local days."""
        existing = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == workout.user_id,
                orm.Workout.source == workout.source,
                orm.Workout.workout_id == workout.workout_id,
            )
        ).scalar_one_or_none()
        affected = {
            local_day(existing.started_at)
            for existing in (existing,)
            if existing is not None and existing.started_at is not None
        }
        started_at = _naive_utc(workout.started_at) if workout.started_at else None
        if workout.started_at is not None:
            affected.add(local_day(workout.started_at))
        values = {
            "user_id": workout.user_id,
            "source": workout.source,
            "workout_id": workout.workout_id,
            "started_at": started_at,
            "vendor_source": workout.vendor_source,
            "data": workout.model_dump(mode="json", exclude_none=True),
        }

        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(orm.Workout).values(**values)
            self.db.execute(statement.on_conflict_do_update(
                index_elements=["user_id", "source", "workout_id"],
                set_={
                    "started_at": statement.excluded.started_at,
                    "vendor_source": statement.excluded.vendor_source,
                    "data": statement.excluded.data,
                },
            ))
        elif existing is None:
            self.db.add(orm.Workout(**values))
        else:
            existing.started_at = started_at
            existing.vendor_source = workout.vendor_source
            existing.data = values["data"]
        self.db.flush()
        return affected

    def rebuild_training_days(self, user_id: str, days: set[date]) -> int:
        """Rebuild derived training rows from canonical workouts."""
        changed = 0
        for day in sorted(days):
            start_at, _ = local_day_utc_bounds(day)
            _, end_at = local_day_utc_bounds(day)
            rows = list(self.db.execute(select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= _naive_utc(start_at),
                orm.Workout.started_at < _naive_utc(end_at),
            )).scalars().all())
            if not rows:
                result = self.db.execute(delete(orm.TrainingRecord).where(
                    orm.TrainingRecord.user_id == user_id,
                    orm.TrainingRecord.date == day,
                ))
                changed += int(bool(result.rowcount))
                continue
            training = TrainingRecord(
                user_id=user_id,
                source="canonical_workouts",
                date=day,
                workout_count=len(rows),
                total_duration=sum(int((row.data or {}).get("duration") or 0) for row in rows),
                total_load=sum(int((row.data or {}).get("load") or 0) for row in rows),
            )
            self.save_daily(NormalizedDaily(
                user_id=user_id,
                date=day,
                training=training,
            ))
            changed += 1
        self.db.flush()
        return changed

    def pending_workout_details(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        limit: int = 100,
        source: str = "zepp",
        *,
        refresh_after: datetime | None = None,
        strength_only: bool = False,
        exclude_workout_ids: set[str] | None = None,
    ) -> list[orm.Workout]:
        """Return bounded detail candidates, preserving backlog progress.

        Unsynced/old-schema rows are always preferred.  ``refresh_after`` allows
        scheduled runs to re-fetch recent strength sessions even when their
        current detail already has schema 4.0; the timestamp lives in the JSON
        detail metadata so no schema migration is needed.
        """
        budget = max(0, int(limit))
        if budget == 0:
            return []
        conditions = [
            orm.Workout.user_id == user_id,
            orm.Workout.source == source,
            orm.Workout.started_at >= _naive_utc(start),
            orm.Workout.started_at < _naive_utc(end),
            orm.Workout.vendor_source.is_not(None),
        ]
        excluded = set(exclude_workout_ids or set())
        if excluded:
            conditions.append(orm.Workout.workout_id.not_in(excluded))
        if strength_only:
            training_family = orm.Workout.data["training_family"].as_string()
            workout_type = orm.Workout.data["type"].as_string()
            conditions.append(or_(
                training_family == "strength",
                func.lower(workout_type) == "strength",
            ))

        schema_version = orm.Workout.detail["schema_version"].as_string()
        fetched_at = orm.Workout.detail["fetched_at"].as_string()
        backlog = or_(
            orm.Workout.detail_synced.is_(False),
            orm.Workout.detail_synced.is_(None),
            orm.Workout.detail.is_(None),
            schema_version.is_(None),
            schema_version != "4.0",
        )
        order = (orm.Workout.started_at.desc(), orm.Workout.id.desc())
        backlog_rows = list(self.db.execute(
            select(orm.Workout).where(*conditions, backlog).order_by(*order).limit(budget)
        ).scalars().all())
        rows = list(backlog_rows)

        # A refresh is a second bounded query.  It only considers current
        # schema details with an old/missing fetched_at; fresh 4.0 details are
        # never returned merely because the caller requested a batch.
        remaining = budget - len(rows)
        if refresh_after is not None and remaining > 0:
            cutoff = _naive_utc(refresh_after)
            cutoff_iso = cutoff.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            refresh_rows = list(self.db.execute(
                select(orm.Workout).where(
                    *conditions,
                    orm.Workout.detail_synced.is_(True),
                    schema_version == "4.0",
                    or_(fetched_at.is_(None), fetched_at < cutoff_iso),
                ).order_by(fetched_at.asc().nulls_first(), *order).limit(remaining)
            ).scalars().all())
            known_ids = {row.id for row in rows}
            rows.extend(row for row in refresh_rows if row.id not in known_ids)

        rows.sort(key=lambda row: (
            not (
                not row.detail_synced
                or not isinstance(row.detail, dict)
                or row.detail.get("schema_version") != "4.0"
            ),
            -(row.started_at.timestamp() if row.started_at else 0),
            -row.id,
        ))
        return rows[:budget]

    def save_workout_detail(
        self,
        user_id: str,
        workout_id: str,
        detail: dict,
        samples: list[WorkoutMetricSample] | None = None,
        *,
        source: str = "zepp",
        fetched_at: datetime | None = None,
    ) -> bool:
        row = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        payload = dict(detail or {})
        fetched = fetched_at or datetime.now(timezone.utc)
        fetched = fetched if fetched.tzinfo else fetched.replace(tzinfo=timezone.utc)
        payload["fetched_at"] = fetched.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        row.detail = payload
        row.detail_synced = True
        if samples is not None:
            self.db.execute(delete(orm.WorkoutMetricSample).where(
                orm.WorkoutMetricSample.user_id == user_id,
                orm.WorkoutMetricSample.source == source,
                orm.WorkoutMetricSample.workout_id == workout_id,
            ))
            self.db.add_all([
                orm.WorkoutMetricSample(
                    user_id=user_id,
                    source=source,
                    workout_id=workout_id,
                    timestamp=_naive_utc(sample.timestamp),
                    metric=sample.metric,
                    value=sample.value,
                    unit=sample.unit,
                    source_scope=sample.source_scope,
                    device_id=sample.device_id,
                )
                for sample in samples
            ])
        self.db.flush()
        return True

    def workout_metric_samples(
        self,
        user_id: str,
        workout_id: str,
        metric: str | None = None,
        limit: int = 250_000,
        source: str = "zepp",
    ) -> list[orm.WorkoutMetricSample]:
        query = select(orm.WorkoutMetricSample).where(
            orm.WorkoutMetricSample.user_id == user_id,
            orm.WorkoutMetricSample.source == source,
            orm.WorkoutMetricSample.workout_id == workout_id,
        )
        if metric:
            query = query.where(orm.WorkoutMetricSample.metric == metric)
        return list(self.db.execute(
            query.order_by(
                orm.WorkoutMetricSample.timestamp,
                orm.WorkoutMetricSample.metric,
            ).limit(limit)
        ).scalars().all())

    def workout_metric_samples_for_workouts(
        self,
        user_id: str,
        workout_ids: list[str] | list[tuple[str, str]],
        limit: int = 2_000_000,
        resolution_seconds: int = 5,
        source: str = "zepp",
    ) -> list[WorkoutAnalysisSample]:
        if not workout_ids:
            return []
        workout_keys = [
            (source, item) if isinstance(item, str) else item
            for item in workout_ids
        ]
        resolution_seconds = max(int(resolution_seconds), 1)
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            epoch = cast(func.strftime("%s", orm.WorkoutMetricSample.timestamp), Integer)
        elif dialect == "postgresql":
            epoch = cast(func.extract("epoch", orm.WorkoutMetricSample.timestamp), Integer)
        else:
            rows = self.workout_metric_samples_for_workouts_raw(
                user_id, workout_keys, limit
            )
            return [
                WorkoutAnalysisSample(
                    source=row.source,
                    workout_id=row.workout_id,
                    timestamp=row.timestamp,
                    metric=row.metric,
                    value=row.value,
                    unit=row.unit,
                    source_scope=row.source_scope,
                    device_id=row.device_id,
                )
                for row in rows
            ]
        bucket = cast(epoch / resolution_seconds, Integer)
        value = case(
            (orm.WorkoutMetricSample.metric == "distance", func.max(orm.WorkoutMetricSample.value)),
            else_=func.avg(orm.WorkoutMetricSample.value),
        ).label("value")
        statement = (
            select(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                func.min(orm.WorkoutMetricSample.timestamp).label("timestamp"),
                orm.WorkoutMetricSample.metric,
                value,
                orm.WorkoutMetricSample.unit,
                orm.WorkoutMetricSample.source_scope,
                orm.WorkoutMetricSample.device_id,
            )
            .where(
                orm.WorkoutMetricSample.user_id == user_id,
                tuple_(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                ).in_(workout_keys),
            )
            .group_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                orm.WorkoutMetricSample.metric,
                orm.WorkoutMetricSample.unit,
                orm.WorkoutMetricSample.source_scope,
                orm.WorkoutMetricSample.device_id,
                bucket,
            )
            .order_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                func.min(orm.WorkoutMetricSample.timestamp),
                orm.WorkoutMetricSample.metric,
            )
            .limit(limit)
        )
        return [
            WorkoutAnalysisSample(
                source=row.source,
                workout_id=row.workout_id,
                timestamp=row.timestamp,
                metric=row.metric,
                value=float(row.value),
                unit=row.unit,
                source_scope=row.source_scope,
                device_id=row.device_id,
            )
            for row in self.db.execute(statement)
        ]

    def workout_metric_samples_for_workouts_raw(
        self,
        user_id: str,
        workout_ids: list[str] | list[tuple[str, str]],
        limit: int = 2_000_000,
        source: str = "zepp",
    ) -> list[orm.WorkoutMetricSample]:
        if not workout_ids:
            return []
        workout_keys = [
            (source, item) if isinstance(item, str) else item
            for item in workout_ids
        ]
        return list(self.db.execute(
            select(orm.WorkoutMetricSample).where(
                orm.WorkoutMetricSample.user_id == user_id,
                tuple_(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                ).in_(workout_keys),
            ).order_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                orm.WorkoutMetricSample.timestamp,
                orm.WorkoutMetricSample.metric,
            ).limit(limit)
        ).scalars().all())

    def workouts(self, user_id: str, start: date, end: date, limit: int = 500) -> list[orm.Workout]:
        start_at, _ = local_day_utc_bounds(start)
        _, end_at = local_day_utc_bounds(end)
        return list(self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= _naive_utc(start_at),
                orm.Workout.started_at < _naive_utc(end_at),
            ).order_by(orm.Workout.started_at.desc()).limit(limit)
        ).scalars().all())

    def open_health_load_inputs(
        self,
        user_id: str,
        start: date,
        end: date,
        *,
        metric: str = "heart_rate",
        source: str | None = None,
        limit: int = 200,
    ):
        """Return source-qualified workouts with only the requested metric samples.

        This loader deliberately does not use the general 28-day profile loader or
        fetch any other workout metric.  ``metric`` remains explicit for query tests;
        the TRIMP algorithm always calls it with ``heart_rate``.
        """
        from vitalis.intelligence.open_health.load import (
            HeartRatePoint,
            LoadWorkout,
            LoadWorkoutBatch,
            PauseInterval,
        )

        start_at, _ = local_day_utc_bounds(start)
        _, end_at = local_day_utc_bounds(end)
        statement = select(orm.Workout).where(
            orm.Workout.user_id == user_id,
            orm.Workout.started_at >= _naive_utc(start_at),
            orm.Workout.started_at < _naive_utc(end_at),
        )
        if source is not None:
            statement = statement.where(orm.Workout.source == source)
        workouts = list(self.db.execute(
            statement.order_by(orm.Workout.started_at, orm.Workout.source, orm.Workout.workout_id).limit(limit)
        ).scalars().all())
        if not workouts:
            return []
        keys = [(row.source, row.workout_id) for row in workouts]
        max_points = 1_000_000
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            epoch = cast(func.strftime("%s", orm.WorkoutMetricSample.timestamp), Integer)
        elif dialect == "postgresql":
            epoch = cast(func.extract("epoch", orm.WorkoutMetricSample.timestamp), Integer)
        else:
            epoch = None
        if epoch is not None:
            bucket = cast(epoch / 5, Integer)
            statement = (
                select(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    func.min(orm.WorkoutMetricSample.timestamp).label("timestamp"),
                    func.avg(orm.WorkoutMetricSample.value).label("value"),
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                )
                .where(
                    orm.WorkoutMetricSample.user_id == user_id,
                    orm.WorkoutMetricSample.metric == metric,
                    tuple_(
                        orm.WorkoutMetricSample.source,
                        orm.WorkoutMetricSample.workout_id,
                    ).in_(keys),
                )
                .group_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                    bucket,
                )
                .order_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    func.min(orm.WorkoutMetricSample.timestamp),
                )
                .limit(max_points + 1)
            )
            sample_rows = list(self.db.execute(statement))
        else:
            sample_rows = list(self.db.execute(
                select(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.timestamp,
                    orm.WorkoutMetricSample.value,
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                ).where(
                    orm.WorkoutMetricSample.user_id == user_id,
                    orm.WorkoutMetricSample.metric == metric,
                    tuple_(
                        orm.WorkoutMetricSample.source,
                        orm.WorkoutMetricSample.workout_id,
                    ).in_(keys),
                ).order_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.timestamp,
                ).limit(max_points + 1)
            ))
        truncated = len(sample_rows) > max_points
        sample_rows = sample_rows[:max_points]
        samples_by_key: dict[tuple[str, str], list[HeartRatePoint]] = {}
        for row in sample_rows:
            timestamp = row.timestamp.replace(tzinfo=timezone.utc)
            samples_by_key.setdefault((row.source, row.workout_id), []).append(
                HeartRatePoint(
                    timestamp=timestamp,
                    value=float(row.value),
                    source=row.source,
                    source_scope=row.source_scope,
                    device_id=row.device_id or None,
                    unit=row.unit,
                )
            )

        def parse_datetime(value):
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return None

        output = []
        for row in workouts:
            data = row.data if isinstance(row.data, dict) else {}
            detail = row.detail if isinstance(row.detail, dict) else {}
            started = row.started_at.replace(tzinfo=timezone.utc) if row.started_at else None
            ended = parse_datetime(data.get("ended_at"))
            duration = data.get("duration")
            try:
                duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            pauses = []
            for item in (detail.get("pauses") or []):
                if not isinstance(item, dict):
                    continue
                pause_start = parse_datetime(item.get("started_at"))
                try:
                    pause_duration = float(item.get("duration_seconds") or 0)
                except (TypeError, ValueError):
                    continue
                if pause_start is not None and pause_duration > 0:
                    pauses.append(PauseInterval(pause_start, pause_duration))
            output.append(LoadWorkout(
                source=row.source,
                workout_id=row.workout_id,
                started_at=started,
                ended_at=ended,
                duration_minutes=duration,
                heart_rate=tuple(samples_by_key.get((row.source, row.workout_id), [])),
                pauses=tuple(pauses),
            ))
        return LoadWorkoutBatch(output, truncated=truncated)

    def workout(
        self, user_id: str, workout_id: str, source: str = "zepp"
    ) -> orm.Workout | None:
        return self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()

    # ---- 持久同步账本 ----

    def create_or_reuse_sync_attempt(
        self,
        user_id: str,
        source: str = "zepp",
        trigger: str = "manual",
        trigger_ref: str | None = None,
        plan_version: str = "zepp-sync-v1",
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        timezone_name: str = "UTC",
        options: dict | None = None,
        deadline_at: datetime | None = None,
        manifest: list[object] | None = None,
        attempt_id: str | None = None,
    ) -> orm.SyncAttempt:
        """Create one active attempt, or return the user's existing active attempt."""
        now = datetime.utcnow()
        if window_start is None or window_end is None:
            raise ValueError("同步尝试必须提供非空时间窗口")
        window_start = _naive_utc(window_start)
        window_end = _naive_utc(window_end)
        if window_end <= window_start:
            raise ValueError("同步尝试结束时间必须晚于开始时间")
        deadline_at = _naive_utc(deadline_at) if deadline_at else None
        options = dict(options or {})
        request_payload = {
            "source": source,
            "trigger": trigger,
            "trigger_ref": trigger_ref,
            "plan_version": plan_version,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "timezone": timezone_name,
            "options": options,
        }
        request_key = hashlib.sha256(
            json.dumps(
                request_payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()
        self.upsert_user(user_id, source=source)

        if attempt_id:
            existing = self.db.get(orm.SyncAttempt, attempt_id)
            if existing is not None:
                if existing.user_id != user_id or existing.source != source:
                    raise ValueError("同步尝试不属于当前用户或数据源")
                self._ensure_sync_chunks(existing, manifest)
                return existing

        active = self.db.execute(
            select(orm.SyncAttempt).where(
                orm.SyncAttempt.user_id == user_id,
                orm.SyncAttempt.source == source,
                orm.SyncAttempt.request_key == request_key,
                orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
            ).order_by(orm.SyncAttempt.created_at.desc()).with_for_update()
        ).scalars().first()
        if active is not None:
            self._ensure_sync_chunks(active, manifest)
            return active

        values = dict(
            id=attempt_id or uuid4().hex,
            user_id=user_id,
            source=source,
            trigger=trigger,
            trigger_ref=trigger_ref,
            plan_version=plan_version,
            request_key=request_key,
            window_start=window_start,
            window_end=window_end,
            timezone=timezone_name,
            options=options,
            status="queued",
            deadline_at=deadline_at,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(orm.SyncAttempt(**values))
                self.db.flush()
        except IntegrityError:
            active = self.db.execute(
                select(orm.SyncAttempt).where(
                    orm.SyncAttempt.user_id == user_id,
                    orm.SyncAttempt.source == source,
                    orm.SyncAttempt.request_key == request_key,
                    orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                ).order_by(orm.SyncAttempt.created_at.desc())
            ).scalars().first()
            if active is None:
                raise
            self._ensure_sync_chunks(active, manifest)
            return active
        attempt = self.db.get(orm.SyncAttempt, values["id"])
        assert attempt is not None
        self._ensure_sync_chunks(attempt, manifest)
        return attempt

    def create_sync_attempt(self, *args, **kwargs) -> orm.SyncAttempt:
        """Compatibility spelling for the coordinator-facing create operation."""
        return self.create_or_reuse_sync_attempt(*args, **kwargs)

    def create_or_reuse_attempt(self, *args, **kwargs) -> orm.SyncAttempt:
        return self.create_or_reuse_sync_attempt(*args, **kwargs)

    def _ensure_sync_chunks(
        self, attempt: orm.SyncAttempt, manifest: list[object] | None
    ) -> None:
        if manifest:
            values = []
            for item in manifest:
                if isinstance(item, dict):
                    get = item.get
                else:
                    get = lambda key, default=None: getattr(item, key, default)
                stable_key = get("stable_key") or get("key")
                stream = get("stream")
                if not stable_key or not stream:
                    raise ValueError("同步 chunk 必须包含 stable_key 和 stream")
                values.append({
                    "attempt_id": attempt.id,
                    "stable_key": str(stable_key),
                    "stream": str(stream),
                    "health_stream": get("health_stream"),
                    "partition": str(get("partition", "") or ""),
                    "ordinal": int(get("ordinal", 0) or 0),
                    "window_start": (
                        _naive_utc(get("window_start")) if get("window_start") else None
                    ),
                    "window_end": (
                        _naive_utc(get("window_end")) if get("window_end") else None
                    ),
                    "cursor": get("cursor"),
                    "allow_unavailable": bool(get("allow_unavailable", False)),
                    "status": "queued",
                    "fetch_status": get("fetch_status", "never"),
                    "parse_status": get("parse_status", "never"),
                    "write_status": get("write_status", "never"),
                    "stages": dict(get("stages", {}) or {}),
                })
            dialect = self.db.get_bind().dialect.name
            if dialect in ("sqlite", "postgresql"):
                if dialect == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert
                else:
                    from sqlalchemy.dialects.postgresql import insert
                statement = insert(orm.SyncChunk).values(values)
                self.db.execute(statement.on_conflict_do_nothing(
                    index_elements=["attempt_id", "stable_key"]
                ))
            else:
                for value in values:
                    exists = self.db.execute(select(orm.SyncChunk).where(
                        orm.SyncChunk.attempt_id == attempt.id,
                        orm.SyncChunk.stable_key == value["stable_key"],
                    )).scalar_one_or_none()
                    if exists is None:
                        self.db.add(orm.SyncChunk(**value))
            self.db.flush()
        attempt.chunk_count = self.db.execute(
            select(func.count(orm.SyncChunk.id)).where(orm.SyncChunk.attempt_id == attempt.id)
        ).scalar_one()
        attempt.updated_at = datetime.utcnow()
        self.db.flush()

    def sync_attempt(self, attempt_id: str, user_id: str | None = None) -> orm.SyncAttempt | None:
        row = self.db.get(orm.SyncAttempt, attempt_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    def get_sync_attempt(self, attempt_id: str, user_id: str | None = None) -> orm.SyncAttempt | None:
        return self.sync_attempt(attempt_id, user_id=user_id)

    def sync_attempts(
        self, user_id: str, source: str | None = None, statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[orm.SyncAttempt]:
        statement = select(orm.SyncAttempt).where(orm.SyncAttempt.user_id == user_id)
        if source is not None:
            statement = statement.where(orm.SyncAttempt.source == source)
        if statuses:
            statement = statement.where(orm.SyncAttempt.status.in_(statuses))
        return list(self.db.execute(
            statement.order_by(orm.SyncAttempt.created_at.desc()).limit(limit)
        ).scalars().all())

    def sync_chunks(
        self, attempt_id: str, user_id: str | None = None, status: str | None = None
    ) -> list[orm.SyncChunk]:
        statement = select(orm.SyncChunk).where(orm.SyncChunk.attempt_id == attempt_id)
        if user_id is not None:
            statement = statement.join(
                orm.SyncAttempt, orm.SyncAttempt.id == orm.SyncChunk.attempt_id
            ).where(orm.SyncAttempt.user_id == user_id)
        if status is not None:
            statement = statement.where(orm.SyncChunk.status == status)
        return list(self.db.execute(
            statement.order_by(orm.SyncChunk.ordinal, orm.SyncChunk.stable_key)
        ).scalars().all())

    def get_sync_chunks(
        self, attempt_id: str, user_id: str | None = None, status: str | None = None
    ) -> list[orm.SyncChunk]:
        return self.sync_chunks(attempt_id, user_id=user_id, status=status)

    def training_history_coverage(
        self,
        user_id: str,
        start: date,
        end: date,
        as_of: datetime,
    ) -> dict:
        """Report conservative, attempt-proven workout-history coverage.

        Coverage is evidence from one completed attempt at a time.  A local
        workout row is deliberately never used as proof: every expected Zepp
        sport partition and every pagination successor must have completed
        fetch, parse, and write stages in that same attempt.
        """
        if end < start:
            raise ValueError("训练历史覆盖窗口无效")
        as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        as_of_utc = as_of_utc.astimezone(timezone.utc)
        as_of_naive = _naive_utc(as_of_utc)
        period_start = start.isoformat()
        period_end = end.isoformat()
        target_days = {
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        }
        limitations: list[str] = []

        period_start_at, _ = local_day_utc_bounds(start)
        _, period_end_at = local_day_utc_bounds(end)
        period_start_utc = _naive_utc(period_start_at)
        period_end_utc = _naive_utc(period_end_at)

        # This is intentionally two bounded set queries, not one query per
        # attempt or partition.  Future terminal evidence is excluded here.
        attempts = list(self.db.execute(
            select(orm.SyncAttempt).where(
                orm.SyncAttempt.user_id == user_id,
                orm.SyncAttempt.source == "zepp",
                orm.SyncAttempt.status.in_(("succeeded", "partial", "failed")),
                orm.SyncAttempt.window_start < period_end_utc,
                orm.SyncAttempt.window_end > period_start_utc,
                orm.SyncAttempt.finished_at <= as_of_naive,
            ).order_by(orm.SyncAttempt.finished_at.desc(), orm.SyncAttempt.created_at.desc())
            .limit(64)
        ).scalars().all())
        attempt_ids = [row.id for row in attempts]
        chunks = []
        oversized_attempts: set[str] = set()
        if attempt_ids:
            chunk_counts = self.db.execute(
                select(orm.SyncChunk.attempt_id, func.count(orm.SyncChunk.id))
                .where(
                    orm.SyncChunk.attempt_id.in_(attempt_ids),
                    orm.SyncChunk.stream == "workouts",
                )
                .group_by(orm.SyncChunk.attempt_id)
            ).all()
            oversized_attempts = {
                attempt_id for attempt_id, count in chunk_counts if count > 4096
            }
            chunks = list(self.db.execute(
                select(orm.SyncChunk).where(
                    orm.SyncChunk.attempt_id.in_(attempt_ids),
                    orm.SyncChunk.stream == "workouts",
                ).order_by(orm.SyncChunk.attempt_id, orm.SyncChunk.partition, orm.SyncChunk.ordinal)
                .limit(4096)
            ).scalars().all())

        chunks_by_attempt: dict[str, list[orm.SyncChunk]] = {}
        for chunk in chunks:
            chunks_by_attempt.setdefault(chunk.attempt_id, []).append(chunk)

        # Import lazily to keep the storage layer's normal import graph small.
        from vitalis.connectors.zepp.client import SPORTS

        verified_days: set[date] = set()
        saw_relevant_attempt = False
        saw_partial_evidence = False
        last_synced_at: datetime | None = None
        as_of_local_day = local_day(as_of_utc)
        for attempt in attempts:
            attempt_chunks = chunks_by_attempt.get(attempt.id, [])
            if not attempt_chunks:
                continue
            saw_relevant_attempt = True
            if attempt.id in oversized_attempts:
                limitations.append("attempt 的 workouts 分页 chunk 超出有界查询，未确认覆盖")
                saw_partial_evidence = True
                continue
            # Never discard a future or unfinished successor.  Its presence
            # means the attempt cannot prove the complete pagination chain.
            if any(
                chunk.finished_at is None or chunk.finished_at > as_of_naive
                for chunk in attempt_chunks
            ):
                limitations.append("attempt 存在未完成或晚于 as_of 的 workouts 分页 chunk")
                saw_partial_evidence = True
                continue
            by_partition: dict[str, list[orm.SyncChunk]] = {}
            for chunk in attempt_chunks:
                if chunk.partition in SPORTS:
                    by_partition.setdefault(chunk.partition, []).append(chunk)
            complete = True
            for sport in SPORTS:
                sport_chunks = by_partition.get(sport, [])
                if not sport_chunks or any(
                    chunk.status != "succeeded"
                    or chunk.fetch_status != "success"
                    or chunk.parse_status != "success"
                    or chunk.write_status != "success"
                    for chunk in sport_chunks
                ):
                    complete = False
                    break
            if not complete:
                if any(
                    chunk.status == "succeeded"
                    and chunk.fetch_status == "success"
                    and chunk.parse_status == "success"
                    and chunk.write_status == "success"
                    for chunk in attempt_chunks
                ):
                    saw_partial_evidence = True
                continue

            window_start = max(attempt.window_start, period_start_utc)
            window_end = min(attempt.window_end, period_end_utc)
            if window_start >= window_end:
                continue
            confirmed_for_attempt: set[date] = set()
            for day in target_days:
                if day > as_of_local_day:
                    continue
                day_start, day_end = local_day_utc_bounds(day)
                required_end = day_end
                if day == as_of_local_day:
                    required_end = min(day_end, as_of_utc)
                if (
                    window_start <= _naive_utc(day_start)
                    and window_end >= _naive_utc(required_end)
                ):
                    confirmed_for_attempt.add(day)
                    verified_days.add(day)
            if confirmed_for_attempt:
                for chunk in attempt_chunks:
                    candidate = chunk.finished_at.replace(tzinfo=timezone.utc)
                    if last_synced_at is None or candidate > last_synced_at:
                        last_synced_at = candidate

        if not attempts:
            limitations.append("没有截至 as_of 的 Zepp 同步 attempt 证据")
        elif not saw_relevant_attempt:
            limitations.append("匹配的 attempt 没有 workouts 分区记录")
        if saw_relevant_attempt and len(verified_days) < len(target_days):
            limitations.append("至少一个窗口缺少完整的 required sport 分区或分页 chunk")
        if as_of_utc.date() < end:
            limitations.append("窗口末端晚于 as_of，未来日期不计入覆盖")
        if not verified_days:
            status = "PARTIAL" if saw_partial_evidence else "UNKNOWN"
        elif verified_days == target_days:
            status = "COMPLETE"
        else:
            status = "PARTIAL"
        return {
            "status": status,
            "period_start": period_start,
            "period_end": period_end,
            "verified_days": [day.isoformat() for day in sorted(verified_days)],
            "last_synced_at": (
                last_synced_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if last_synced_at else None
            ),
            "limitations": limitations,
        }

    def sync_chunk(
        self, attempt_id: str, stable_key: str, user_id: str | None = None
    ) -> orm.SyncChunk | None:
        return self.db.execute(
            select(orm.SyncChunk).join(
                orm.SyncAttempt, orm.SyncAttempt.id == orm.SyncChunk.attempt_id
            ).where(
                orm.SyncChunk.attempt_id == attempt_id,
                orm.SyncChunk.stable_key == stable_key,
                *([orm.SyncAttempt.user_id == user_id] if user_id is not None else []),
            )
        ).scalar_one_or_none()

    def claim_sync_attempt(
        self, attempt_id: str, lease_token: str, *, now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        other = orm.SyncAttempt.__table__.alias("other_sync_attempt")
        no_other_running = ~select(other.c.id).where(
            other.c.user_id == orm.SyncAttempt.user_id,
            other.c.source == orm.SyncAttempt.source,
            other.c.status == "running",
            other.c.id != orm.SyncAttempt.id,
        ).exists()
        try:
            with self.db.begin_nested():
                result = self.db.execute(update(orm.SyncAttempt).where(
                    orm.SyncAttempt.id == attempt_id,
                    no_other_running,
                    orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                    orm.SyncAttempt.cancel_requested_at.is_(None),
                    or_(orm.SyncAttempt.next_retry_at.is_(None), orm.SyncAttempt.next_retry_at <= now),
                    or_(orm.SyncAttempt.lease_expires_at.is_(None), orm.SyncAttempt.lease_expires_at <= now),
                ).values(
                    status="running",
                    lease_token=lease_token,
                    lease_epoch=orm.SyncAttempt.lease_epoch + 1,
                    attempt_count=orm.SyncAttempt.attempt_count + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=func.coalesce(orm.SyncAttempt.started_at, now),
                    next_retry_at=None,
                    updated_at=now,
                ))
                self.db.flush()
        except IntegrityError:
            # The partial unique running-attempt index is the final cross-worker fence.
            return False
        return bool(result.rowcount)

    def claim_attempt(self, *args, **kwargs) -> bool:
        return self.claim_sync_attempt(*args, **kwargs)

    def takeover_expired_attempt(self, *args, **kwargs) -> bool:
        return self.claim_sync_attempt(*args, **kwargs)

    def acquire_sync_attempt_lease(
        self, attempt_id: str, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> SyncLease | None:
        token = uuid4().hex
        if not self.claim_sync_attempt(
            attempt_id, token, now=now, lease_seconds=lease_seconds
        ):
            return None
        row = self.sync_attempt(attempt_id)
        assert row is not None
        return SyncLease(row.id, token, row.lease_epoch, row.lease_expires_at)

    def claim_sync_chunk(
        self, chunk_id: int, lease_token: str, *, now: datetime | None = None,
        lease_seconds: int = 60, attempt_lease_token: str | None = None,
        attempt_lease_epoch: int | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        active_conditions = [
            orm.SyncAttempt.id == orm.SyncChunk.attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.cancel_requested_at.is_(None),
        ]
        if attempt_lease_token is not None:
            active_conditions.extend([
                orm.SyncAttempt.lease_token == attempt_lease_token,
                orm.SyncAttempt.lease_epoch == attempt_lease_epoch,
                orm.SyncAttempt.lease_expires_at > now,
            ])
        active_attempt = select(orm.SyncAttempt.id).where(*active_conditions)
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.attempt_id.in_(active_attempt),
            orm.SyncChunk.status.in_(("queued", "running", "retry_wait")),
            or_(orm.SyncChunk.next_retry_at.is_(None), orm.SyncChunk.next_retry_at <= now),
            or_(orm.SyncChunk.lease_expires_at.is_(None), orm.SyncChunk.lease_expires_at <= now),
        ).values(
            status="running",
            lease_token=lease_token,
            lease_epoch=orm.SyncChunk.lease_epoch + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=func.coalesce(orm.SyncChunk.started_at, now),
            next_retry_at=None,
            attempt_count=orm.SyncChunk.attempt_count + 1,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def claim_chunk(self, *args, **kwargs) -> bool:
        return self.claim_sync_chunk(*args, **kwargs)

    def takeover_expired_chunk(self, *args, **kwargs) -> bool:
        return self.claim_sync_chunk(*args, **kwargs)

    def acquire_sync_chunk_lease(
        self, chunk_id: int, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> SyncLease | None:
        token = uuid4().hex
        if not self.claim_sync_chunk(chunk_id, token, now=now, lease_seconds=lease_seconds):
            return None
        row = self.db.get(orm.SyncChunk, chunk_id)
        assert row is not None
        return SyncLease(row.id, token, row.lease_epoch, row.lease_expires_at)

    def renew_sync_attempt_lease(
        self, attempt_id: str, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, lease_seconds: int = 60
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
            orm.SyncAttempt.lease_expires_at > now,
        ).values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def renew_attempt_lease(self, *args, **kwargs) -> bool:
        return self.renew_sync_attempt_lease(*args, **kwargs)

    def renew_sync_chunk_lease(
        self, chunk_id: int, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, lease_seconds: int = 60
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
            orm.SyncChunk.lease_expires_at > now,
        ).values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def renew_chunk_lease(self, *args, **kwargs) -> bool:
        return self.renew_sync_chunk_lease(*args, **kwargs)

    def release_sync_attempt_lease(
        self, attempt_id: str, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, status: str = "queued"
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def release_attempt_lease(self, *args, **kwargs) -> bool:
        return self.release_sync_attempt_lease(*args, **kwargs)

    def release_sync_chunk_lease(
        self, chunk_id: int, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, status: str = "queued"
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def release_chunk_lease(self, *args, **kwargs) -> bool:
        return self.release_sync_chunk_lease(*args, **kwargs)

    def request_sync_cancel(
        self, attempt_id: str, *, now: datetime | None = None
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
            orm.SyncAttempt.cancel_requested_at.is_(None),
        ).values(
            cancel_requested_at=now,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def request_cancel(self, *args, **kwargs) -> bool:
        return self.request_sync_cancel(*args, **kwargs)

    def cancel_sync_attempt(
        self, attempt_id: str, *, now: datetime | None = None,
        lease_token: str | None = None, lease_epoch: int | None = None
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        conditions = [
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
            orm.SyncAttempt.cancel_requested_at.is_not(None),
        ]
        if lease_token is not None:
            conditions.extend([
                orm.SyncAttempt.lease_token == lease_token,
                orm.SyncAttempt.lease_epoch == lease_epoch,
            ])
        else:
            conditions.append(or_(
                orm.SyncAttempt.status.in_(("queued", "retry_wait")),
                orm.SyncAttempt.lease_expires_at <= now,
            ))
        result = self.db.execute(update(orm.SyncAttempt).where(*conditions).values(
            status="cancelled",
            lease_token=None,
            lease_expires_at=None,
            cancelled_at=now,
            finished_at=now,
            updated_at=now,
        ))
        if result.rowcount:
            self.db.execute(update(orm.SyncChunk).where(
                orm.SyncChunk.attempt_id == attempt_id,
                orm.SyncChunk.status.in_(("queued", "retry_wait")),
            ).values(
                status="cancelled",
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            ))
        self.db.flush()
        return bool(result.rowcount)

    def _finalize_sync_chunk(
        self, chunk_id: int, lease_token: str, lease_epoch: int, status: str,
        *, now: datetime | None = None, next_retry_at: datetime | None = None,
        stages: dict | None = None, raw_records: int = 0, records_written: int = 0,
        error_kind: str | None = None, error: str | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        if status == "retry_wait" and next_retry_at is None:
            raise ValueError("重试等待 chunk 必须提供 next_retry_at")
        values = {
            "status": status,
            "lease_token": None,
            "lease_expires_at": None,
            "next_retry_at": _naive_utc(next_retry_at) if next_retry_at else None,
            "raw_records": raw_records,
            "records_written": records_written,
            "error_kind": error_kind,
            "error": error[:2000] if error else None,
            "finished_at": now if status in ("succeeded", "unavailable", "failed", "cancelled") else None,
            "updated_at": now,
        }
        if stages is not None:
            values["stages"] = dict(stages)
            for stage_name in ("fetch_status", "parse_status", "write_status"):
                if stage_name in stages:
                    values[stage_name] = stages[stage_name]
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
            *([orm.SyncChunk.allow_unavailable.is_(True)] if status == "unavailable" else []),
        ).values(**values))
        self.db.flush()
        return bool(result.rowcount)

    def finalize_sync_chunk_success(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "succeeded", **kwargs)

    def finalize_sync_chunk_unavailable(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "unavailable", **kwargs)

    def finalize_sync_chunk_retry(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "retry_wait", **kwargs)

    def finalize_sync_chunk_failure(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "failed", **kwargs)

    def finalize_chunk(self, chunk_id, lease_token, lease_epoch, status, **kwargs) -> bool:
        return self._finalize_sync_chunk(
            chunk_id, lease_token, lease_epoch, status, **kwargs
        )

    def _finalize_sync_attempt(
        self, attempt_id: str, lease_token: str, lease_epoch: int, status: str,
        *, now: datetime | None = None, next_retry_at: datetime | None = None,
        error_kind: str | None = None, error: str | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        if status == "retry_wait" and next_retry_at is None:
            raise ValueError("重试等待 attempt 必须提供 next_retry_at")
        aggregate = self.aggregate_sync_attempt(attempt_id)
        if status == "succeeded" and (
            not aggregate.complete or aggregate.failed_chunks
        ):
            return False
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.cancel_requested_at.is_(None),
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=_naive_utc(next_retry_at) if next_retry_at else None,
            retry_count=(
                orm.SyncAttempt.retry_count + 1 if status == "retry_wait"
                else orm.SyncAttempt.retry_count
            ),
            completed_count=aggregate.completed_count,
            chunk_count=aggregate.total_chunks,
            raw_records=aggregate.raw_records,
            records_written=aggregate.records_written,
            error_kind=error_kind,
            error=error[:4000] if error else None,
            finished_at=(
                now if status in ("succeeded", "partial", "failed", "needs_reauth", "cancelled")
                else None
            ),
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def finalize_sync_attempt_success(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "succeeded", **kwargs)

    def finalize_sync_attempt_retry(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "retry_wait", **kwargs)

    def finalize_sync_attempt_failure(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "failed", **kwargs)

    def finalize_sync_attempt_partial(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "partial", **kwargs)

    def finalize_sync_attempt_needs_reauth(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "needs_reauth", **kwargs)

    def finalize_attempt_success(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_success(*args, **kwargs)

    def finalize_attempt_retry(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_retry(*args, **kwargs)

    def finalize_attempt_failure(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_failure(*args, **kwargs)

    def finalize_attempt(self, attempt_id, lease_token, lease_epoch, status, **kwargs) -> bool:
        return self._finalize_sync_attempt(
            attempt_id, lease_token, lease_epoch, status, **kwargs
        )

    def aggregate_sync_attempt(self, attempt_id: str) -> SyncAttemptAggregate:
        attempt = self.db.get(orm.SyncAttempt, attempt_id)
        if attempt is None:
            raise ValueError("同步尝试不存在")
        rows = self.sync_chunks(attempt_id)
        counts = {status: 0 for status in (
            "succeeded", "unavailable", "failed", "cancelled",
            "retry_wait", "running", "queued"
        )}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return SyncAttemptAggregate(
            attempt_id=attempt.id,
            status=attempt.status,
            total_chunks=len(rows),
            succeeded_chunks=counts["succeeded"],
            unavailable_chunks=counts["unavailable"],
            failed_chunks=counts["failed"],
            cancelled_chunks=counts["cancelled"],
            retrying_chunks=counts["retry_wait"],
            running_chunks=counts["running"],
            queued_chunks=counts["queued"],
            raw_records=sum(row.raw_records or 0 for row in rows),
            records_written=sum(row.records_written or 0 for row in rows),
        )

    def sync_attempt_aggregate(self, *args, **kwargs) -> SyncAttemptAggregate:
        return self.aggregate_sync_attempt(*args, **kwargs)

    # ---- 同步数据健康 ----

    def save_sync_stream_state(
        self,
        user_id: str,
        stream: str,
        *,
        fetch_status: str,
        parse_status: str,
        write_status: str,
        fetched_at: datetime | None,
        parsed_at: datetime | None,
        written_at: datetime | None,
        raw_records: int,
        records_written: int,
        error_kind: str | None = None,
        message: str | None = None,
        source: str = "zepp",
        attempt_id: str | None = None,
    ) -> orm.SyncStreamState:
        incoming_attempt = None
        if attempt_id is not None:
            incoming_attempt = self.sync_attempt(attempt_id, user_id=user_id)
            if incoming_attempt is None or incoming_attempt.source != source:
                raise ValueError("同步投影的 attempt 不属于当前用户或数据源")
        row = self.db.execute(select(orm.SyncStreamState).where(
            orm.SyncStreamState.user_id == user_id,
            orm.SyncStreamState.source == source,
            orm.SyncStreamState.stream == stream,
        )).scalar_one_or_none()
        if row is None:
            row = orm.SyncStreamState(
                user_id=user_id, source=source, stream=stream, attempt_id=attempt_id
            )
            self.db.add(row)
        elif attempt_id is not None and row.attempt_id is not None:
            current_attempt = self.sync_attempt(row.attempt_id)
            if (
                current_attempt is not None
                and incoming_attempt is not None
                and current_attempt.created_at > incoming_attempt.created_at
            ):
                return row
        if attempt_id is not None:
            row.attempt_id = attempt_id
        row.fetch_status = fetch_status
        row.parse_status = parse_status
        row.write_status = write_status
        row.fetched_at = _naive_utc(fetched_at) if fetched_at else None
        row.parsed_at = _naive_utc(parsed_at) if parsed_at else None
        row.written_at = _naive_utc(written_at) if written_at else None
        row.last_sample_at = self.latest_stream_sample_at(
            user_id, stream, source=source
        )
        row.raw_records = raw_records
        row.records_written = records_written
        row.error_kind = error_kind
        row.message = message[:1000] if message else None
        row.updated_at = datetime.utcnow()
        self.db.flush()
        return row

    def sync_stream_states(self, user_id: str) -> list[orm.SyncStreamState]:
        return list(self.db.execute(
            select(orm.SyncStreamState).where(
                orm.SyncStreamState.user_id == user_id
            ).order_by(orm.SyncStreamState.stream)
        ).scalars().all())

    def latest_stream_sample_at(
        self, user_id: str, stream: str, source: str = "zepp"
    ) -> datetime | None:
        metric_by_stream = {
            "heart_rate": "heart_rate",
            "heart_rate/minute_endpoint": "heart_rate",
            "hrv": "hrv_sdnn",
            "wellness/hrv_rmssd": "hrv_rmssd",
            "wellness/spo2_point": "spo2",
            "wellness/spo2_odi": "spo2_odi",
            "wellness/spo2_osa": "spo2_apnea_low",
            "wellness/respiratory_rate": "respiratory_rate",
            "wellness/all_day_stress": "stress",
            "wellness/pai": "pai_daily",
            "wellness/lactate_threshold": "lactate_threshold_hr",
        }
        metric = metric_by_stream.get(stream)
        if metric in {
            "heart_rate", "hrv_sdnn", "hrv_rmssd", "spo2", "spo2_apnea_low",
            "stress",
        }:
            latest_sample = self.db.execute(
                select(func.max(orm.MetricSample.timestamp)).where(
                    orm.MetricSample.user_id == user_id,
                    orm.MetricSample.source == source,
                    orm.MetricSample.metric == metric,
                )
            ).scalar_one_or_none()
            if latest_sample is not None or metric != "stress":
                return latest_sample
        if metric:
            value = self.db.execute(select(func.max(orm.DailyMetric.date)).where(
                orm.DailyMetric.user_id == user_id,
                orm.DailyMetric.source == source,
                orm.DailyMetric.metric == metric,
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream == "workout_detail":
            return self.db.execute(select(func.max(orm.WorkoutMetricSample.timestamp)).where(
                orm.WorkoutMetricSample.user_id == user_id,
                orm.WorkoutMetricSample.source == source,
            )).scalar_one_or_none()
        if stream == "workouts":
            return self.db.execute(select(func.max(orm.Workout.started_at)).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
            )).scalar_one_or_none()
        if stream == "sleep":
            value = self.db.execute(select(func.max(orm.SleepRecord.date)).where(
                orm.SleepRecord.user_id == user_id
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream == "daily_summary":
            value = self.db.execute(select(func.max(orm.DailyMetric.date)).where(
                orm.DailyMetric.user_id == user_id,
                orm.DailyMetric.source == source,
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream in {"dense_files", "heart_rate/dense_file"}:
            return self.db.execute(select(func.max(orm.DenseDataFile.end_utc)).where(
                orm.DenseDataFile.user_id == user_id,
                orm.DenseDataFile.source == source,
            )).scalar_one_or_none()
        return None

    def replace_strength_exercises(
        self,
        user_id: str,
        workout_id: str,
        exercises: list[StrengthExerciseRecord],
        workout_source: str = "zepp",
    ) -> list[StrengthExerciseRecord]:
        self.db.execute(delete(orm.StrengthExercise).where(
            orm.StrengthExercise.user_id == user_id,
            orm.StrengthExercise.workout_source == workout_source,
            orm.StrengthExercise.workout_id == workout_id,
        ))
        self.db.add_all([
            orm.StrengthExercise(**item.model_dump(mode="python"))
            for item in exercises
        ])
        self.db.flush()
        return exercises

    def strength_exercises_for_workouts(
        self, user_id: str, workout_ids: list[str], source: str = "zepp"
    ) -> dict[str, list[StrengthExerciseRecord]]:
        keyed = self.strength_exercises_for_workout_keys(
            user_id, [(source, workout_id) for workout_id in workout_ids]
        )
        return {
            workout_id: values
            for (_source, workout_id), values in keyed.items()
        }

    def strength_exercises_for_workout_keys(
        self, user_id: str, workout_keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], list[StrengthExerciseRecord]]:
        if not workout_keys:
            return {}
        rows = self.db.execute(select(orm.StrengthExercise).where(
            orm.StrengthExercise.user_id == user_id,
            tuple_(
                orm.StrengthExercise.workout_source,
                orm.StrengthExercise.workout_id,
            ).in_(workout_keys),
        ).order_by(
            orm.StrengthExercise.workout_source,
            orm.StrengthExercise.workout_id,
            orm.StrengthExercise.order,
        )).scalars().all()
        output: dict[tuple[str, str], list[StrengthExerciseRecord]] = {}
        for row in rows:
            key = (row.workout_source, row.workout_id)
            output.setdefault(key, []).append(StrengthExerciseRecord(
                id=row.id,
                user_id=row.user_id,
                workout_source=row.workout_source,
                workout_id=row.workout_id,
                order=row.order,
                exercise_name=row.exercise_name,
                exercise_id=row.exercise_id,
                session_focus=row.session_focus,
                movement_pattern=row.movement_pattern,
                movement_pattern_label=row.movement_pattern_label,
                muscle_groups=list(row.muscle_groups or []),
                muscle_group_labels=list(row.muscle_group_labels or []),
                sets=row.sets,
                repetitions=row.repetitions,
                weight_kg=row.weight_kg,
                rpe=row.rpe,
                rir=row.rir,
                rest_seconds=row.rest_seconds,
                source=row.source,
                confidence=row.confidence,
                confidence_label=row.confidence_label,
                created_at=row.created_at.replace(tzinfo=timezone.utc),
            ))
        return output

    def save_training_preferences(
        self, user_id: str, preferences: TrainingPreferenceInput
    ) -> TrainingPreferences:
        row = self.db.get(orm.TrainingPreference, user_id)
        values = preferences.model_dump(mode="python")
        if row is None:
            row = orm.TrainingPreference(user_id=user_id, **values)
            self.db.add(row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = datetime.utcnow()
        self.db.flush()
        return self.training_preferences(user_id)

    def patch_training_preferences(
        self, user_id: str, patch: TrainingPreferencePatch
    ) -> TrainingPreferences:
        current = self.training_preferences(user_id)
        merged = current.model_dump(
            mode="python",
            exclude={
                "user_id", "primary_goal", "primary_goal_label",
                "running_required", "strength_required", "updated_at",
            },
        )
        merged.update(patch.model_dump(mode="python", exclude_unset=True))
        validated = TrainingPreferenceInput.model_validate(merged)
        row = self.db.get(orm.TrainingPreference, user_id)
        values = validated.model_dump(mode="python")
        if row is None:
            row = orm.TrainingPreference(user_id=user_id, **values)
            self.db.add(row)
        else:
            for field, value in values.items():
                setattr(row, field, value)
        row.updated_at = datetime.utcnow()
        self.db.flush()
        return self.training_preferences(user_id)

    def training_preferences(self, user_id: str) -> TrainingPreferences:
        row = self.db.get(orm.TrainingPreference, user_id)
        if row is None:
            return TrainingPreferences(user_id=user_id)
        return TrainingPreferences(
            user_id=row.user_id,
            weekly_running_target=row.weekly_running_target,
            weekly_strength_target=row.weekly_strength_target,
            rotation_policy=row.rotation_policy,
            treadmill_available=row.treadmill_available,
            bad_weather_running_policy=row.bad_weather_running_policy,
            available_weekdays=list(row.available_weekdays or []),
            max_session_minutes=row.max_session_minutes,
            running_experience=row.running_experience,
            strength_experience=row.strength_experience,
            equipment=list(row.equipment or []),
            pain_or_injury_status=row.pain_or_injury_status,
            pain_or_injury_notes=row.pain_or_injury_notes,
            updated_at=(
                row.updated_at.replace(tzinfo=timezone.utc) if row.updated_at else None
            ),
        )

    # ---- 用户档案 ----

    def user_profile(self, user_id: str) -> UserProfile:
        row = self.db.get(orm.UserProfile, user_id)
        if row is None:
            return UserProfile(user_id=user_id)
        return _user_profile_from_row(row)

    def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profile(user_id)

    def patch_user_profile(
        self, user_id: str, patch: UserProfilePatch
    ) -> UserProfile:
        row = self.db.get(orm.UserProfile, user_id)
        current = _user_profile_from_row(row) if row else UserProfile(user_id=user_id)
        if patch.expected_revision != current.revision:
            raise ProfileRevisionConflict(patch.expected_revision, current.revision)

        values = patch.model_dump(
            mode="python", exclude={"expected_revision"}, exclude_unset=True
        )
        changed_fields = [
            field for field, value in values.items()
            if _profile_field_value(getattr(current, field)) != value
        ]
        if not changed_fields:
            return current

        revision = current.revision + 1
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        payload = current.model_dump(
            mode="json", exclude={"schema_version", "user_id", "revision"}
        )
        for field in changed_fields:
            value = values[field]
            if value is None:
                payload.pop(field, None)
                continue
            payload[field] = ProfileField(
                value=value,
                source=ProfileSource.USER_CONFIRMED,
                confidence=ConfidenceBand.HIGH,
                revision=revision,
                updated_at=now,
            ).model_dump(mode="json")

        revision_row = orm.UserProfileRevision(
            user_id=user_id,
            revision=revision,
            changed_fields=changed_fields,
            payload=payload,
            created_at=naive_now,
        )
        if row is None:
            try:
                with self.db.begin_nested():
                    self.db.add(orm.UserProfile(
                        user_id=user_id,
                        payload=payload,
                        revision=revision,
                        schema_version="1.0",
                        updated_at=naive_now,
                    ))
                    self.db.add(revision_row)
                    self.db.flush()
            except IntegrityError as exc:
                actual = self.db.get(orm.UserProfile, user_id)
                raise ProfileRevisionConflict(
                    patch.expected_revision,
                    actual.revision if actual is not None else revision,
                ) from exc
        else:
            result = self.db.execute(
                update(orm.UserProfile)
                .where(
                    orm.UserProfile.user_id == user_id,
                    orm.UserProfile.revision == patch.expected_revision,
                )
                .values(
                    payload=payload,
                    revision=revision,
                    schema_version="1.0",
                    updated_at=naive_now,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                self.db.expire(row)
                actual = self.db.get(orm.UserProfile, user_id)
                raise ProfileRevisionConflict(
                    patch.expected_revision,
                    actual.revision if actual is not None else revision,
                )
            self.db.add(revision_row)
            self.db.flush()
        updated = self.db.get(orm.UserProfile, user_id)
        assert updated is not None
        self.db.refresh(updated)
        return _user_profile_from_row(updated)

    def patch_profile(self, user_id: str, patch: UserProfilePatch) -> UserProfile:
        return self.patch_user_profile(user_id, patch)

    # ---- 健康智能事件 ----

    def active_health_events(self, user_id: str) -> list[HealthEvent]:
        rows = self.db.execute(select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id,
            orm.HealthEventRecord.lifecycle != "RESOLVED",
        )).scalars().all()
        return [_event_from_row(row) for row in rows]

    def save_health_event(self, user_id: str, event: HealthEvent) -> HealthEvent:
        row = self.db.get(orm.HealthEventRecord, event.id)
        if row is None:
            row = orm.HealthEventRecord(id=event.id, user_id=user_id)
            self.db.add(row)
        if row.user_id != user_id:
            raise ValueError("健康事件 ID 与用户不匹配")
        payload = event.model_dump(mode="json")
        row.event_type = event.type
        row.metric = event.metric
        row.start_date = event.start_date
        row.end_date = event.end_date
        row.lifecycle = event.lifecycle.value
        row.last_observed_date = event.last_observed_date
        row.last_evaluated_date = event.last_evaluated_date or event.end_date
        row.resolved_at = event.resolved_at
        payload["acknowledged"] = row.acknowledged_at is not None
        payload["acknowledged_at"] = (
            row.acknowledged_at.isoformat() if row.acknowledged_at else None
        )
        row.payload = payload
        self.db.flush()
        return _event_from_row(row)

    def save_event_observation(
        self, observation: HealthEventObservation
    ) -> HealthEventObservation:
        row = orm.HealthEventObservation(
            **observation.model_dump(mode="python")
        )
        row.previous_lifecycle = (
            observation.previous_lifecycle.value
            if observation.previous_lifecycle else None
        )
        row.lifecycle = observation.lifecycle.value
        row.created_at = _naive_utc(observation.created_at)
        self.db.add(row)
        self.db.flush()
        return observation

    def event_observations(
        self, user_id: str, event_id: str
    ) -> list[HealthEventObservation]:
        rows = self.db.execute(select(orm.HealthEventObservation).where(
            orm.HealthEventObservation.user_id == user_id,
            orm.HealthEventObservation.event_id == event_id,
        ).order_by(orm.HealthEventObservation.date, orm.HealthEventObservation.created_at)).scalars().all()
        return [HealthEventObservation.model_validate(row, from_attributes=True) for row in rows]

    def event_observations_range(
        self, user_id: str, start: date, end: date
    ) -> list[HealthEventObservation]:
        rows = self.db.execute(select(orm.HealthEventObservation).where(
            orm.HealthEventObservation.user_id == user_id,
            orm.HealthEventObservation.date.between(start, end),
        ).order_by(orm.HealthEventObservation.date.desc(), orm.HealthEventObservation.created_at.desc())).scalars().all()
        return [HealthEventObservation.model_validate(row, from_attributes=True) for row in rows]

    def health_events(
        self,
        user_id: str,
        start: date,
        end: date,
        event_type: str | None = None,
    ) -> list[HealthEvent]:
        statement = select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id,
            or_(
                (
                    (orm.HealthEventRecord.end_date >= start)
                    & (orm.HealthEventRecord.start_date <= end)
                ),
                orm.HealthEventRecord.resolved_at.between(start, end),
            ),
        )
        if event_type:
            statement = statement.where(orm.HealthEventRecord.event_type == event_type)
        rows = self.db.execute(
            statement.order_by(orm.HealthEventRecord.end_date.desc(), orm.HealthEventRecord.id)
        ).scalars().all()
        output = []
        for row in rows:
            output.append(_event_from_row(row))
        return output

    def health_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        row = self.db.get(orm.HealthEventRecord, event_id)
        if row is None or row.user_id != user_id:
            return None
        return _event_from_row(row)

    def acknowledge_health_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        row = self.db.get(orm.HealthEventRecord, event_id)
        if row is None or row.user_id != user_id:
            return None
        if row.acknowledged_at is None:
            row.acknowledged_at = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = dict(row.payload or {})
        payload["acknowledged"] = True
        payload["acknowledged_at"] = row.acknowledged_at.isoformat()
        row.payload = payload
        self.db.flush()
        return _event_from_row(row)

    # ---- 分析运行、不可变快照与主观反馈 ----

    def create_analysis_run(self, run) -> orm.AnalysisRun:
        row = orm.AnalysisRun(
            id=run.id,
            user_id=run.user_id,
            target_date=run.target_date,
            status=run.status.value,
            started_at=_naive_utc(run.started_at),
            completed_at=_naive_utc(run.completed_at) if run.completed_at else None,
            intelligence_version=run.intelligence_version,
            decision_policy_version=run.decision_policy_version,
            evidence_version=run.evidence_version,
            error=run.error,
            profile_revision_used=run.profile_revision_used,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def complete_analysis_run(self, run_id: str, status: str, error: str | None = None):
        row = self.db.get(orm.AnalysisRun, run_id)
        if row is None:
            raise ValueError("分析运行不存在")
        row.status = status
        row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.error = error[:1024] if error else None
        self.db.flush()
        return row

    def analysis_run(self, user_id: str, run_id: str):
        row = self.db.get(orm.AnalysisRun, run_id)
        return row if row is not None and row.user_id == user_id else None

    def analysis_runs(self, user_id: str, start: date, end: date):
        return list(self.db.execute(select(orm.AnalysisRun).where(
            orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.target_date.between(start, end),
        ).order_by(orm.AnalysisRun.target_date.desc(), orm.AnalysisRun.started_at.desc())).scalars().all())

    def save_analysis_snapshot(
        self,
        analysis_run_id: str,
        user_id: str,
        profile_type: str,
        period_start: date,
        period_end: date,
        schema_version: str,
        intelligence_version: str,
        decision_policy_version: str,
        evidence_version: str,
        payload: dict,
    ) -> orm.AnalysisSnapshot:
        row = orm.AnalysisSnapshot(
            id=uuid4().hex,
            analysis_run_id=analysis_run_id,
            user_id=user_id,
            profile_type=profile_type,
            period_start=period_start,
            period_end=period_end,
            intelligence_version=intelligence_version,
            decision_policy_version=decision_policy_version,
            evidence_version=evidence_version,
            schema_version=schema_version,
            payload=payload,
        )
        self.db.add(row)
        generated_at = payload.get("generated_at")
        if isinstance(generated_at, str):
            parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            row.generated_at = _naive_utc(parsed)
        self.db.flush()
        return row

    def latest_analysis_snapshot(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
    ) -> orm.AnalysisSnapshot | None:
        conditions = [
            orm.AnalysisSnapshot.user_id == user_id,
            orm.AnalysisSnapshot.profile_type == profile_type,
            orm.AnalysisSnapshot.period_end == period_end,
            orm.AnalysisSnapshot.intelligence_version == INTELLIGENCE_VERSION,
            orm.AnalysisSnapshot.decision_policy_version == DECISION_POLICY_VERSION,
            orm.AnalysisSnapshot.evidence_version == EVIDENCE_VERSION,
        ]
        schema = _CURRENT_SNAPSHOT_SCHEMAS.get(profile_type)
        if schema is not None:
            conditions.append(orm.AnalysisSnapshot.schema_version == schema)
        return self.db.execute(
            select(orm.AnalysisSnapshot).where(*conditions).order_by(
                orm.AnalysisSnapshot.generated_at.desc(), orm.AnalysisSnapshot.id.desc()
            )
        ).scalars().first()

    def latest_analysis_snapshot_on_or_before(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
    ) -> orm.AnalysisSnapshot | None:
        conditions = [
            orm.AnalysisSnapshot.user_id == user_id,
            orm.AnalysisSnapshot.profile_type == profile_type,
            orm.AnalysisSnapshot.period_end <= period_end,
            orm.AnalysisSnapshot.intelligence_version == INTELLIGENCE_VERSION,
            orm.AnalysisSnapshot.decision_policy_version == DECISION_POLICY_VERSION,
            orm.AnalysisSnapshot.evidence_version == EVIDENCE_VERSION,
        ]
        schema = _CURRENT_SNAPSHOT_SCHEMAS.get(profile_type)
        if schema is not None:
            conditions.append(orm.AnalysisSnapshot.schema_version == schema)
        return self.db.execute(
            select(orm.AnalysisSnapshot).where(*conditions).order_by(
                orm.AnalysisSnapshot.period_end.desc(),
                orm.AnalysisSnapshot.generated_at.desc(),
                orm.AnalysisSnapshot.id.desc(),
            )
        ).scalars().first()

    def save_recommendation(
        self, recommendation: RecommendationInstance
    ) -> RecommendationInstance:
        row = orm.RecommendationInstance(
            id=recommendation.id,
            analysis_run_id=recommendation.analysis_run_id,
            user_id=recommendation.user_id,
            date=recommendation.date,
            decision=recommendation.decision.model_dump(mode="json"),
            linked_workout_source=recommendation.linked_workout_source,
            linked_workout_id=recommendation.linked_workout_id,
            completion_status=recommendation.completion_status.value,
            created_at=_naive_utc(recommendation.created_at),
            completed_at=(
                _naive_utc(recommendation.completed_at)
                if recommendation.completed_at else None
            ),
        )
        self.db.add(row)
        self.db.flush()
        return recommendation

    def recommendation(
        self, user_id: str, recommendation_id: str
    ) -> RecommendationInstance | None:
        row = self.db.get(orm.RecommendationInstance, recommendation_id)
        if row is None or row.user_id != user_id:
            return None
        return _recommendation_from_row(row)

    def recommendations(
        self, user_id: str, start: date, end: date
    ) -> list[RecommendationInstance]:
        rows = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.date.between(start, end),
        ).order_by(orm.RecommendationInstance.date.desc(), orm.RecommendationInstance.created_at.desc())).scalars().all()
        return [_recommendation_from_row(row) for row in rows]

    def recommendations_for_workouts(
        self, user_id: str, workout_ids: list[str], source: str = "zepp"
    ) -> dict[str, str]:
        keyed = self.recommendations_for_workout_keys(
            user_id, [(source, workout_id) for workout_id in workout_ids]
        )
        return {
            workout_id: recommendation_id
            for (_source, workout_id), recommendation_id in keyed.items()
        }

    def recommendations_for_workout_keys(
        self, user_id: str, workout_keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        if not workout_keys:
            return {}
        rows = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            tuple_(
                orm.RecommendationInstance.linked_workout_source,
                orm.RecommendationInstance.linked_workout_id,
            ).in_(workout_keys),
        )).scalars().all()
        return {
            (row.linked_workout_source, row.linked_workout_id): row.id
            for row in rows
            if row.linked_workout_source is not None
            and row.linked_workout_id is not None
        }

    def link_recommendation(
        self,
        user_id: str,
        recommendation_id: str,
        workout_id: str,
        workout_source: str = "zepp",
    ) -> RecommendationInstance:
        row = self.db.get(orm.RecommendationInstance, recommendation_id)
        if row is None or row.user_id != user_id:
            raise ValueError("训练建议不存在或不属于当前用户")
        if self.workout(user_id, workout_id, source=workout_source) is None:
            raise ValueError("指定训练不存在或不属于当前用户")
        existing = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.linked_workout_source == workout_source,
            orm.RecommendationInstance.linked_workout_id == workout_id,
            orm.RecommendationInstance.id != recommendation_id,
        )).scalar_one_or_none()
        if existing is not None:
            raise ValueError("该训练已关联其他训练建议")
        if row.linked_workout_id and (
            row.linked_workout_source != workout_source
            or row.linked_workout_id != workout_id
        ):
            raise ValueError("训练建议已关联其他训练")
        row.linked_workout_source = workout_source
        row.linked_workout_id = workout_id
        row.completion_status = RecommendationStatus.COMPLETED.value
        row.completed_at = row.completed_at or datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.flush()
        return _recommendation_from_row(row)

    def analysis_snapshots(
        self,
        user_id: str,
        profile_type: str,
        start: date,
        end: date,
    ) -> list[orm.AnalysisSnapshot]:
        conditions = [
            orm.AnalysisSnapshot.user_id == user_id,
            orm.AnalysisSnapshot.profile_type == profile_type,
            orm.AnalysisSnapshot.period_end.between(start, end),
            orm.AnalysisSnapshot.intelligence_version == INTELLIGENCE_VERSION,
            orm.AnalysisSnapshot.decision_policy_version == DECISION_POLICY_VERSION,
            orm.AnalysisSnapshot.evidence_version == EVIDENCE_VERSION,
        ]
        schema = _CURRENT_SNAPSHOT_SCHEMAS.get(profile_type)
        if schema is not None:
            conditions.append(orm.AnalysisSnapshot.schema_version == schema)
        return list(self.db.execute(
            select(orm.AnalysisSnapshot).where(*conditions).order_by(
                orm.AnalysisSnapshot.period_end, orm.AnalysisSnapshot.generated_at
            )
        ).scalars().all())

    def save_subjective_feedback(self, feedback: SubjectiveFeedback) -> SubjectiveFeedback:
        row = orm.SubjectiveFeedback(**feedback.model_dump(mode="python"))
        row.created_at = _naive_utc(feedback.created_at)
        self.db.add(row)
        self.db.flush()
        return feedback

    def subjective_feedback(
        self,
        user_id: str,
        start: date,
        end: date,
    ) -> list[SubjectiveFeedback]:
        rows = self.db.execute(
            select(orm.SubjectiveFeedback).where(
                orm.SubjectiveFeedback.user_id == user_id,
                orm.SubjectiveFeedback.date.between(start, end),
            ).order_by(orm.SubjectiveFeedback.date, orm.SubjectiveFeedback.created_at)
        ).scalars().all()
        return [SubjectiveFeedback.model_validate(row, from_attributes=True) for row in rows]

    # ---- OAuth 令牌 ----

    def save_token(self, token: AuthToken) -> None:
        """Atomically claim a source-qualified vendor identity and save its token."""
        token.source_user_id = (
            token.source_user_id.strip()
            if isinstance(token.source_user_id, str)
            else token.source_user_id
        ) or None
        if token.source == "zepp" and not token.source_user_id:
            raise SourceIdentityConflict("Zepp 凭据必须包含厂商用户 id")

        row = self.db.execute(
            select(orm.AuthToken).where(
                orm.AuthToken.user_id == token.user_id,
                orm.AuthToken.source == token.source,
            )
        ).scalar_one_or_none()
        if (
            row is not None
            and row.source_user_id
            and token.source_user_id
            and row.source_user_id != token.source_user_id
        ):
            raise SourceIdentityConflict("当前本地用户已绑定其他厂商账号")

        user = self.db.get(orm.User, token.user_id)
        if (
            token.source == "zepp"
            and user is not None
            and user.source_user_id
            and token.source_user_id
            and (
                user.source != token.source
                or user.source_user_id != token.source_user_id
            )
        ):
            raise SourceIdentityConflict("当前本地用户已映射到其他 Zepp 账号")
        if token.source_user_id and self.source_identity_owned_by_other(
            token.user_id, token.source, token.source_user_id
        ):
            raise SourceIdentityConflict("该厂商账号已绑定到其他本地用户")

        if row is None:
            row = orm.AuthToken(user_id=token.user_id, source=token.source)
            self.db.add(row)
        if user is None:
            user = orm.User(id=token.user_id)
            self.db.add(user)

        from .token_cipher import encrypt_token

        row.access_token = encrypt_token(token.access_token)
        row.refresh_token = encrypt_token(token.refresh_token)
        row.expires_at = token.expires_at
        row.scope = token.scope
        row.region_host = token.region_host
        row.source_user_id = token.source_user_id
        if token.source == "zepp":
            user.source = token.source
            user.source_user_id = token.source_user_id
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise SourceIdentityConflict(
                "该厂商账号已绑定到其他本地用户"
            ) from exc

    def get_token(self, user_id: str, source: str = "zepp") -> AuthToken | None:
        row = self.db.execute(
            select(orm.AuthToken).where(
                orm.AuthToken.user_id == user_id,
                orm.AuthToken.source == source,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        from .token_cipher import decrypt_token

        return AuthToken(
            user_id=row.user_id, source=row.source,
            access_token=decrypt_token(row.access_token), refresh_token=decrypt_token(row.refresh_token),
            expires_at=row.expires_at, scope=row.scope,
            region_host=row.region_host or "",
            source_user_id=row.source_user_id,
        )

    def save_oauth_state(self, state: str, user_id: str, source: str = "zepp") -> None:
        self.db.merge(orm.OAuthState(id=state, user_id=user_id, source=source))
        self.db.flush()

    def oauth_state_exists(self, state: str) -> bool:
        return self.db.get(orm.OAuthState, state) is not None

    def consume_oauth_state(self, state: str) -> str | None:
        """校验并消费一次性 state，返回绑定用户 id（不存在/已用返回 None）。"""
        row = self.db.get(orm.OAuthState, state)
        if row is None:
            return None
        self.db.delete(row)
        self.db.flush()
        return row.user_id

    # ---- Zepp 浏览器扩展配对 ----

    def create_pairing_session(self, pairing_id: str, user_id: str, expires_at: datetime, sync_days: int = 30) -> orm.ZeppPairingSession:
        self.upsert_user(user_id)
        row = orm.ZeppPairingSession(
            id=pairing_id,
            user_id=user_id,
            expires_at=_naive_utc(expires_at),
            sync_days=sync_days,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def pairing_session(self, pairing_id: str) -> orm.ZeppPairingSession | None:
        return self.db.get(orm.ZeppPairingSession, pairing_id)

    def claim_pairing_session(
        self, pairing_id: str, processing_lease_seconds: int = 120
    ) -> bool:
        now = datetime.utcnow()
        reclaim_before = now - timedelta(seconds=max(1, processing_lease_seconds))
        result = self.db.execute(
            update(orm.ZeppPairingSession).where(
                orm.ZeppPairingSession.id == pairing_id,
                or_(
                    orm.ZeppPairingSession.status.in_(("waiting", "failed")),
                    (
                        (orm.ZeppPairingSession.status == "processing")
                        & (orm.ZeppPairingSession.processing_started_at <= reclaim_before)
                    ),
                ),
                orm.ZeppPairingSession.expires_at > now,
            ).values(
                status="processing", message="正在验证 Zepp 凭据",
                processing_started_at=now,
            )
        )
        self.db.flush()
        return bool(result.rowcount)

    def finish_pairing_session(
        self, pairing_id: str, message: str = "已连接", sync_attempt_id: str | None = None
    ) -> None:
        row = self.db.get(orm.ZeppPairingSession, pairing_id)
        if row:
            row.status = "connected"
            row.message = message
            row.consumed_at = datetime.utcnow()
            row.processing_started_at = None
            if sync_attempt_id is not None:
                row.sync_attempt_id = sync_attempt_id
            self.db.flush()

    def fail_pairing_session(self, pairing_id: str, message: str) -> None:
        row = self.db.get(orm.ZeppPairingSession, pairing_id)
        if row and row.status != "connected":
            row.status = "failed"
            row.message = message[:512]
            row.processing_started_at = None
            self.db.flush()

    # ---- Zepp 浏览器持续连接 ----

    def create_browser_link(
        self, token_digest: str, user_id: str, sync_attempt_id: str | None = None
    ) -> orm.ZeppBrowserLink:
        self.upsert_user(user_id)
        row = orm.ZeppBrowserLink(
            token_digest=token_digest,
            user_id=user_id,
            status="connected",
            message="浏览器已连接",
            last_verified_at=datetime.utcnow(),
            sync_attempt_id=sync_attempt_id,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def browser_link(self, token_digest: str) -> orm.ZeppBrowserLink | None:
        return self.db.get(orm.ZeppBrowserLink, token_digest)

    def latest_browser_link(self, user_id: str) -> orm.ZeppBrowserLink | None:
        return self.db.execute(
            select(orm.ZeppBrowserLink).where(
                orm.ZeppBrowserLink.user_id == user_id,
                orm.ZeppBrowserLink.revoked_at.is_(None),
            ).order_by(orm.ZeppBrowserLink.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    def mark_browser_link_verified(self, token_digest: str, message: str = "登录状态有效") -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            now = datetime.utcnow()
            row.status = "connected"
            row.message = message[:512]
            row.last_seen_at = now
            row.last_verified_at = now
            self.db.flush()

    def mark_browser_link_reauth(self, token_digest: str, message: str) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            row.status = "needs_login"
            row.message = message[:512]
            row.last_seen_at = datetime.utcnow()
            self.db.flush()

    def mark_user_browser_links_reauth(self, user_id: str, message: str) -> None:
        rows = self.db.execute(
            select(orm.ZeppBrowserLink).where(
                orm.ZeppBrowserLink.user_id == user_id,
                orm.ZeppBrowserLink.revoked_at.is_(None),
            )
        ).scalars().all()
        now = datetime.utcnow()
        for row in rows:
            row.status = "needs_login"
            row.message = message[:512]
            row.last_seen_at = now
        self.db.flush()

    def mark_browser_link_synced(
        self, token_digest: str, message: str, sync_attempt_id: str | None = None
    ) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            if row.status != "needs_login":
                row.status = "connected"
                row.message = message[:512]
            row.last_sync_at = datetime.utcnow()
            if sync_attempt_id is not None:
                row.sync_attempt_id = sync_attempt_id
            self.db.flush()

    def mark_browser_link_sync_failed(self, token_digest: str, message: str) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None and row.status != "needs_login":
            row.status = "connected"
            row.message = message[:512]
            self.db.flush()

    # ---- Balance 2 Zepp OS device upload links ----

    def create_device_link(
        self, token_digest: str, user_id: str, device_label: str = "balance2_zepp_os"
    ) -> orm.ZeppDeviceLink:
        self.upsert_user(user_id)
        row = orm.ZeppDeviceLink(
            token_digest=token_digest,
            user_id=user_id,
            device_label=device_label,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def device_link(self, token_digest: str) -> orm.ZeppDeviceLink | None:
        return self.db.get(orm.ZeppDeviceLink, token_digest)

    def mark_device_link_seen(self, token_digest: str) -> None:
        row = self.device_link(token_digest)
        if row and row.revoked_at is None:
            row.last_seen_at = datetime.utcnow()
            self.db.flush()

    def delete_for_user(self, user_id: str) -> None:
        for model in (
            orm.Device, orm.SleepRecord, orm.ActivityRecord, orm.TrainingRecord,
            orm.MetricSample, orm.DailyMetric, orm.DenseDataFile,
            orm.WorkoutMetricSample, orm.StrengthExercise, orm.Workout,
            orm.TrainingPreference,
            orm.UserProfileRevision, orm.UserProfile,
            orm.HealthEventObservation, orm.HealthEventRecord, orm.AnalysisSnapshot,
            orm.RecommendationInstance,
            orm.AnalysisRun,
            orm.SubjectiveFeedback,
            orm.SyncStreamState,
            orm.ZeppDeviceLink, orm.ZeppBrowserLink, orm.ZeppPairingSession,
            orm.AuthToken, orm.OAuthState,
        ):
            self.db.execute(delete(model).where(model.user_id == user_id))
        attempt_ids = select(orm.SyncAttempt.id).where(orm.SyncAttempt.user_id == user_id)
        self.db.execute(delete(orm.SyncChunk).where(orm.SyncChunk.attempt_id.in_(attempt_ids)))
        self.db.execute(delete(orm.SyncAttempt).where(orm.SyncAttempt.user_id == user_id))
        self.db.execute(delete(orm.User).where(orm.User.id == user_id))


def _profile_field_value(field: ProfileField | None):
    return field.value if field is not None else None


def _user_profile_from_row(row: orm.UserProfile) -> UserProfile:
    payload = dict(row.payload or {})
    values = {"user_id": row.user_id, "revision": row.revision}
    for field in ("sex", "confirmed_hrmax_bpm", "sleep_target_minutes"):
        raw = payload.get(field)
        if raw is not None:
            values[field] = ProfileField.model_validate(raw)
        else:
            values[field] = None
    return UserProfile.model_validate(values)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _recommendation_from_row(row: orm.RecommendationInstance) -> RecommendationInstance:
    return RecommendationInstance(
        id=row.id,
        analysis_run_id=row.analysis_run_id,
        user_id=row.user_id,
        date=row.date,
        decision=row.decision,
        linked_workout_source=row.linked_workout_source,
        linked_workout_id=row.linked_workout_id,
        completion_status=RecommendationStatus(row.completion_status),
        created_at=row.created_at.replace(tzinfo=timezone.utc),
        completed_at=(
            row.completed_at.replace(tzinfo=timezone.utc) if row.completed_at else None
        ),
    )


def _event_from_row(row: orm.HealthEventRecord) -> HealthEvent:
    payload = dict(row.payload or {})
    payload.update({
        "lifecycle": row.lifecycle,
        "last_observed_date": row.last_observed_date,
        "last_evaluated_date": row.last_evaluated_date,
        "resolved_at": row.resolved_at,
        "acknowledged": row.acknowledged_at is not None,
        "acknowledged_at": (
            row.acknowledged_at.replace(tzinfo=timezone.utc)
            if row.acknowledged_at else None
        ),
    })
    return HealthEvent.model_validate(payload)
