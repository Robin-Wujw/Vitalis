"""仓储层：封装对 ORM 的读写，业务层只依赖仓储接口。"""

from datetime import date, datetime, timedelta, timezone
import hashlib
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from vitalis.models import (
    AuthToken,
    Device,
    NormalizedDaily,
    DailyMetric,
    DenseDataFile,
    MetricSample,
    Workout,
    WorkoutSample,
)
from vitalis.intelligence.contracts import (
    HealthEvent,
    HealthEventObservation,
    RecommendationInstance,
    RecommendationStatus,
    SubjectiveFeedback,
)

from . import models as orm


class HealthRepository:
    """健康数据仓储：负责 vitalis.models（schema）<-> ORM（表）的映射。"""

    def __init__(self, db: Session):
        self.db = db

    # ---- 用户 ----
    def upsert_user(self, user_id: str, name: str = "", source: str = "zepp", source_user_id: str | None = None) -> orm.User:
        u = self.db.get(orm.User, user_id)
        if u is None:
            u = orm.User(id=user_id, name=name, source=source, source_user_id=source_user_id)
            self.db.add(u)
        else:
            u.name = name or u.name
            u.source = source
            u.source_user_id = source_user_id or u.source_user_id
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
        """JSON 列数据按 (user_id, date) 唯一键更新或插入。"""
        existing = self.db.query(model).filter(
            model.user_id == user_id, model.date == day
        ).one_or_none()
        if existing:
            existing.data = data
        else:
            self.db.add(model(user_id=user_id, date=day, data=data))

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
        deduplicated: dict[tuple[str, str, str, datetime, str], MetricSample] = {}
        for sample in samples:
            key = (
                sample.user_id,
                sample.source,
                sample.metric,
                _naive_utc(sample.timestamp),
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
                    "device_id": device_id,
                    "value": sample.value,
                    "unit": sample.unit,
                    "source_scope": sample.source_scope,
                }
                for (user_id, source, metric, timestamp, device_id), sample in deduplicated.items()
            ]
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.MetricSample).values(rows[offset:offset + 500])
                statement = statement.on_conflict_do_update(
                    index_elements=["user_id", "source", "metric", "timestamp", "device_id"],
                    set_={
                        "value": statement.excluded.value,
                        "unit": statement.excluded.unit,
                        "source_scope": statement.excluded.source_scope,
                    },
                )
                self.db.execute(statement)
            self.db.flush()
            return len(rows)

        written = 0
        for (user_id, source, metric, timestamp, device_id), sample in deduplicated.items():
            row = self.db.execute(
                select(orm.MetricSample).where(
                    orm.MetricSample.user_id == user_id,
                    orm.MetricSample.source == source,
                    orm.MetricSample.metric == metric,
                    orm.MetricSample.timestamp == timestamp,
                    orm.MetricSample.device_id == device_id,
                )
            ).scalar_one_or_none()
            if row is None:
                row = orm.MetricSample(
                    user_id=user_id,
                    source=source,
                    metric=metric,
                    timestamp=timestamp,
                    device_id=device_id,
                )
                self.db.add(row)
            row.value = sample.value
            row.unit = sample.unit
            row.source_scope = sample.source_scope
            row.device_id = device_id
            written += 1
        self.db.flush()
        return written

    def metric_samples(
        self, user_id: str, metric: str, start: datetime, end: datetime, limit: int = 50_000
    ) -> list[orm.MetricSample]:
        return list(self.db.execute(
            select(orm.MetricSample).where(
                orm.MetricSample.user_id == user_id,
                orm.MetricSample.metric == metric,
                orm.MetricSample.timestamp.between(_naive_utc(start), _naive_utc(end)),
            ).order_by(orm.MetricSample.timestamp).limit(limit)
        ).scalars().all())

    def save_daily_metrics(self, metrics: list[DailyMetric]) -> int:
        """Idempotently upsert sparse daily vendor metrics."""
        deduplicated: dict[tuple[str, str, date, str], DailyMetric] = {}
        for metric in metrics:
            key = (metric.user_id, metric.source, metric.date, metric.metric)
            deduplicated[key] = metric

        written = 0
        for (user_id, source, day, metric_name), metric in deduplicated.items():
            row = self.db.execute(
                select(orm.DailyMetric).where(
                    orm.DailyMetric.user_id == user_id,
                    orm.DailyMetric.source == source,
                    orm.DailyMetric.date == day,
                    orm.DailyMetric.metric == metric_name,
                )
            ).scalar_one_or_none()
            if row is None:
                row = orm.DailyMetric(
                    user_id=user_id,
                    source=source,
                    date=day,
                    metric=metric_name,
                )
                self.db.add(row)
            row.value = metric.value
            row.unit = metric.unit
            row.source_scope = metric.source_scope
            row.device_id = metric.device_id
            written += 1
        self.db.flush()
        return written

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
        """Idempotently persist opaque file indexes without treating them as samples."""
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

    # ---- 单次运动 ----

    def save_workout(self, workout: Workout) -> None:
        row = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == workout.user_id,
                orm.Workout.source == workout.source,
                orm.Workout.workout_id == workout.workout_id,
            )
        ).scalar_one_or_none()
        if row is None:
            row = orm.Workout(
                user_id=workout.user_id,
                source=workout.source,
                workout_id=workout.workout_id,
            )
            self.db.add(row)
        row.started_at = _naive_utc(workout.started_at) if workout.started_at else None
        row.vendor_source = workout.vendor_source
        row.data = workout.model_dump(mode="json", exclude_none=True)
        self.db.flush()

    def pending_workout_details(
        self, user_id: str, start: datetime, end: datetime, limit: int = 100
    ) -> list[orm.Workout]:
        return list(self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at.between(_naive_utc(start), _naive_utc(end)),
                orm.Workout.vendor_source.is_not(None),
                orm.Workout.detail_synced.is_(False),
            ).order_by(orm.Workout.started_at).limit(limit)
        ).scalars().all())

    def save_workout_detail(
        self,
        user_id: str,
        workout_id: str,
        detail: dict,
        samples: list[WorkoutSample] | None = None,
    ) -> bool:
        row = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        row.detail = detail
        row.detail_synced = True
        if samples is not None:
            self.db.execute(delete(orm.WorkoutSample).where(
                orm.WorkoutSample.user_id == user_id,
                orm.WorkoutSample.workout_id == workout_id,
            ))
            self.db.add_all([
                orm.WorkoutSample(
                    user_id=user_id,
                    workout_id=workout_id,
                    timestamp=_naive_utc(sample.timestamp),
                    heart_rate=sample.heart_rate,
                    source_scope=sample.source_scope,
                    device_id=sample.device_id,
                )
                for sample in samples
            ])
        self.db.flush()
        return True

    def workout_samples(
        self, user_id: str, workout_id: str, limit: int = 50_000
    ) -> list[orm.WorkoutSample]:
        return list(self.db.execute(
            select(orm.WorkoutSample).where(
                orm.WorkoutSample.user_id == user_id,
                orm.WorkoutSample.workout_id == workout_id,
            ).order_by(orm.WorkoutSample.timestamp).limit(limit)
        ).scalars().all())

    def workouts(self, user_id: str, start: date, end: date, limit: int = 500) -> list[orm.Workout]:
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time())
        return list(self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= start_dt,
                orm.Workout.started_at < end_dt,
            ).order_by(orm.Workout.started_at.desc()).limit(limit)
        ).scalars().all())

    def workout(self, user_id: str, workout_id: str) -> orm.Workout | None:
        return self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()

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
        return self.db.execute(
            select(orm.AnalysisSnapshot).where(
                orm.AnalysisSnapshot.user_id == user_id,
                orm.AnalysisSnapshot.profile_type == profile_type,
                orm.AnalysisSnapshot.period_end == period_end,
            ).order_by(orm.AnalysisSnapshot.generated_at.desc(), orm.AnalysisSnapshot.id.desc())
        ).scalars().first()

    def latest_analysis_snapshot_on_or_before(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
    ) -> orm.AnalysisSnapshot | None:
        return self.db.execute(
            select(orm.AnalysisSnapshot).where(
                orm.AnalysisSnapshot.user_id == user_id,
                orm.AnalysisSnapshot.profile_type == profile_type,
                orm.AnalysisSnapshot.period_end <= period_end,
            ).order_by(
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
        self, user_id: str, workout_ids: list[str]
    ) -> dict[str, str]:
        if not workout_ids:
            return {}
        rows = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.linked_workout_id.in_(workout_ids),
        )).scalars().all()
        return {
            row.linked_workout_id: row.id
            for row in rows
            if row.linked_workout_id is not None
        }

    def link_recommendation(
        self, user_id: str, recommendation_id: str, workout_id: str
    ) -> RecommendationInstance:
        row = self.db.get(orm.RecommendationInstance, recommendation_id)
        if row is None or row.user_id != user_id:
            raise ValueError("训练建议不存在或不属于当前用户")
        if self.workout(user_id, workout_id) is None:
            raise ValueError("指定训练不存在或不属于当前用户")
        existing = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.linked_workout_id == workout_id,
            orm.RecommendationInstance.id != recommendation_id,
        )).scalar_one_or_none()
        if existing is not None:
            raise ValueError("该训练已关联其他训练建议")
        if row.linked_workout_id and row.linked_workout_id != workout_id:
            raise ValueError("训练建议已关联其他训练")
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
        return list(self.db.execute(
            select(orm.AnalysisSnapshot).where(
                orm.AnalysisSnapshot.user_id == user_id,
                orm.AnalysisSnapshot.profile_type == profile_type,
                orm.AnalysisSnapshot.period_end.between(start, end),
            ).order_by(orm.AnalysisSnapshot.period_end, orm.AnalysisSnapshot.generated_at)
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
        """保存/更新某用户的厂商访问令牌。"""
        row = self.db.execute(
            select(orm.AuthToken).where(
                orm.AuthToken.user_id == token.user_id,
                orm.AuthToken.source == token.source,
            )
        ).scalar_one_or_none()
        if row is None:
            row = orm.AuthToken(user_id=token.user_id, source=token.source)
            self.db.add(row)
        from .token_cipher import encrypt_token

        row.access_token = encrypt_token(token.access_token)
        row.refresh_token = encrypt_token(token.refresh_token)
        row.expires_at = token.expires_at
        row.scope = token.scope
        row.region_host = token.region_host
        row.source_user_id = token.source_user_id
        self.db.flush()

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

    def claim_pairing_session(self, pairing_id: str) -> bool:
        now = datetime.utcnow()
        result = self.db.execute(
            update(orm.ZeppPairingSession).where(
                orm.ZeppPairingSession.id == pairing_id,
                orm.ZeppPairingSession.status.in_(("waiting", "failed")),
                orm.ZeppPairingSession.expires_at > now,
            ).values(status="processing", message="正在验证 Zepp 凭据")
        )
        self.db.flush()
        return bool(result.rowcount)

    def finish_pairing_session(self, pairing_id: str, message: str = "已连接") -> None:
        row = self.db.get(orm.ZeppPairingSession, pairing_id)
        if row:
            row.status = "connected"
            row.message = message
            row.consumed_at = datetime.utcnow()
            self.db.flush()

    def fail_pairing_session(self, pairing_id: str, message: str) -> None:
        row = self.db.get(orm.ZeppPairingSession, pairing_id)
        if row and row.status != "connected":
            row.status = "failed"
            row.message = message[:512]
            self.db.flush()

    # ---- Zepp 浏览器持续连接 ----

    def create_browser_link(self, token_digest: str, user_id: str) -> orm.ZeppBrowserLink:
        self.upsert_user(user_id)
        row = orm.ZeppBrowserLink(
            token_digest=token_digest,
            user_id=user_id,
            status="connected",
            message="浏览器已连接",
            last_verified_at=datetime.utcnow(),
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

    def mark_browser_link_synced(self, token_digest: str, message: str) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            if row.status != "needs_login":
                row.status = "connected"
                row.message = message[:512]
            row.last_sync_at = datetime.utcnow()
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
            orm.MetricSample, orm.DailyMetric, orm.DenseDataFile, orm.Workout,
            orm.HealthEventObservation, orm.HealthEventRecord, orm.AnalysisSnapshot,
            orm.RecommendationInstance,
            orm.AnalysisRun,
            orm.SubjectiveFeedback,
            orm.ZeppDeviceLink,
        ):
            self.db.execute(delete(model).where(model.user_id == user_id))


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
