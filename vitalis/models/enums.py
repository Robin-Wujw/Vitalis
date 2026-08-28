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


class TrainingReadiness(StrEnum):
    NOT_READY = "not_ready"
    EASY = "easy"        # 只适合低强度
    MODERATE = "moderate"
    FULL = "full"        # 可全力训练
