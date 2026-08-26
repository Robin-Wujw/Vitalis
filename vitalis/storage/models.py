"""存储层 ORM 模型。

表（对应架构文档）：
- users                用户
- devices              设备
- health_daily         每日健康快照
- sleep_records        睡眠
- activity_records     活动
- training_records     训练
- analysis_records     分析结果

健康数据用 JSON 列存统一 Schema（Vitalis Schema）字段，保证跨厂商可扩展，
同时保留结构化列便于按日期/用户查询。
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    __table_args__ = ({"info": {"timescale": True}},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis SleepRecord schema")


class ActivityRecord(Base):
    __tablename__ = "activity_records"
    __table_args__ = ({"info": {"timescale": True}},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis ActivityRecord schema")


class TrainingRecord(Base):
    __tablename__ = "training_records"
    __table_args__ = ({"info": {"timescale": True}},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    data: Mapped[dict] = mapped_column(JSON, comment="Vitalis TrainingRecord schema")


class MetricSample(Base):
    """Timestamped vendor-neutral measurements such as heart rate and SpO2."""

    __tablename__ = "metric_samples"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "metric", "timestamp", name="uq_metric_sample"),
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
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class DailyMetric(Base):
    """Sparse daily values such as readiness, stress, PAI and VO2 max."""

    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("user_id", "source", "date", "metric", name="uq_daily_metric"),
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
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


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


class WorkoutSample(Base):
    """High-frequency samples normalized from one workout detail payload."""

    __tablename__ = "workout_samples"
    __table_args__ = (
        UniqueConstraint("user_id", "workout_id", "timestamp", name="uq_workout_sample"),
        {"info": {"timescale": True}},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    workout_id: Mapped[str] = mapped_column(String(128), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    heart_rate: Mapped[int] = mapped_column(Integer)
    source_scope: Mapped[str] = mapped_column(String(32), default="unknown")
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class HealthDaily(Base):
    """每日健康快照（含分析引擎生成的分/趋势）。"""

    __tablename__ = "health_daily"
    __table_args__ = ({"info": {"timescale": True}},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)

    hrv: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hrv_trend_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_score: Mapped[int] = mapped_column(Integer, default=0)
    recovery_level: Mapped[str] = mapped_column(String(24), default="moderate")
    stress_level: Mapped[str] = mapped_column(String(24), default="medium")
    overall_score: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")


class AnalysisRecord(Base):
    __tablename__ = "analysis_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    engine: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(32))
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)


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
