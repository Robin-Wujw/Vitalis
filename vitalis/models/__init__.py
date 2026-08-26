"""Vitalis Health Agent - 统一健康数据模型（Vitalis Schema）。

设计原则：
1. 不保存厂商格式 —— 所有连接器在 parser 层把数据转换为这里的统一模型。
2. 所有字段使用统一单位：
   - 时长 time: 分钟 (minutes)
   - 心率 heart_rate: bpm
   - 负荷 load: 相对单位（0~100）
3. Pydantic v2 模型，同时作为 API 响应和存储前的校验层。
"""

from .enums import RecoveryLevel, SleepQuality, StressLevel, TrainingReadiness, WorkoutType
from .models import (
    ActivityRecord,
    AnalysisRecord,
    AuthToken,
    DailyMetric,
    DailyHealth,
    Decision,
    Device,
    HealthSnapshot,
    MetricSample,
    SleepRecord,
    TrainingRecord,
    User,
    Workout,
)

__all__ = [
    "ActivityRecord",
    "AnalysisRecord",
    "AuthToken",
    "DailyMetric",
    "DailyHealth",
    "Decision",
    "Device",
    "HealthSnapshot",
    "MetricSample",
    "RecoveryLevel",
    "SleepQuality",
    "SleepRecord",
    "StressLevel",
    "TrainingReadiness",
    "TrainingRecord",
    "User",
    "Workout",
    "WorkoutType",
]
