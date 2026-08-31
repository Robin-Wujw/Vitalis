"""Vitalis Health Agent - 统一健康数据模型（Vitalis Schema）。

设计原则：
1. 不保存厂商格式 —— 所有连接器在 parser 层把数据转换为这里的统一模型。
2. 所有字段使用统一单位：
   - 时长 time: 分钟 (minutes)
   - 心率 heart_rate: bpm
   - 负荷 load: 厂商训练负荷（非负，不假设固定上限）
3. Pydantic v2 模型，同时作为 API 响应和存储前的校验层。
"""

from .enums import SleepQuality, TrainingReadiness, WorkoutType
from .models import (
    ActivityRecord,
    AuthToken,
    DailyMetric,
    DenseDataFile,
    NormalizedDaily,
    Device,
    MetricSample,
    SleepRecord,
    SleepStageSlice,
    TrainingRecord,
    User,
    Workout,
    WorkoutDetail,
    WorkoutLap,
    WorkoutMetricSample,
    WorkoutPause,
    StrengthSetObservation,
)

__all__ = [
    "ActivityRecord",
    "AuthToken",
    "DailyMetric",
    "DenseDataFile",
    "NormalizedDaily",
    "Device",
    "MetricSample",
    "SleepQuality",
    "SleepRecord",
    "SleepStageSlice",
    "TrainingReadiness",
    "TrainingRecord",
    "User",
    "Workout",
    "WorkoutDetail",
    "WorkoutLap",
    "WorkoutMetricSample",
    "WorkoutPause",
    "StrengthSetObservation",
    "WorkoutType",
]
