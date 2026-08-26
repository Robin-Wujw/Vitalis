"""统一健康数据模型（Vitalis Schema 的核心定义）。"""

import uuid
from datetime import date as DateType, datetime, timedelta, time, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    RecoveryLevel,
    SleepQuality,
    StressLevel,
    TrainingReadiness,
    WorkoutType,
)


class User(BaseModel):
    """系统内用户。source_user_id 对应厂商平台的用户 id。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(default="", description="用户显示名")
    source_user_id: str | None = Field(default=None, description="厂商平台用户 id（如 Zepp user id）")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Device(BaseModel):
    """用户的设备。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    source: str = Field(description="数据源，如 zepp / garmin")
    model: str = Field(default="", description="设备型号")
    device_id: str = Field(default="", description="厂商设备 id")
    connected_at: datetime = Field(default_factory=datetime.utcnow)


class SleepRecord(BaseModel):
    """统一睡眠记录。时长统一为「分钟」。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    date: Optional[DateType] = None
    source: str = "zepp"

    sleep_duration: int = Field(ge=0, description="总睡眠时长（分钟）")
    deep_sleep: int = Field(default=0, ge=0, description="深睡时长（分钟）")
    rem_sleep: int = Field(default=0, ge=0, description="REM 睡眠（分钟）")
    light_sleep: int = Field(default=0, ge=0, description="浅睡时长（分钟）")
    awake: int = Field(default=0, ge=0, description="清醒时长（分钟）")
    sleep_score: int | None = Field(default=None, ge=0, le=100, description="睡眠评分")
    bedtime: time | None = None
    wake_time: time | None = None

    @property
    def quality(self) -> SleepQuality:
        """根据时长与评分给出确定性质量等级。"""
        if self.sleep_score is not None:
            if self.sleep_score >= 85:
                return SleepQuality.EXCELLENT
            if self.sleep_score >= 70:
                return SleepQuality.GOOD
            if self.sleep_score >= 55:
                return SleepQuality.FAIR
            return SleepQuality.POOR
        if self.sleep_duration >= 480:
            return SleepQuality.GOOD
        if self.sleep_duration >= 360:
            return SleepQuality.FAIR
        return SleepQuality.POOR


class Workout(BaseModel):
    """单次训练记录。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    source: str = "zepp"
    workout_id: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None

    type: WorkoutType = WorkoutType.OTHER
    duration: int = Field(ge=0, description="时长（分钟）")
    heart_rate_avg: int = Field(default=0, ge=0, description="平均心率 bpm")
    heart_rate_max: int = Field(default=0, ge=0)
    load: int = Field(default=0, ge=0, description="厂商训练负荷（非负，无固定上限）")
    calories: int = Field(default=0, ge=0)
    distance_km: float | None = Field(default=None, ge=0)
    vendor_source: str | None = Field(default=None, description="Zepp workout detail source")


class WorkoutSample(BaseModel):
    """One normalized heart-rate sample within a workout."""

    workout_id: str = ""
    timestamp: datetime
    heart_rate: int = Field(ge=1, le=300, description="Heart rate in bpm")
    source_scope: str = Field(
        default="unknown",
        description="Sensor provenance; unknown when the vendor detail omits it",
    )
    device_id: str | None = None


class MetricSample(BaseModel):
    """A timestamped measurement in the vendor-neutral data layer."""

    user_id: str = ""
    source: str = "zepp"
    metric: str
    timestamp: datetime
    value: float
    unit: str = ""
    source_scope: str = "unknown"
    device_id: str | None = None


class DailyMetric(BaseModel):
    """A sparse daily metric, kept separate from computed Vitalis scores."""

    user_id: str = ""
    source: str = "zepp"
    date: DateType
    metric: str
    value: float
    unit: str = ""
    source_scope: str = "unknown"
    device_id: str | None = None


class TrainingRecord(BaseModel):
    """某一天的训练汇总。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    date: Optional[DateType] = None
    workout_count: int = 0
    total_duration: int = 0  # 分钟
    total_load: int = 0
    training_status: TrainingReadiness = TrainingReadiness.MODERATE


class ActivityRecord(BaseModel):
    """日常活动（步数/卡路里/运动时长等）。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    date: Optional[DateType] = None
    source: str = "zepp"
    steps: int = Field(default=0, ge=0)
    active_minutes: int = Field(default=0, ge=0)
    calories: int = Field(default=0, ge=0)
    distance_km: float = Field(default=0, ge=0)
    resting_hr: int = Field(default=0, ge=0, description="静息心率 bpm")


class DailyHealth(BaseModel):
    """某一日的健康汇总快照 —— API 与 Agent 的核心数据类型。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    date: DateType

    sleep: SleepRecord | None = None
    activity: ActivityRecord | None = None
    training: TrainingRecord | None = None

    # 由分析引擎生成
    hrv: int | None = Field(default=None, description="今日 HRV（ms），或 7 天均值偏移")
    hrv_trend_pct: float | None = Field(default=None, description="HRV 7 天趋势（相对均值 %）")

    recovery_score: int = Field(default=0, ge=0, le=100, description="恢复分 0-100")
    recovery_level: RecoveryLevel = RecoveryLevel.MODERATE
    stress_level: StressLevel = StressLevel.MEDIUM

    overall_score: int = Field(default=0, ge=0, le=100, description="综合健康分")
    summary: str = Field(default="", description="给人看的自然语言小结（由 AI 生成可选）")


class HealthSnapshot(DailyHealth):
    """DailyHealth 别名：某一日的完整健康快照。"""


class AuthToken(BaseModel):
    """厂商访问令牌：扫码授权或 apptoken 导入后保存，供数据获取使用。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    source: str = "zepp"
    access_token: str
    refresh_token: str = ""
    expires_at: datetime | None = None
    scope: str = ""
    region_host: str = Field(default="", description="区域云端主机，如 api-mifitcn.zepp.com")
    source_user_id: str | None = Field(default=None, description="厂商侧用户 id（取数时使用）")

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        # 提前 60 秒视为过期，给刷新留缓冲
        now = datetime.now(timezone.utc).replace(tzinfo=None)  # 存储为 naive UTC
        return now >= self.expires_at - timedelta(seconds=60)


class AnalysisRecord(BaseModel):
    """分析引擎输出的持久化结果。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    date: datetime = Field(default_factory=datetime.utcnow)

    engine: str = Field(description="rule / statistical / ai")
    kind: str = Field(description="sleep / training / recovery / stress / summary")
    inputs: dict[str, Any] = Field(default_factory=dict)
    output: str = Field(default="", description="结果文本")
    score: int | None = None


class Decision(BaseModel):
    """Rule Engine 的决策输出：结构化的、可解释的结论。"""

    overall_score: int
    recovery_level: RecoveryLevel
    stress_level: StressLevel
    training_readiness: TrainingReadiness
    items: list[str] = Field(default_factory=list, description="命中规则列表（人类可读）")
    reasons: dict[str, Any] = Field(default_factory=dict, description="结构化依据，供 LLM 解释用")
