"""存储层 ORM 模型。

表（对应架构文档）：
- users                用户
- devices              设备
- sleep_records        睡眠
- activity_records     活动
- training_records     训练

健康数据用 JSON 列存统一 Schema（Vitalis Schema）字段，保证跨厂商可扩展，
同时保留结构化列便于按日期/用户查询。
"""

from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[str] = mapped_column(String(32), default="zepp", comment="数据源")
    source_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128), default="")
    device_id: Mapped[str] = mapped_column(String(128), default="")
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SleepRecord(Base):
    __tablename__ = "sleep_records"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_sleep_record_day"),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis SleepRecord schema")


class ActivityRecord(Base):
    __tablename__ = "activity_records"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_activity_record_day"),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis ActivityRecord schema")


class TrainingRecord(Base):
    __tablename__ = "training_records"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_training_record_day"),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis TrainingRecord schema")


class MetricSample(Base):
    """Timestamped vendor-neutral measurements such as heart rate and SpO2."""

    __tablename__ = "metric_samples"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "metric", "timestamp", "source_scope", "device_id",
            name="uq_metric_sample",
        ),
        Index(
            "ix_metric_samples_user_metric_timestamp",
            "user_id",
            "metric",
            "timestamp",
        ),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    metric: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24), default="")
    source_scope: Mapped[str] = mapped_column(String(24), default="unknown")
    device_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)


class DailyMetric(Base):
    """Sparse daily values such as readiness, stress, PAI and VO2 max."""

    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "date", "metric", "source_scope", "device_id",
            name="uq_daily_metric",
        ),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    date: Mapped[date] = mapped_column(Date, index=True)
    metric: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24), default="")
    source_scope: Mapped[str] = mapped_column(String(24), default="unknown")
    device_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)


class DenseDataFile(Base):
    """Indexed vendor file whose payload has not necessarily been decoded."""

    __tablename__ = "dense_data_files"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "stream", "file_id", "start_utc", "device_id",
            name="uq_dense_data_file",
        ),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    stream: Mapped[str] = mapped_column(String(64), index=True)
    file_id: Mapped[str] = mapped_column(String(256))
    file_type: Mapped[str] = mapped_column(String(64), default="")
    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    start_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    end_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_scope: Mapped[str] = mapped_column(String(24), default="unknown")
    device_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    parse_status: Mapped[str] = mapped_column(String(24), default="indexed", index=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)


class Workout(Base):
    """One workout summary plus normalized detail metadata when available."""

    __tablename__ = "workouts"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "workout_id", name="uq_workout"),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    workout_id: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    vendor_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detail_synced: Mapped[bool] = mapped_column(Boolean, default=False)


