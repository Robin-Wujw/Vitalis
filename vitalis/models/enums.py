"""统一枚举：所有枚举值跨数据源保持一致。"""

from enum import StrEnum


class WorkoutType(StrEnum):
    STRENGTH = "strength"
    RUNNING = "running"
    CYCLING = "cycling"
    SWIMMING = "swimming"
    WALKING = "walking"
    HIIT = "hiit"
    YOGA = "yoga"
    OTHER = "other"


class SleepQuality(StrEnum):
    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"
    EXCELLENT = "excellent"


class StressLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecoveryLevel(StrEnum):
    LOW = "low"          # 恢复不足，需要休息
    MODERATE = "moderate"
    READY = "ready"      # 恢复良好，适合训练
    OVERREACH = "overreach"  # 过度训练


class TrainingReadiness(StrEnum):
    NOT_READY = "not_ready"
    EASY = "easy"        # 只适合低强度
    MODERATE = "moderate"
    FULL = "full"        # 可全力训练
