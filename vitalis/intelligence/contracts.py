"""Versioned contracts shared by the intelligence engine and its consumers."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


SCHEMA_VERSION = "1.0"
MODEL_VERSION = "vitalis-intelligence-1"


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class QualityStatus(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class ConfidenceBand(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class DecisionAction(str, Enum):
    TRAIN_HARD = "TRAIN_HARD"
    TRAIN_NORMAL = "TRAIN_NORMAL"
    TRAIN_LIGHT = "TRAIN_LIGHT"
    RECOVERY = "RECOVERY"
    REST = "REST"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class RecoveryState(str, Enum):
    GOOD = "GOOD"
    NORMAL = "NORMAL"
    SUPPRESSED = "SUPPRESSED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SleepState(str, Enum):
    ABOVE_BASELINE = "ABOVE_BASELINE"
    NEAR_BASELINE = "NEAR_BASELINE"
    BELOW_BASELINE = "BELOW_BASELINE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class LoadState(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Provenance(BaseModel):
    source: str
    source_scope: str = "unknown"
    device_id: str | None = None


class MeasurementFact(BaseModel):
    metric: str
    value: float
    unit: str
    observed_at: datetime | date
    provenance: Provenance


class Coverage(BaseModel):
    metric: str
    sample_count: int = Field(ge=0)
    distinct_days: int = Field(ge=0)
    first_observed_at: datetime | date | None = None
    last_observed_at: datetime | date | None = None
    device_ids: list[str] = Field(default_factory=list)


class QualityFlag(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"]
    detail: str


class DeviceValidity(BaseModel):
    """Evidence metadata, never a synthetic measurement-quality probability."""

    device_id: str
    status: Literal["UNKNOWN", "SUPPORTED_BY_EVIDENCE", "LIMITED_BY_EVIDENCE"] = "UNKNOWN"
    evidence_refs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class DataQuality(BaseModel):
    status: QualityStatus
    status_label: str = ""
    required_signals: list[str] = Field(default_factory=list)
    required_signal_labels: list[str] = Field(default_factory=list)
    missing_required_signals: list[str] = Field(default_factory=list)
    missing_required_signal_labels: list[str] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    flags: list[QualityFlag] = Field(default_factory=list)
    device_validity: list[DeviceValidity] = Field(default_factory=list)


class BaselineStats(BaseModel):
    status: Availability
    metric: str
    source: str
    source_scope: str
    device_id: str | None
    unit: str
    window_days: Literal[7, 28]
    transform: Literal["identity", "natural_log"] = "identity"
    sample_count: int = Field(ge=0)
    distinct_days: int = Field(ge=0)
    minimum_days: int = Field(ge=1)
    coverage_ratio: float = Field(ge=0, le=1)
    median: float | None = None
    mad: float | None = None
    percentile_25: float | None = None
    percentile_75: float | None = None
    trend_per_day: float | None = None
    reference_value: float | None = Field(
        default=None,
        description="Median expressed in the original measurement unit",
    )


class Deviation(BaseModel):
    metric: str
    baseline_window_days: Literal[7, 28]
    percent: float | None = None
    robust_z: float | None = None
    direction: Literal["above", "near", "below", "unknown"] = "unknown"


class SleepFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    duration_minutes: int | None = None
    bedtime: str | None = None
    wake_time: str | None = None
    deep_minutes: int | None = None
    rem_minutes: int | None = None
    awake_minutes: int | None = None
    vendor_sleep_score: int | None = None
    duration_deviation: Deviation | None = None
    regularity_minutes: float | None = None
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class HrvStreamFeature(BaseModel):
    metric: Literal["hrv_rmssd", "hrv_sdnn", "sleep_hrv"]
    device_id: str | None = None
    value_ms: float
    ln_rmssd: float | None = None
    deviation: Deviation | None = None
    selected: bool = False


class HrvFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    preferred_metric: Literal["hrv_rmssd", "hrv_sdnn", "sleep_hrv"] | None = None
    preferred_device_id: str | None = None
    value_ms: float | None = None
    ln_rmssd: float | None = None
    deviation: Deviation | None = None
    rhr_bpm: float | None = None
    rhr_deviation: Deviation | None = None
    streams: list[HrvStreamFeature] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class RecoveryFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    state: RecoveryState
    state_label: str = ""
    positive_signals: list[str] = Field(default_factory=list)
    positive_signal_labels: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    negative_signal_labels: list[str] = Field(default_factory=list)
    vendor_readiness: float | None = None
    vendor_charge: float | None = None
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class WorkoutFeature(BaseModel):
    date: date
    type: str
    type_label: str
    sport_mode: str
    sport_mode_label: str
    training_family: str
    training_family_label: str
    recognition_confidence: str
    recognition_confidence_label: str
    recognition_source: str
    recognition_source_label: str
    vendor_type_id: int | None = None
    duration_minutes: int = Field(ge=0)
    vendor_load: float = Field(ge=0)
    heart_rate_avg_bpm: int | None = Field(default=None, ge=1)
    heart_rate_max_bpm: int | None = Field(default=None, ge=1)
    detail_available: bool = False


class TrainingFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    today_duration_minutes: int | None = None
    today_load: float | None = None
    today_workouts: int | None = None
    duration_7d: int | None = None
    load_7d: float | None = None
    load_28d: float | None = None
    aerobic_minutes_7d: int | None = None
    strength_sessions_7d: int | None = None
    days_since_last_strength: int | None = Field(default=None, ge=0)
    workout_type_counts_7d: dict[str, int] = Field(default_factory=dict)
    workout_type_labels_7d: dict[str, str] = Field(default_factory=dict)
    sport_mode_counts_7d: dict[str, int] = Field(default_factory=dict)
    recent_workouts: list[WorkoutFeature] = Field(default_factory=list)
    load_deviation: Deviation | None = None
    load_state: LoadState = LoadState.INSUFFICIENT_DATA
    load_state_label: str = ""
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class ProfileFeatures(BaseModel):
    sleep: SleepFeatures
    hrv: HrvFeatures
    recovery: RecoveryFeatures
    training: TrainingFeatures


class ProfileStates(BaseModel):
    sleep: SleepState = SleepState.INSUFFICIENT_DATA
    recovery: RecoveryState = RecoveryState.INSUFFICIENT_DATA
    training_load: LoadState = LoadState.INSUFFICIENT_DATA
    sleep_label: str = ""
    recovery_label: str = ""
    training_load_label: str = ""


class TrainingStep(BaseModel):
    order: int = Field(ge=1)
    name: str
    duration_minutes: tuple[int, int] | None = None
    sets: int | None = Field(default=None, ge=1)
    repetitions: str | None = None
    rest_seconds: tuple[int, int] | None = None
    intensity: str | None = None
    instructions: list[str] = Field(default_factory=list)


class TrainingPrescription(BaseModel):
    code: str
    title: str
    goal: str
    total_duration_minutes: tuple[int, int] | None = None
    steps: list[TrainingStep] = Field(default_factory=list)
    progression: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class TrainingDecision(BaseModel):
    action: DecisionAction
    action_label: str = ""
    confidence: ConfidenceBand
    confidence_label: str = ""
    drivers: list[str] = Field(default_factory=list)
    driver_labels: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    suggested_types: list[str] = Field(default_factory=list)
    suggested_type_labels: list[str] = Field(default_factory=list)
    intensity: Literal["high", "moderate", "low", "none", "undetermined"] = "undetermined"
    intensity_label: str = ""
    duration_minutes: tuple[int, int] | None = None
    prescription_guidance: str = ""
    prescriptions: list[TrainingPrescription] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    id: str
    title: str
    url: str
    applies_to: list[str] = Field(default_factory=list)


class DailyProfile(BaseModel):
    schema_version: str = SCHEMA_VERSION
    model_version: str = MODEL_VERSION
    user_id: str
    date: date
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: DataQuality
    facts: dict[str, list[MeasurementFact]] = Field(default_factory=dict)
    baselines: dict[str, list[BaselineStats]] = Field(default_factory=dict)
    features: ProfileFeatures
    states: ProfileStates
    decision: TrainingDecision
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
