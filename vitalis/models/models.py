"""统一健康数据模型（Vitalis Schema 的核心定义）。"""

import uuid
from datetime import date as DateType, datetime, timedelta, time, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import SleepQuality, TrainingReadiness, WorkoutType


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
    rem_sleep: int | None = Field(
        default=None, ge=0, description="快速眼动睡眠时长（分钟）"
    )
    light_sleep: int = Field(default=0, ge=0, description="浅睡时长（分钟）")
    awake: int = Field(default=0, ge=0, description="清醒时长（分钟）")
    sleep_score: int | None = Field(default=None, ge=0, le=100, description="睡眠评分")
    bedtime: time | None = None
    wake_time: time | None = None
    stages: list["SleepStageSlice"] = Field(default_factory=list)
    wake_count: int | None = Field(default=None, ge=0)

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


class SleepStageSlice(BaseModel):
    """One vendor-observed sleep-stage interval in UTC."""

    stage: Literal["deep", "light", "rem", "awake"]
    start_time: datetime
    end_time: datetime


class Workout(BaseModel):
    """单次训练记录。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    source: str = "zepp"
    workout_id: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None

    type: WorkoutType = WorkoutType.OTHER
    sport_mode: str = Field(default="unknown", description="具体 Zepp 运动模式")
    sport_mode_label: str = Field(default="未知运动", description="运动模式中文名称")
    training_family: str = Field(default="skill", description="训练家族：有氧/力量/灵活性/混合/技巧")
    recognition_confidence: str = Field(default="NONE", description="运动类型识别置信等级")
    recognition_confidence_label: str = Field(default="无法识别", description="运动类型识别置信度中文名称")
    recognition_source: str = Field(default="missing_vendor_type", description="运动类型映射来源")
    recognition_source_label: str = Field(default="缺少厂商运动类型", description="运动类型映射来源中文名称")
    duration: int = Field(ge=0, description="时长（分钟）")
    heart_rate_avg: int = Field(default=0, ge=0, description="平均心率 bpm")
    heart_rate_max: int = Field(default=0, ge=0)
    load: int = Field(default=0, ge=0, description="厂商训练负荷（非负，无固定上限）")
    calories: int = Field(default=0, ge=0)
    distance_km: float | None = Field(default=None, ge=0)
    vendor_source: str | None = Field(default=None, description="Zepp workout detail source")
    vendor_type_id: int | None = Field(
        default=None,
        description="Original numeric vendor workout type retained for mapping audits",
    )
    heart_rate_zone_setting_type: int | None = Field(
        default=None,
        description="Vendor heart-rate-zone model identifier from the workout summary",
    )
    heart_rate_zone_boundaries_bpm: list[int] = Field(
        default_factory=list,
        description="Six strictly increasing device boundaries from heart_range",
    )


class WorkoutMetricSample(BaseModel):
    """One typed metric observation within a workout."""

    source: str = "zepp"
    workout_id: str = ""
    timestamp: datetime
    metric: Literal[
        "heart_rate",
        "speed",
        "equivalent_pace",
        "cadence",
        "stride_length",
        "distance",
        "altitude",
        "running_power",
        "ground_contact_time",
        "vertical_oscillation",
        "vertical_stride_ratio",
    ]
    value: float
    unit: str
    source_scope: str = Field(
        default="workout_detail",
        description="Sensor provenance; unknown when the vendor detail omits it",
    )
    device_id: str | None = None


class WorkoutLap(BaseModel):
    """A vendor-recorded workout lap with only semantically verified fields."""

    index: int = Field(ge=0)
    duration_seconds: int = Field(ge=0)
    distance_meters: float = Field(ge=0)


class WorkoutPause(BaseModel):
    """A vendor-recorded pause interval."""

    started_at: datetime
    duration_seconds: int = Field(ge=0)


class StrengthSetObservation(BaseModel):
    """One explicit vendor strength set; absent fields remain absent."""

    started_at: datetime | None = None
    ended_at: datetime | None = None
    exercise_id: str | None = None
    exercise_name: str | None = None
    repetitions: int | None = Field(default=None, ge=1)
    weight_kg: float | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    rest_seconds: int | None = Field(default=None, ge=0)


class WorkoutDetail(BaseModel):
    """Current normalized workout-detail contract."""

    schema_version: Literal["4.0"] = "4.0"
    workout_id: str
    metrics_present: list[str] = Field(default_factory=list)
    metric_sample_counts: dict[str, int] = Field(default_factory=dict)
    laps: list[WorkoutLap] = Field(default_factory=list)
    pauses: list[WorkoutPause] = Field(default_factory=list)
    strength_sets: list[StrengthSetObservation] = Field(default_factory=list)
    samples: list[WorkoutMetricSample] = Field(default_factory=list, exclude=True)


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


class DenseDataFile(BaseModel):
    """One indexed vendor file that may contain a dense sensor series."""

    user_id: str = ""
    source: str = "zepp"
    stream: str
    file_id: str
    file_type: str = ""
    date: DateType | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    source_scope: str = "unknown"
    device_id: str | None = None
    parse_status: str = "indexed"
    sample_count: int = 0


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
    source: str = "zepp"
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


class NormalizedDaily(BaseModel):
    """One day's normalized source records without derived intelligence fields."""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    date: DateType

    sleep: SleepRecord | None = None
    activity: ActivityRecord | None = None
    training: TrainingRecord | None = None
    metric_samples: list[MetricSample] = Field(default_factory=list)


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