class WorkoutMetricSample(Base):
    """Typed high-frequency observations from one workout detail payload."""

    __tablename__ = "workout_metric_samples"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "workout_id", "metric", "timestamp",
            name="uq_workout_metric_sample",
        ),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    workout_id: Mapped[str] = mapped_column(String(128), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    metric: Mapped[str] = mapped_column(String(48), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(24))
    source_scope: Mapped[str] = mapped_column(String(32), default="unknown")
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SyncStreamState(Base):
    """Latest fetch/parse/write state for one user-owned source stream."""

    __tablename__ = "sync_stream_states"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "stream", name="uq_sync_stream_state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    stream: Mapped[str] = mapped_column(String(96), index=True)
    fetch_status: Mapped[str] = mapped_column(String(24), default="never")
    parse_status: Mapped[str] = mapped_column(String(24), default="never")
    write_status: Mapped[str] = mapped_column(String(24), default="never")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    written_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sample_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_records: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_attempts.id"), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SyncAttempt(Base):
    """Durable owner record for one source synchronization attempt."""

    __tablename__ = "sync_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'partial', 'failed', 'needs_reauth', 'cancelled')",
            name="ck_sync_attempt_status",
        ),
        CheckConstraint("window_end > window_start", name="ck_sync_attempt_window"),
        CheckConstraint(
            "attempt_count >= 0 AND retry_count >= 0 AND chunk_count >= 0 "
            "AND completed_count >= 0 AND raw_records >= 0 AND records_written >= 0",
            name="ck_sync_attempt_counts",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_sync_attempt_lease",
        ),
        CheckConstraint(
            "status != 'retry_wait' OR next_retry_at IS NOT NULL",
            name="ck_sync_attempt_retry_time",
        ),
        Index("ix_sync_attempts_user_source_status", "user_id", "source", "status"),
        Index("ix_sync_attempts_lease", "status", "lease_expires_at"),
        Index(
            "uq_sync_attempt_running",
            "user_id",
            "source",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_sync_attempt_active_request",
            "user_id",
            "source",
            "request_key",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running', 'retry_wait')"),
            postgresql_where=text("status IN ('queued', 'running', 'retry_wait')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp", index=True)
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    trigger_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_version: Mapped[str] = mapped_column(String(32), default="zepp-sync-v1")
    request_key: Mapped[str] = mapped_column(String(64), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_records: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SyncChunk(Base):
    """Durable manifest item and independently leased source work unit."""

    __tablename__ = "sync_chunks"
    __table_args__ = (
        UniqueConstraint("attempt_id", "stable_key", name="uq_sync_chunk_stable_key"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'unavailable', 'failed', 'cancelled')",
            name="ck_sync_chunk_status",
        ),
        CheckConstraint(
            "(window_start IS NULL AND window_end IS NULL) OR "
            "(window_start IS NOT NULL AND window_end IS NOT NULL AND window_end > window_start)",
            name="ck_sync_chunk_window",
        ),
        CheckConstraint(
            "ordinal >= 0 AND attempt_count >= 0 AND raw_records >= 0 "
            "AND records_written >= 0",
            name="ck_sync_chunk_counts",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_sync_chunk_lease",
        ),
        CheckConstraint(
            "status != 'retry_wait' OR next_retry_at IS NOT NULL",
            name="ck_sync_chunk_retry_time",
        ),
        Index("ix_sync_chunks_attempt_status", "attempt_id", "status"),
        Index("ix_sync_chunks_lease", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("sync_attempts.id", ondelete="CASCADE"), index=True
    )
    stable_key: Mapped[str] = mapped_column(String(256))
    stream: Mapped[str] = mapped_column(String(96), index=True)
    health_stream: Mapped[str | None] = mapped_column(String(128), nullable=True)
    partition: Mapped[str] = mapped_column(String(128), default="")
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cursor: Mapped[object | None] = mapped_column(JSON, nullable=True)
    allow_unavailable: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    fetch_status: Mapped[str] = mapped_column(String(24), default="never")
    parse_status: Mapped[str] = mapped_column(String(24), default="never")
    write_status: Mapped[str] = mapped_column(String(24), default="never")
    stages: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    raw_records: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    error_kind: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class StrengthExercise(Base):
    """One explicit strength exercise attached to a user-owned workout."""

    __tablename__ = "strength_exercises"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "workout_source", "workout_id", "order",
            name="uq_strength_exercise_order",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workout_source: Mapped[str] = mapped_column(String(32), index=True)
    workout_id: Mapped[str] = mapped_column(String(128), index=True)
    order: Mapped[int] = mapped_column(Integer)
    exercise_name: Mapped[str] = mapped_column(String(100))
    exercise_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    session_focus: Mapped[str | None] = mapped_column(String(24), nullable=True)
    movement_pattern: Mapped[str] = mapped_column(String(32), index=True)
    movement_pattern_label: Mapped[str] = mapped_column(String(40))
    muscle_groups: Mapped[list] = mapped_column(JSON, default=list)
    muscle_group_labels: Mapped[list] = mapped_column(JSON, default=list)
    sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repetitions: Mapped[str | None] = mapped_column(String(50), nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    rpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    rir: Mapped[float | None] = mapped_column(Float, nullable=True)
    rest_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[str] = mapped_column(String(16))
    confidence_label: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TrainingPreference(Base):
    """Current health-first concurrent training constraints for one local user."""

    __tablename__ = "training_preferences"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    weekly_running_target: Mapped[int] = mapped_column(Integer)
    weekly_strength_target: Mapped[int] = mapped_column(Integer)
    rotation_policy: Mapped[str] = mapped_column(String(24), default="BALANCE")
    treadmill_available: Mapped[bool] = mapped_column(Boolean, default=False)
    bad_weather_running_policy: Mapped[str] = mapped_column(String(24), default="DEFER")
    available_weekdays: Mapped[list] = mapped_column(JSON, default=list)
    max_session_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    running_experience: Mapped[str | None] = mapped_column(String(24), nullable=True)
    strength_experience: Mapped[str | None] = mapped_column(String(24), nullable=True)
    equipment: Mapped[list] = mapped_column(JSON, default=list)
    pain_or_injury_status: Mapped[str] = mapped_column(String(16))
    pain_or_injury_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserProfile(Base):
    """Current user-confirmed profile values, versioned as one aggregate."""

    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserProfileRevision(Base):
    """Immutable change record for one user profile revision."""

    __tablename__ = "user_profile_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "revision", name="uq_user_profile_revision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    changed_fields: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class HealthEventRecord(Base):
    """A deterministic, explainable event emitted by the intelligence engine."""

    __tablename__ = "health_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    metric: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    lifecycle: Mapped[str] = mapped_column(String(16), index=True)
    last_observed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_evaluated_date: Mapped[date] = mapped_column(Date, index=True)
    resolved_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class HealthEventObservation(Base):
    """Immutable per-analysis observation of one event lifecycle."""

    __tablename__ = "health_event_observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    detected: Mapped[bool] = mapped_column(Boolean)
    previous_lifecycle: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AnalysisRun(Base):
    """One auditable execution of the deterministic intelligence pipeline."""

    __tablename__ = "analysis_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    intelligence_version: Mapped[str] = mapped_column(String(32))
    decision_policy_version: Mapped[str] = mapped_column(String(32))
    evidence_version: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    profile_revision_used: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)


class AnalysisSnapshot(Base):
    """Immutable structured output produced by exactly one analysis run."""

    __tablename__ = "analysis_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    profile_type: Mapped[str] = mapped_column(String(32), index=True)
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    schema_version: Mapped[str] = mapped_column(String(16))
    intelligence_version: Mapped[str] = mapped_column(String(32))
    decision_policy_version: Mapped[str] = mapped_column(String(32))
    evidence_version: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RecommendationInstance(Base):
    """A daily decision identity explicitly linked to at most one completed workout."""

    __tablename__ = "recommendation_instances"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "linked_workout_source", "linked_workout_id",
            name="uq_recommendation_completed_workout",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_run_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    decision: Mapped[dict] = mapped_column(JSON)
    linked_workout_source: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    linked_workout_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    completion_status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SubjectiveFeedback(Base):
    """User-entered perception data kept separate from wearable facts."""

    __tablename__ = "subjective_feedback"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    workout_source: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    workout_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_rpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    physical_fatigue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mental_state: Mapped[int | None] = mapped_column(Integer, nullable=True)
    muscle_soreness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthToken(Base):
    """厂商 OAuth2 令牌（扫码授权后保存）。"""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scope: Mapped[str] = mapped_column(String(256), default="")
    region_host: Mapped[str] = mapped_column(String(256), default="")
    source_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OAuthState(Base):
    """扫码授权临时 state（防 CSRF + 回调时定位用户）。"""

    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="state 值")
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="zepp")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ZeppPairingSession(Base):
    """Short-lived one-time channel between a user's browser and Vitalis cloud."""

    __tablename__ = "zepp_pairing_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="waiting", index=True)
    message: Mapped[str] = mapped_column(String(512), default="")
    sync_days: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_attempts.id"), nullable=True, index=True
    )


class ZeppBrowserLink(Base):
    """Long-lived, revocable link to a user-controlled browser extension."""

    __tablename__ = "zepp_browser_links"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="connected", index=True)
    message: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_attempts.id"), nullable=True, index=True
    )


class ZeppDeviceLink(Base):
    """Revocable upload link for the user-owned Balance 2 Zepp OS app."""

    __tablename__ = "zepp_device_links"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    device_label: Mapped[str] = mapped_column(String(64), default="balance2_zepp_os")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
