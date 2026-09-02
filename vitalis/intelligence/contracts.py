"""Versioned contracts shared by the intelligence engine and its consumers."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DateValue = date


DAILY_SCHEMA_VERSION = "10.0"
WEEKLY_SCHEMA_VERSION = "4.0"
MONTHLY_SCHEMA_VERSION = "2.0"
INTELLIGENCE_VERSION = "10.0"
DECISION_POLICY_VERSION = "7.0"
EVIDENCE_VERSION = "2026-09a"
TRAINING_RESPONSE_SCHEMA_VERSION = "1.0"
PERSONAL_MODEL_SCHEMA_VERSION = "2.0"
ASSOCIATION_SCHEMA_VERSION = "1.0"
USER_PROFILE_SCHEMA_VERSION = "1.0"
AGENT_CONTEXT_SCHEMA_VERSION = "5.0"

ProfileValue = TypeVar("ProfileValue")


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


class TrendDirection(str, Enum):
    RISING = "RISING"
    STABLE = "STABLE"
    FALLING = "FALLING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class EventSeverity(str, Enum):
    INFO = "INFO"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class AnalysisRunStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Sex(str, Enum):
    FEMALE = "FEMALE"
    MALE = "MALE"
    INTERSEX = "INTERSEX"
    PREFER_NOT_TO_SAY = "PREFER_NOT_TO_SAY"


class ProfileSource(str, Enum):
    USER_CONFIRMED = "USER_CONFIRMED"


class ProfileField(BaseModel, Generic[ProfileValue]):
    value: ProfileValue
    source: ProfileSource
    confidence: ConfidenceBand
    revision: int = Field(ge=1)
    updated_at: datetime

    @field_validator("source", mode="before")
    @classmethod
    def validate_source(cls, value: object) -> ProfileSource:
        try:
            source = ProfileSource(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("profile field source must be USER_CONFIRMED") from exc
        if source is not ProfileSource.USER_CONFIRMED:
            raise ValueError("profile field source must be USER_CONFIRMED")
        return source


class UserProfile(BaseModel):
    schema_version: Literal["1.0"] = USER_PROFILE_SCHEMA_VERSION
    user_id: str
    revision: int = Field(default=0, ge=0)
    sex: ProfileField[Sex] | None = None
    confirmed_hrmax_bpm: ProfileField[int] | None = None
    sleep_target_minutes: ProfileField[int] | None = None


class UserProfilePatch(BaseModel):
    expected_revision: int = Field(ge=0)
    sex: Sex | None = None
    confirmed_hrmax_bpm: int | None = Field(default=None, ge=100, le=240)
    sleep_target_minutes: int | None = Field(default=None, ge=180, le=900)


class ProfileRevisionConflict(ValueError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"用户档案版本冲突：期望 revision {expected}，当前为 {actual}"
        )


class MissingUserInput(BaseModel):
    field: Literal["sex", "confirmed_hrmax_bpm", "sleep_target_minutes"]
    label: str
    question: str
    reason: str
    blocking: bool = True
    required_for: list[str] = Field(default_factory=list, max_length=4)
    patch_path: Literal["/api/v1/intelligence/profile"] = "/api/v1/intelligence/profile"


class ContextProfile(BaseModel):
    revision: int = Field(ge=0)
    sex: Sex | None = None
    confirmed_hrmax_bpm: int | None = Field(default=None, ge=100, le=240)
    sleep_target_minutes: int | None = Field(default=None, ge=180, le=900)


class RecommendationStatus(str, Enum):
    PLANNED = "PLANNED"
    COMPLETED = "COMPLETED"


class RecoveryOutcome(str, Enum):
    RETURNED_TO_BASELINE = "RETURNED_TO_BASELINE"
    NOT_RETURNED = "NOT_RETURNED"
    CONFOUNDED = "CONFOUNDED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class EventLifecycle(str, Enum):
    DETECTED = "DETECTED"
    PERSISTING = "PERSISTING"
    IMPROVING = "IMPROVING"
    RESOLVED = "RESOLVED"


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
    device_label: str | None = None
    measurement_site: Literal["upper_arm", "wrist", "unknown"] = "unknown"
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
    baseline_window_days: Literal[7, 21, 28]
    percent: float | None = None
    robust_z: float | None = None
    direction: Literal["above", "near", "below", "unknown"] = "unknown"


class TrendFeature(BaseModel):
    metric: str
    metric_label: str
    window_days: Literal[7, 28, 90]
    source: str
    source_scope: str
    device_id: str | None = None
    unit: str
    status: Availability
    status_label: str
    current_distinct_days: int = Field(ge=0)
    previous_distinct_days: int = Field(ge=0)
    minimum_days: int = Field(ge=1)
    coverage_ratio: float = Field(ge=0, le=1)
    current_median: float | None = None
    previous_median: float | None = None
    change_percent: float | None = None
    slope_per_day: float | None = None
    variability_mad: float | None = None
    direction: TrendDirection
    direction_label: str
    confidence: ConfidenceBand
    confidence_label: str


class HealthEventEvidence(BaseModel):
    fact: str
    fact_label: str
    value: float | str | None = None
    unit: str | None = None


class HealthEvent(BaseModel):
    id: str
    type: str
    type_label: str
    severity: EventSeverity
    severity_label: str
    metric: str | None = None
    metric_label: str | None = None
    start_date: date
    end_date: date
    duration_days: int = Field(ge=1)
    deviation_percent: float | None = None
    baseline_window_days: int | None = None
    confidence: ConfidenceBand
    confidence_label: str
    summary: str
    evidence: list[HealthEventEvidence] = Field(default_factory=list)
    lifecycle: EventLifecycle = EventLifecycle.DETECTED
    lifecycle_label: str = "已发现"
    last_observed_date: date | None = None
    last_evaluated_date: date | None = None
    resolved_at: date | None = None
    acknowledged: bool = False
    acknowledged_at: datetime | None = None


class HealthEventObservation(BaseModel):
    id: str
    analysis_run_id: str
    event_id: str
    user_id: str
    date: DateValue
    detected: bool
    previous_lifecycle: EventLifecycle | None = None
    lifecycle: EventLifecycle
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    wake_count: int | None = Field(default=None, ge=0)
    wake_count_deviation: Deviation | None = None
    stage_timeline_available: bool = False
    stage_slice_count: int = Field(default=0, ge=0)
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class HrvStreamFeature(BaseModel):
    metric: Literal["hrv_rmssd", "hrv_sdnn", "sleep_hrv"]
    device_id: str | None = None
    device_label: str | None = None
    measurement_site: Literal["upper_arm", "wrist", "unknown"] = "unknown"
    value_ms: float
    ln_rmssd: float | None = None
    deviation: Deviation | None = None
    sample_count_today: int = Field(default=0, ge=0)
    baseline_distinct_days: int = Field(default=0, ge=0)
    selected: bool = False


class HrvCurvePoint(BaseModel):
    time: str
    median_ms: float = Field(gt=0)
    sample_count: int = Field(ge=1)


class DailyHrvCurveFeature(BaseModel):
    metric: Literal["hrv_rmssd"] = "hrv_rmssd"
    bin_minutes: Literal[1] = 1
    device_id: str | None = None
    device_label: str
    selection_basis: Literal["widest_target_day_coverage"] = "widest_target_day_coverage"
    sample_count: int = Field(ge=1)
    covered_minutes: int = Field(ge=1, le=1440)
    first_sample_time: str
    last_sample_time: str
    points: list[HrvCurvePoint] = Field(min_length=1, max_length=1440)


class DailySleepHrvPoint(BaseModel):
    date: DateValue
    value_ms: float = Field(gt=0)
    sample_count: int = Field(ge=1)


class DailySleepHrvTrendFeature(BaseModel):
    """One comparable nightly sleep-HRV value per local calendar day."""

    metric: Literal["sleep_hrv"] = "sleep_hrv"
    aggregation: Literal["daily_median"] = "daily_median"
    device_id: str | None = None
    device_label: str
    today_value_ms: float = Field(gt=0)
    today_sample_count: int = Field(ge=1)
    points: list[DailySleepHrvPoint] = Field(min_length=1, max_length=7)


class HeartRateCoverageFeature(BaseModel):
    """Indexed high-frequency coverage; values remain unavailable until decoded."""

    device_id: str
    device_label: str
    measurement_site: Literal["upper_arm", "wrist", "unknown"] = "unknown"
    today_coverage_minutes: float = Field(default=0, ge=0)
    coverage_hours_28d: float = Field(default=0, ge=0)
    covered_days_28d: int = Field(default=0, ge=0, le=28)
    file_count_28d: int = Field(default=0, ge=0)
    payload_decoded: bool = False


class NocturnalHeartRateFeature(BaseModel):
    status: Availability
    status_label: str = ""
    device_id: str | None = None
    device_label: str | None = None
    measurement_site: Literal["upper_arm", "wrist", "unknown"] = "unknown"
    median_bpm: float | None = Field(default=None, ge=1)
    low_5m_bpm: float | None = Field(default=None, ge=1)
    low_5m_time: str | None = None
    first_half_median_bpm: float | None = Field(default=None, ge=1)
    second_half_median_bpm: float | None = Field(default=None, ge=1)
    second_minus_first_bpm: float | None = None
    sample_count: int = Field(default=0, ge=0)
    coverage_ratio: float = Field(default=0, ge=0, le=1)
    baseline_median_bpm: float | None = Field(default=None, ge=1)
    baseline_nights: int = Field(default=0, ge=0, le=28)
    deviation_percent: float | None = None
    direction: Literal["above", "near", "below", "unknown"] = "unknown"


class HrvFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    preferred_metric: Literal["hrv_rmssd", "hrv_sdnn", "sleep_hrv"] | None = None
    preferred_device_id: str | None = None
    preferred_device_label: str | None = None
    value_ms: float | None = None
    ln_rmssd: float | None = None
    deviation: Deviation | None = None
    recent_7d_median_ms: float | None = Field(default=None, ge=0)
    previous_7d_median_ms: float | None = Field(default=None, ge=0)
    recent_7d_change_percent: float | None = None
    recent_7d_direction: Literal["above", "near", "below", "unknown"] = "unknown"
    recent_7d_days: int = Field(default=0, ge=0, le=7)
    previous_7d_days: int = Field(default=0, ge=0, le=7)
    fusion_method: Literal[
        "vendor_fused_with_device_audit",
        "canonical_device_with_corroboration",
    ] | None = None
    fusion_direction: Literal["above", "near", "below", "unknown"] = "unknown"
    fusion_confidence: ConfidenceBand = ConfidenceBand.NONE
    fusion_confidence_label: str = ""
    fusion_summary: str = ""
    corroboration_status: Literal[
        "not_available", "consistent", "conflicting", "insufficient"
    ] = "not_available"
    corroborating_stream_count: int = Field(default=0, ge=0)
    corroboration_affects_decision: bool = False
    rhr_bpm: float | None = None
    rhr_metric: Literal["sleep_rhr", "resting_hr", "nocturnal_heart_rate"] | None = None
    rhr_source_scope: str | None = None
    rhr_device_id: str | None = None
    rhr_deviation: Deviation | None = None
    nocturnal_heart_rate: NocturnalHeartRateFeature | None = None
    daily_curve: DailyHrvCurveFeature | None = None
    sleep_hrv_daily_trend: DailySleepHrvTrendFeature | None = None
    streams: list[HrvStreamFeature] = Field(default_factory=list)
    heart_rate_coverage: list[HeartRateCoverageFeature] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class OxygenFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    device_id: str | None = None
    median_percent: float | None = Field(default=None, ge=50, le=100)
    lower_10th_percent: float | None = Field(default=None, ge=50, le=100)
    sample_count: int = Field(default=0, ge=0)
    measured_minutes: int | None = Field(default=None, ge=0)
    coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    odi_events_per_hour: float | None = Field(default=None, ge=0)
    odi_events: int | None = Field(default=None, ge=0)
    odi_baseline: float | None = Field(default=None, ge=0)
    odi_deviation: Deviation | None = None
    repeated_elevation: bool = False
    interpretation: Literal[
        "within_personal_range",
        "single_night_elevation",
        "repeated_elevation",
        "insufficient",
    ] = "insufficient"
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class OvernightVitalsFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    respiratory_rate: float | None = Field(default=None, ge=1)
    respiratory_rate_deviation: Deviation | None = None
    skin_temperature_delta_c: float | None = None
    oxygen: OxygenFeatures
    outlier_labels: list[str] = Field(default_factory=list)
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
    vendor_readiness_components: dict[str, float] = Field(default_factory=dict)
    vendor_charge_components: dict[str, float] = Field(default_factory=dict)
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


class RunningHeartRateZone(BaseModel):
    zone: Literal[1, 2, 3, 4, 5]
    label: str
    lower_bpm: int | None = Field(default=None, ge=1)
    upper_bpm: int | None = Field(default=None, ge=1)
    duration_seconds: int = Field(ge=0)
    share_percent: float = Field(ge=0, le=100)


class RunningSegment(BaseModel):
    kind: Literal["work", "recovery"]
    kind_label: str
    start_offset_seconds: int = Field(ge=0)
    end_offset_seconds: int = Field(ge=0)
    duration_seconds: int = Field(ge=1)
    average_speed_mps: float | None = Field(default=None, ge=0)
    average_pace_seconds_per_km: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=1)


class RunningKilometerSplit(BaseModel):
    index: int = Field(ge=1)
    distance_km: float = Field(gt=0, le=1)
    elapsed_seconds: float = Field(gt=0)
    moving_seconds: float = Field(gt=0)
    average_pace_seconds_per_km: float = Field(gt=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=1)
    maximum_heart_rate_bpm: float | None = Field(default=None, ge=1)
    elevation_gain_m: float | None = Field(default=None, ge=0)
    elevation_loss_m: float | None = Field(default=None, ge=0)


class ComparableRunBaseline(BaseModel):
    sample_count: int = Field(ge=3, le=10)
    window_days: Literal[180] = 180
    distance_tolerance_percent: Literal[20] = 20
    workout_sources: list[str] = Field(min_length=3, max_length=10)
    workout_ids: list[str] = Field(min_length=3, max_length=10)
    median_pace_seconds_per_km: float = Field(gt=0)
    pace_difference_percent: float
    median_heart_rate_bpm: float | None = Field(default=None, ge=1)
    heart_rate_difference_bpm: float | None = None
    median_power_watts: float | None = Field(default=None, ge=0)
    power_difference_percent: float | None = None


class RunningSessionAnalysis(BaseModel):
    workout_id: str
    source: str = "zepp"
    date: DateValue
    classification: Literal[
        "RECOVERY_RUN",
        "EASY_RUN",
        "STEADY_RUN",
        "TEMPO_RUN",
        "INTERVAL_RUN",
        "LONG_RUN",
        "UNCLASSIFIED",
    ]
    classification_label: str
    confidence: ConfidenceBand
    confidence_label: str
    duration_minutes: int = Field(ge=0)
    moving_duration_minutes: float = Field(default=0, ge=0)
    pause_duration_seconds: int = Field(default=0, ge=0)
    distance_km: float | None = Field(default=None, ge=0)
    average_pace_seconds_per_km: float | None = Field(default=None, ge=0)
    median_speed_mps: float | None = Field(default=None, ge=0)
    median_cadence_spm: float | None = Field(default=None, ge=0)
    median_stride_length_cm: float | None = Field(default=None, ge=0)
    cadence_variability_percent: float | None = Field(default=None, ge=0)
    average_power_watts: float | None = Field(default=None, ge=0)
    median_equivalent_pace_seconds_per_km: float | None = Field(default=None, ge=0)
    median_ground_contact_time_ms: float | None = Field(default=None, ge=0)
    median_vertical_oscillation_mm: float | None = Field(default=None, ge=0)
    median_vertical_stride_ratio_percent: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=1)
    maximum_heart_rate_bpm: float | None = Field(default=None, ge=1)
    heart_rate_zone_source: Literal["device_workout", "lactate_threshold", "unavailable"] = "unavailable"
    heart_rate_zone_boundaries_bpm: list[int] = Field(default_factory=list)
    heart_rate_zones: list[RunningHeartRateZone] = Field(default_factory=list)
    cardiac_drift_percent: float | None = None
    comparable_baseline: ComparableRunBaseline | None = None
    segments: list[RunningSegment] = Field(default_factory=list, max_length=20)
    kilometer_splits: list[RunningKilometerSplit] = Field(default_factory=list, max_length=200)
    evidence: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RunningAnalysis(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    status: Availability
    status_label: str
    zone_method: Literal["device_workout", "lactate_threshold", "mixed", "unavailable"] = "unavailable"
    lactate_threshold_bpm: float | None = Field(default=None, ge=1)
    sessions_7d: int = Field(ge=0)
    duration_minutes_7d: int = Field(ge=0)
    distance_km_7d: float | None = Field(default=None, ge=0)
    sessions_28d: int = Field(ge=0)
    duration_minutes_28d: int = Field(ge=0)
    distance_km_28d: float | None = Field(default=None, ge=0)
    distance_change_percent: float | None = None
    session_type_counts_28d: dict[str, int] = Field(default_factory=dict)
    recent_sessions: list[RunningSessionAnalysis] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list)


class StrengthExerciseInput(BaseModel):
    exercise_name: str = Field(min_length=1, max_length=100)
    exercise_id: str | None = Field(default=None, max_length=100)
    sets: int | None = Field(default=None, ge=1, le=100)
    repetitions: str | None = Field(default=None, max_length=50)
    weight_kg: float | None = Field(default=None, ge=0, le=2000)
    rpe: float | None = Field(default=None, ge=1, le=10)
    rir: float | None = Field(default=None, ge=0, le=10)
    rest_seconds: int | None = Field(default=None, ge=0, le=3600)


class StrengthWorkoutConfirmationInput(BaseModel):
    session_focus: Literal[
        "PUSH", "PULL", "LEGS", "UPPER", "LOWER", "FULL_BODY",
        "CHEST", "BACK", "SHOULDERS", "ARMS",
    ] | None = None
    exercises: list[StrengthExerciseInput] = Field(min_length=1, max_length=50)


class StrengthExerciseRecord(BaseModel):
    id: str
    user_id: str
    workout_source: str = "zepp"
    workout_id: str
    order: int = Field(ge=1)
    exercise_name: str
    exercise_id: str | None = None
    session_focus: str | None = None
    movement_pattern: Literal[
        "squat",
        "hinge",
        "horizontal_push",
        "horizontal_pull",
        "vertical_push",
        "vertical_pull",
        "unilateral_leg",
        "core",
        "carry",
        "isolation",
        "unknown",
    ]
    movement_pattern_label: str
    muscle_groups: list[str] = Field(default_factory=list)
    muscle_group_labels: list[str] = Field(default_factory=list)
    sets: int | None = Field(default=None, ge=1)
    repetitions: str | None = None
    weight_kg: float | None = Field(default=None, ge=0)
    rpe: float | None = Field(default=None, ge=1, le=10)
    rir: float | None = Field(default=None, ge=0, le=10)
    rest_seconds: int | None = Field(default=None, ge=0)
    source: Literal["user_confirmed", "vendor_explicit"]
    confidence: ConfidenceBand
    confidence_label: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExerciseHypothesis(BaseModel):
    exercise_name: str | None = None
    movement_pattern: str = "unknown"
    movement_pattern_label: str = "动作模式未知"
    muscle_groups: list[str] = Field(default_factory=list)
    muscle_group_labels: list[str] = Field(default_factory=list)
    estimated_work_bouts: int | None = Field(default=None, ge=1)
    confidence: ConfidenceBand
    confidence_label: str
    source: Literal["heart_rate_structure", "rotation_history"]
    evidence: list[str] = Field(default_factory=list)


class StrengthSessionAnalysis(BaseModel):
    workout_id: str
    source: str = "zepp"
    date: DateValue
    duration_minutes: int = Field(ge=0)
    focus: Literal[
        "PUSH", "PULL", "LEGS", "UPPER", "LOWER", "FULL_BODY",
        "CHEST", "BACK", "SHOULDERS", "ARMS", "UNKNOWN",
    ]
    focus_label: str
    confidence: ConfidenceBand
    confidence_label: str
    explicit_exercises: list[StrengthExerciseRecord] = Field(default_factory=list)
    hypotheses: list[ExerciseHypothesis] = Field(default_factory=list)
    movement_patterns: list[str] = Field(default_factory=list)
    movement_pattern_labels: list[str] = Field(default_factory=list)
    muscle_groups: list[str] = Field(default_factory=list)
    muscle_group_labels: list[str] = Field(default_factory=list)
    total_sets: int | None = Field(default=None, ge=0)
    estimated_work_bouts: int | None = Field(default=None, ge=0)
    median_work_seconds: float | None = Field(default=None, ge=0)
    median_rest_seconds: float | None = Field(default=None, ge=0)
    average_heart_rate_bpm: float | None = Field(default=None, ge=1)
    maximum_heart_rate_bpm: float | None = Field(default=None, ge=1)
    heart_rate_zones: list[RunningHeartRateZone] = Field(default_factory=list)
    session_rpe: float | None = Field(default=None, ge=1, le=10)
    muscle_soreness: int | None = Field(default=None, ge=1, le=5)
    limitations: list[str] = Field(default_factory=list)


class MuscleRecoveryStatus(BaseModel):
    muscle_group: str
    muscle_group_label: str
    days_since_last_trained: int | None = Field(default=None, ge=0)
    last_session_rpe: float | None = Field(default=None, ge=1, le=10)
    latest_soreness: int | None = Field(default=None, ge=1, le=5)


class StrengthAnalysis(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Availability
    status_label: str
    sessions_7d: int = Field(ge=0)
    sessions_28d: int = Field(ge=0)
    explicit_session_coverage: float = Field(ge=0, le=1)
    detected_split: Literal[
        "FULL_BODY", "UPPER_LOWER", "PUSH_PULL_LEGS", "FIVE_DAY", "UNRESOLVED"
    ] = "UNRESOLVED"
    detected_split_label: str = "尚未识别训练分化"
    split_confidence: ConfidenceBand = ConfidenceBand.NONE
    split_confidence_label: str = "无"
    next_focus: str | None = None
    next_focus_label: str | None = None
    recent_sessions: list[StrengthSessionAnalysis] = Field(default_factory=list, max_length=8)
    muscle_recovery: list[MuscleRecoveryStatus] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class TrainingStatusFeatures(BaseModel):
    """Longer-horizon fitness facts; never used as a same-day recovery score."""

    status: Availability
    status_label: str = ""
    vo2max_ml_kg_min: float | None = Field(default=None, gt=0)
    vo2max_observed_on: date | None = None
    vo2max_change_28d_percent: float | None = None
    lactate_threshold_hr_bpm: float | None = Field(default=None, gt=0)
    lactate_threshold_pace_seconds_per_km: float | None = Field(default=None, gt=0)
    lactate_threshold_observed_on: date | None = None
    lactate_threshold_pace_change_28d_percent: float | None = None
    pai_earned_7d: float | None = Field(default=None, ge=0)
    pai_low_7d: float | None = Field(default=None, ge=0)
    pai_medium_7d: float | None = Field(default=None, ge=0)
    pai_high_7d: float | None = Field(default=None, ge=0)
    dominant_pai_zone: Literal["low", "medium", "high"] | None = None
    dominant_pai_zone_label: str | None = None
    conclusions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class TrainingFeatures(BaseModel):
    status: Availability
    status_label: str = ""
    today_duration_minutes: int | None = None
    today_load: float | None = None
    today_workouts: int | None = None
    duration_7d: int | None = None
    load_7d: float | None = None
    load_7d_reference: float | None = None
    load_7d_change_percent: float | None = None
    load_28d: float | None = None
    aerobic_minutes_7d: int | None = None
    strength_sessions_7d: int | None = None
    days_since_last_strength: int | None = Field(default=None, ge=0)
    workout_type_counts_7d: dict[str, int] = Field(default_factory=dict)
    workout_type_labels_7d: dict[str, str] = Field(default_factory=dict)
    sport_mode_counts_7d: dict[str, int] = Field(default_factory=dict)
    recent_workouts: list[WorkoutFeature] = Field(default_factory=list)
    running: RunningAnalysis | None = None
    strength: StrengthAnalysis | None = None
    training_status: TrainingStatusFeatures | None = None
    load_deviation: Deviation | None = None
    load_state: LoadState = LoadState.INSUFFICIENT_DATA
    load_state_label: str = ""
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)


class ProfileFeatures(BaseModel):
    sleep: SleepFeatures
    hrv: HrvFeatures
    overnight_vitals: OvernightVitalsFeatures
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
    load_kg: float | None = Field(default=None, ge=0)
    rest_seconds: tuple[int, int] | None = None
    intensity: str | None = None
    instructions: list[str] = Field(default_factory=list)


class TrainingPreferenceInput(BaseModel):
    weekly_running_target: int = Field(default=3, ge=1, le=7)
    weekly_strength_target: int = Field(default=3, ge=1, le=7)
    rotation_policy: Literal["BALANCE", "ALTERNATE"] = "BALANCE"
    treadmill_available: bool = False
    bad_weather_running_policy: Literal["DEFER", "STRENGTH", "RECOVERY"] = "DEFER"
    available_weekdays: list[int] = Field(default_factory=list, max_length=7)
    max_session_minutes: int | None = Field(default=None, ge=20, le=180)
    running_experience: Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"] | None = None
    strength_experience: Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"] | None = None
    equipment: list[str] = Field(default_factory=list, max_length=30)
    pain_or_injury_status: Literal["NONE", "PRESENT", "UNKNOWN"] = "UNKNOWN"
    pain_or_injury_notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_preferences(self):
        if len(set(self.available_weekdays)) != len(self.available_weekdays):
            raise ValueError("available_weekdays must not contain duplicates")
        if any(day < 1 or day > 7 for day in self.available_weekdays):
            raise ValueError("available_weekdays must use ISO weekday values 1 through 7")
        if self.pain_or_injury_status == "PRESENT" and not self.pain_or_injury_notes:
            raise ValueError("pain_or_injury_notes is required when pain or injury is present")
        return self


class TrainingPreferencePatch(BaseModel):
    """Partial preference update; cross-field rules are checked after merge."""

    weekly_running_target: int | None = Field(default=None, ge=1, le=7)
    weekly_strength_target: int | None = Field(default=None, ge=1, le=7)
    rotation_policy: Literal["BALANCE", "ALTERNATE"] | None = None
    treadmill_available: bool | None = None
    bad_weather_running_policy: Literal["DEFER", "STRENGTH", "RECOVERY"] | None = None
    available_weekdays: list[int] | None = Field(default=None, max_length=7)
    max_session_minutes: int | None = Field(default=None, ge=20, le=180)
    running_experience: Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"] | None = None
    strength_experience: Literal["BEGINNER", "INTERMEDIATE", "ADVANCED"] | None = None
    equipment: list[str] | None = Field(default=None, max_length=30)
    pain_or_injury_status: Literal["NONE", "PRESENT", "UNKNOWN"] | None = None
    pain_or_injury_notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_patch(self):
        if self.available_weekdays is not None:
            if len(set(self.available_weekdays)) != len(self.available_weekdays):
                raise ValueError("available_weekdays must not contain duplicates")
            if any(day < 1 or day > 7 for day in self.available_weekdays):
                raise ValueError("available_weekdays must use ISO weekday values 1 through 7")
        return self


class TrainingPreferences(TrainingPreferenceInput):
    user_id: str
    primary_goal: Literal["HEALTH"] = "HEALTH"
    primary_goal_label: str = "综合健康优先"
    running_required: Literal[True] = True
    strength_required: Literal[True] = True
    updated_at: datetime | None = None


class PlannedSession(BaseModel):
    role: Literal["PRIMARY", "OPTIONAL"]
    role_label: str
    session_type: Literal["RUNNING", "STRENGTH", "RECOVERY", "REST"]
    session_type_label: str
    code: str
    title: str
    focus: str
    goal: str
    intensity: Literal["high", "moderate", "low", "none"]
    intensity_label: str
    total_duration_minutes: tuple[int, int] | None = None
    steps: list[TrainingStep] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    evidence_ref_ids: list[str] = Field(default_factory=list)
    personalization_reasons: list[str] = Field(default_factory=list)
    progression: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class ConcurrentWeeklyBalance(BaseModel):
    running_completed_7d: int = Field(ge=0)
    running_target_7d: int = Field(ge=1)
    running_completed_28d: int = Field(ge=0)
    running_target_28d: int = Field(ge=4)
    strength_completed_7d: int = Field(ge=0)
    strength_target_7d: int = Field(ge=1)
    strength_completed_28d: int = Field(ge=0)
    strength_target_28d: int = Field(ge=4)
    running_due: bool
    strength_due: bool


class ActionPlan(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    goal: Literal["HEALTH_FIRST_CONCURRENT"] = "HEALTH_FIRST_CONCURRENT"
    goal_label: str = "综合健康优先，跑步与力量并行"
    valid_for_date: DateValue
    expires_at: datetime
    safety_status: Literal["CLEAR", "LIMITED", "UNKNOWN"]
    safety_status_label: str
    weekly_balance: ConcurrentWeeklyBalance
    primary_session: PlannedSession | None = None
    optional_session: PlannedSession | None = None
    session_relationship: Literal["ADDITION", "ALTERNATIVE", "NONE"] = "NONE"
    session_relationship_label: str
    conflict_checks: list[str] = Field(default_factory=list)
    missing_input_gates: list[str] = Field(default_factory=list)


class TrainingDecision(BaseModel):
    recommendation_id: str
    action: DecisionAction
    action_label: str = ""
    confidence: ConfidenceBand
    confidence_label: str = ""
    drivers: list[str] = Field(default_factory=list)
    driver_labels: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    limitation_labels: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    action_plan: ActionPlan


class EvidenceRef(BaseModel):
    id: str
    title: str
    url: str
    applies_to: list[str] = Field(default_factory=list)


class WeeklyDataQuality(BaseModel):
    status: QualityStatus
    status_label: str
    sleep_days: int = Field(ge=0, le=7)
    hrv_days: int = Field(ge=0, le=7)
    activity_days: int = Field(ge=0, le=7)
    training_days: int = Field(ge=0, le=7)
    confidence: ConfidenceBand
    confidence_label: str
    limitations: list[str] = Field(default_factory=list)


class WeeklySleepFacts(BaseModel):
    available_days: int = Field(ge=0, le=7)
    average_minutes: float | None = None
    median_minutes: float | None = None
    previous_average_minutes: float | None = None
    change_percent: float | None = None
    bedtime_regularity_minutes: float | None = None


class WeeklyRecoveryFacts(BaseModel):
    hrv_available_days: int = Field(ge=0, le=7)
    hrv_metric: str | None = None
    hrv_metric_label: str | None = None
    hrv_device_id: str | None = None
    hrv_median_ms: float | None = None
    hrv_previous_median_ms: float | None = None
    hrv_change_percent: float | None = None
    sleep_hrv_device_id: str | None = None
    sleep_hrv_daily: list[DailySleepHrvPoint] = Field(default_factory=list, max_length=7)
    rhr_available_days: int = Field(ge=0, le=7)
    rhr_metric: str | None = None
    rhr_median_bpm: float | None = None
    rhr_previous_median_bpm: float | None = None
    rhr_change_percent: float | None = None


class WeeklyTrainingFacts(BaseModel):
    workout_count: int = Field(ge=0)
    training_days: int = Field(ge=0, le=7)
    rest_days: int = Field(ge=0, le=7)
    duration_minutes: int = Field(ge=0)
    vendor_load: float = Field(ge=0)
    previous_vendor_load: float | None = Field(default=None, ge=0)
    load_change_percent: float | None = None
    aerobic_minutes: int = Field(ge=0)
    strength_sessions: int = Field(ge=0)
    sport_mode_counts: dict[str, int] = Field(default_factory=dict)


class WeeklyActivityFacts(BaseModel):
    available_days: int = Field(ge=0, le=7)
    total_steps: int | None = Field(default=None, ge=0)
    average_steps: float | None = Field(default=None, ge=0)
    previous_average_steps: float | None = Field(default=None, ge=0)
    steps_change_percent: float | None = None
    active_minutes: int | None = Field(default=None, ge=0)


class WeeklyFeedbackFacts(BaseModel):
    response_count: int = Field(ge=0)
    average_session_rpe: float | None = Field(default=None, ge=1, le=10)
    average_physical_fatigue: float | None = Field(default=None, ge=1, le=5)
    average_mental_state: float | None = Field(default=None, ge=1, le=5)
    average_muscle_soreness: float | None = Field(default=None, ge=1, le=5)


class WeeklyFacts(BaseModel):
    sleep: WeeklySleepFacts
    recovery: WeeklyRecoveryFacts
    training: WeeklyTrainingFacts
    activity: WeeklyActivityFacts
    feedback: WeeklyFeedbackFacts


class WeeklyInferences(BaseModel):
    trends: list[TrendFeature] = Field(default_factory=list)
    events: list[HealthEvent] = Field(default_factory=list)
    key_changes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class WeeklyRecommendation(BaseModel):
    priority: int = Field(ge=1)
    code: str
    title: str
    action: str
    reasons: list[str] = Field(default_factory=list)


class WeeklyActions(BaseModel):
    recommendations: list[WeeklyRecommendation] = Field(default_factory=list)


class WeeklyProfile(BaseModel):
    schema_version: str = WEEKLY_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    user_id: str
    period_start: date
    period_end: date
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: WeeklyDataQuality
    facts: WeeklyFacts
    inferences: WeeklyInferences
    actions: WeeklyActions
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    open_health_period_summary: "OpenHealthPeriodSummary | None" = None
    open_health_coverage: dict[str, "OpenHealthCoverage"] = Field(default_factory=dict)


class MonthlyDataQuality(BaseModel):
    status: QualityStatus
    status_label: str
    sleep_days: int = Field(ge=0, le=28)
    hrv_days: int = Field(ge=0, le=28)
    activity_days: int = Field(ge=0, le=28)
    training_record_days: int = Field(ge=0, le=28)
    training_days: int | None = Field(default=None, ge=0, le=28)
    confidence: ConfidenceBand
    confidence_label: str
    limitations: list[str] = Field(default_factory=list)


class MonthlySleepFacts(BaseModel):
    available_days: int = Field(ge=0, le=28)
    average_minutes: float | None = None
    median_minutes: float | None = None
    previous_average_minutes: float | None = None
    change_percent: float | None = None
    bedtime_regularity_minutes: float | None = None


class MonthlyRecoveryStreamFacts(BaseModel):
    metric: str
    metric_label: str
    source: str
    source_scope: str
    device_id: str | None = None
    unit: str
    available_days: int = Field(ge=0, le=28)
    previous_available_days: int = Field(ge=0, le=28)
    median: float | None = None
    previous_median: float | None = None
    change_percent: float | None = None


class MonthlyRecoveryFacts(BaseModel):
    streams: list[MonthlyRecoveryStreamFacts] = Field(default_factory=list)


class MonthlyTrainingFacts(BaseModel):
    record_days: int = Field(ge=0, le=28)
    workout_count: int | None = Field(default=None, ge=0)
    training_days: int | None = Field(default=None, ge=0, le=28)
    rest_days: int | None = Field(default=None, ge=0, le=28)
    duration_minutes: int | None = Field(default=None, ge=0)
    vendor_load: float | None = Field(default=None, ge=0)
    previous_vendor_load: float | None = Field(default=None, ge=0)
    load_change_percent: float | None = None
    aerobic_minutes: int | None = Field(default=None, ge=0)
    strength_sessions: int | None = Field(default=None, ge=0)
    sport_mode_counts: dict[str, int] = Field(default_factory=dict)


class MonthlyActivityFacts(BaseModel):
    available_days: int = Field(ge=0, le=28)
    total_steps: int | None = Field(default=None, ge=0)
    average_steps: float | None = Field(default=None, ge=0)
    previous_average_steps: float | None = Field(default=None, ge=0)
    steps_change_percent: float | None = None
    active_minutes: int | None = Field(default=None, ge=0)


class MonthlyFeedbackFacts(BaseModel):
    response_count: int = Field(ge=0)
    average_session_rpe: float | None = Field(default=None, ge=1, le=10)
    average_physical_fatigue: float | None = Field(default=None, ge=1, le=5)
    average_mental_state: float | None = Field(default=None, ge=1, le=5)
    average_muscle_soreness: float | None = Field(default=None, ge=1, le=5)


class MonthlyFacts(BaseModel):
    sleep: MonthlySleepFacts
    recovery: MonthlyRecoveryFacts
    training: MonthlyTrainingFacts
    activity: MonthlyActivityFacts
    feedback: MonthlyFeedbackFacts


class PersonalAssociation(BaseModel):
    id: str
    status: Availability
    status_label: str
    predictor_metric: str
    predictor_metric_label: str
    predictor_source: str
    predictor_source_scope: str
    predictor_device_id: str | None = None
    predictor_unit: str
    outcome_metric: str
    outcome_metric_label: str
    outcome_source: str
    outcome_source_scope: str
    outcome_device_id: str | None = None
    outcome_unit: str
    lag_days: Literal[0, 1]
    window_days: Literal[60, 90]
    paired_days: int = Field(ge=0)
    minimum_paired_days: int = Field(ge=1)
    coverage_ratio: float = Field(ge=0, le=1)
    method: Literal["SPEARMAN"] = "SPEARMAN"
    coefficient: float | None = Field(default=None, ge=-1, le=1)
    direction: Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "INSUFFICIENT_DATA"]
    direction_label: str
    strength: Literal["WEAK", "MODEST", "MODERATE", "STRONG", "INSUFFICIENT_DATA"]
    strength_label: str
    confidence: ConfidenceBand
    confidence_label: str
    predictor_median: float | None = None
    outcome_median: float | None = None
    confounded_pair_days: int = Field(default=0, ge=0)
    summary: str
    limitations: list[str] = Field(default_factory=list)
    association_only: Literal[True] = True


class PersonalAssociationProfile(BaseModel):
    schema_version: str = ASSOCIATION_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    user_id: str
    date: DateValue
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    associations: list[PersonalAssociation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class MonthlyInferences(BaseModel):
    trends: list[TrendFeature] = Field(default_factory=list)
    events: list[HealthEvent] = Field(default_factory=list)
    personal_associations: list[PersonalAssociation] = Field(default_factory=list)
    key_changes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class MonthlyActions(BaseModel):
    recommendations: list[WeeklyRecommendation] = Field(default_factory=list)


class MonthlyProfile(BaseModel):
    schema_version: str = MONTHLY_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    user_id: str
    period_start: date
    period_end: date
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: MonthlyDataQuality
    facts: MonthlyFacts
    inferences: MonthlyInferences
    actions: MonthlyActions
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    open_health_period_summary: "OpenHealthPeriodSummary | None" = None
    open_health_coverage: dict[str, "OpenHealthCoverage"] = Field(default_factory=dict)


class SubjectiveFeedbackInput(BaseModel):
    date: DateValue | None = None
    workout_source: str | None = Field(default=None, min_length=1, max_length=32)
    workout_id: str | None = Field(default=None, max_length=128)
    recommendation_id: str | None = Field(default=None, max_length=64)
    session_rpe: float | None = Field(default=None, ge=1, le=10)
    physical_fatigue: int | None = Field(default=None, ge=1, le=5)
    mental_state: int | None = Field(default=None, ge=1, le=5)
    muscle_soreness: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_observation(self):
        if not any((
            self.session_rpe is not None,
            self.physical_fatigue is not None,
            self.mental_state is not None,
            self.muscle_soreness is not None,
            bool(self.notes and self.notes.strip()),
        )):
            raise ValueError("至少填写一项主观反馈")
        if self.notes is not None:
            self.notes = self.notes.strip() or None
        if self.session_rpe is not None and not self.workout_id:
            raise ValueError("训练 RPE 必须关联一次已完成训练")
        if self.workout_id and not self.workout_source:
            raise ValueError("关联训练时必须提供 workout_source")
        if self.workout_source and not self.workout_id:
            raise ValueError("workout_source 必须与 workout_id 同时提供")
        if self.recommendation_id and not self.workout_id:
            raise ValueError("建议反馈必须同时关联已完成训练")
        return self


class SubjectiveFeedback(BaseModel):
    id: str
    user_id: str
    date: DateValue
    workout_source: str | None = None
    workout_id: str | None = None
    recommendation_id: str | None = None
    session_rpe: float | None = Field(default=None, ge=1, le=10)
    physical_fatigue: int | None = Field(default=None, ge=1, le=5)
    mental_state: int | None = Field(default=None, ge=1, le=5)
    muscle_soreness: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DailyProfile(BaseModel):
    schema_version: Literal["10.0"] = DAILY_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: Literal["10.0"] = INTELLIGENCE_VERSION
    decision_policy_version: Literal["7.0"] = DECISION_POLICY_VERSION
    evidence_version: Literal["2026-09a"] = EVIDENCE_VERSION
    user_id: str
    date: DateValue
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: DataQuality
    facts: dict[str, list[MeasurementFact]] = Field(default_factory=dict)
    baselines: dict[str, list[BaselineStats]] = Field(default_factory=dict)
    features: ProfileFeatures
    trends: list[TrendFeature] = Field(default_factory=list)
    events: list[HealthEvent] = Field(default_factory=list)
    states: ProfileStates
    decision: TrainingDecision
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    open_health_insights: "OpenHealthBundle | None" = None


class TrendResponse(BaseModel):
    user_id: str
    date: DateValue
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trends: list[TrendFeature] = Field(default_factory=list)


class HealthEventResponse(BaseModel):
    user_id: str
    period_start: DateValue
    period_end: DateValue
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[HealthEvent] = Field(default_factory=list)


class ExplanationFact(BaseModel):
    code: str
    label: str
    value: float | int | str | None = None
    unit: str | None = None


class DecisionExplanation(BaseModel):
    user_id: str
    date: DateValue
    facts: list[ExplanationFact] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    action: TrainingDecision
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class ContextCurrent(BaseModel):
    analysis_run_id: str
    date: DateValue
    data_quality_status: QualityStatus
    data_quality_label: str
    sleep_minutes: int | None = None
    hrv_metric: str | None = None
    hrv_value_ms: float | None = None
    hrv_device_id: str | None = None
    rhr_bpm: float | None = None
    recovery_state: RecoveryState
    recovery_state_label: str
    training_load_state: LoadState
    training_load_state_label: str
    action: DecisionAction
    action_label: str
    confidence: ConfidenceBand
    confidence_label: str
    recommendation_id: str
    primary_session_title: str | None = None
    optional_session_title: str | None = None
    driver_labels: list[str] = Field(default_factory=list, max_length=5)
    limitation_labels: list[str] = Field(default_factory=list, max_length=5)


class ContextEvent(BaseModel):
    id: str
    type: str
    type_label: str
    lifecycle: EventLifecycle
    lifecycle_label: str
    severity: EventSeverity
    summary: str
    acknowledged: bool


class ContextFeedback(BaseModel):
    id: str
    date: DateValue
    workout_source: str | None = None
    workout_id: str | None = None
    session_rpe: float | None = None
    physical_fatigue: int | None = None
    mental_state: int | None = None
    muscle_soreness: int | None = None


class ContextRecent(BaseModel):
    period_start: DateValue
    period_end: DateValue
    sleep_average_minutes: float | None = None
    hrv_change_percent: float | None = None
    rhr_change_percent: float | None = None
    workout_count: int = Field(ge=0)
    training_duration_minutes: int = Field(ge=0)
    sport_mode_counts: dict[str, int] = Field(default_factory=dict)
    active_events: list[ContextEvent] = Field(default_factory=list, max_length=5)
    feedback: list[ContextFeedback] = Field(default_factory=list, max_length=5)


class ContextTrend(BaseModel):
    metric: str
    metric_label: str
    window_days: Literal[7, 28, 90]
    device_id: str | None = None
    change_percent: float | None = None
    direction: TrendDirection
    direction_label: str
    confidence: ConfidenceBand
    confidence_label: str


class ContextPatternMetric(BaseModel):
    metric: str
    device_id: str | None = None
    median: float | None = None
    unit: str
    sample_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)


class ContextPattern(BaseModel):
    group_type: Literal["training_family", "sport_mode"]
    group_key: str
    group_label: str
    response_count: int = Field(ge=0)
    confidence: ConfidenceBand
    confidence_label: str
    metrics: list[ContextPatternMetric] = Field(default_factory=list, max_length=3)


class ContextAssociation(BaseModel):
    id: str
    predictor_metric_label: str
    outcome_metric_label: str
    predictor_source: str
    predictor_source_scope: str
    predictor_device_id: str | None = None
    outcome_source: str
    outcome_source_scope: str
    outcome_device_id: str | None = None
    lag_days: Literal[0, 1]
    window_days: Literal[60, 90]
    coefficient: float
    direction_label: str
    strength_label: str
    confidence: ConfidenceBand
    confidence_label: str
    summary: str


class ContextPersonal(BaseModel):
    patterns: list[ContextPattern] = Field(default_factory=list, max_length=6)
    associations: list[ContextAssociation] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=3)


class AgentContext(BaseModel):
    schema_version: Literal["5.0"] = AGENT_CONTEXT_SCHEMA_VERSION
    user_id: str
    date: DateValue
    current: ContextCurrent
    recent: ContextRecent
    trend: list[ContextTrend] = Field(default_factory=list, max_length=12)
    personal: ContextPersonal
    profile: ContextProfile
    missing_inputs: list[MissingUserInput] = Field(default_factory=list, max_length=3)
    open_health_summary: "OpenHealthSummary | None" = None
    insights_stale: bool = False


class TimelineItem(BaseModel):
    id: str
    type: Literal[
        "analysis",
        "recommendation",
        "workout",
        "feedback",
        "event_transition",
        "training_response",
        "monthly_summary",
        "personal_association",
    ]
    date: DateValue
    title: str
    summary: str
    references: dict[str, str] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class HealthTimeline(BaseModel):
    user_id: str
    period_start: DateValue
    period_end: DateValue
    items: list[TimelineItem] = Field(default_factory=list, max_length=100)


class EventAcknowledgement(BaseModel):
    status: Literal["acknowledged"] = "acknowledged"
    event: HealthEvent


class RecommendationInstance(BaseModel):
    id: str
    analysis_run_id: str
    user_id: str
    date: DateValue
    decision: TrainingDecision
    linked_workout_source: str | None = None
    linked_workout_id: str | None = None
    completion_status: RecommendationStatus = RecommendationStatus.PLANNED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class WorkoutExposure(BaseModel):
    workout_id: str
    source: str = "zepp"
    date: DateValue
    type: str
    sport_mode: str
    sport_mode_label: str
    training_family: str
    training_family_label: str
    duration_minutes: int = Field(ge=0)
    vendor_load: float = Field(ge=0)
    heart_rate_avg_bpm: int | None = Field(default=None, ge=1)
    heart_rate_max_bpm: int | None = Field(default=None, ge=1)


class ResponseMetricObservation(BaseModel):
    metric: str
    device_id: str | None = None
    unit: str
    status: Availability
    baseline_reference: float | None = None
    value: float | None = None
    deviation_percent: float | None = None
    direction: Literal["above", "near", "below", "unknown"] = "unknown"


class TrainingResponseDay(BaseModel):
    day_offset: Literal[1, 2, 3]
    date: DateValue
    observations: list[ResponseMetricObservation] = Field(default_factory=list)
    overlapping_workout_ids: list[str] = Field(default_factory=list)


class TrainingResponse(BaseModel):
    analysis_run_id: str
    user_id: str
    exposure: WorkoutExposure
    recommendation_id: str | None = None
    feedback: list[SubjectiveFeedback] = Field(default_factory=list)
    response_days: list[TrainingResponseDay] = Field(default_factory=list)
    missing_windows: list[str] = Field(default_factory=list)
    overlapping_workout_ids: list[str] = Field(default_factory=list)
    recovery_status: RecoveryOutcome
    recovery_status_label: str
    recovery_hours: int | None = Field(default=None, ge=0)
    confidence: ConfidenceBand
    confidence_label: str
    limitations: list[str] = Field(default_factory=list)


class TrainingResponseProfile(BaseModel):
    schema_version: str = TRAINING_RESPONSE_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    user_id: str
    date: DateValue
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    responses: list[TrainingResponse] = Field(default_factory=list)


class PersonalMetricStats(BaseModel):
    metric: str
    device_id: str | None = None
    unit: str
    median: float | None = None
    mad: float | None = None
    sample_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)


class PersonalResponsePattern(BaseModel):
    group_type: Literal["training_family", "sport_mode"]
    group_key: str
    group_label: str
    response_count: int = Field(ge=0)
    metrics: list[PersonalMetricStats] = Field(default_factory=list)
    confidence: ConfidenceBand
    confidence_label: str


class PersonalModel(BaseModel):
    schema_version: str = PERSONAL_MODEL_SCHEMA_VERSION
    analysis_run_id: str
    user_id: str
    date: DateValue
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    baselines: list[BaselineStats] = Field(default_factory=list)
    long_term_trends: list[TrendFeature] = Field(default_factory=list)
    training_response_patterns: list[PersonalResponsePattern] = Field(default_factory=list)
    personal_associations: list[PersonalAssociation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class LinkRecommendationInput(BaseModel):
    workout_source: str = Field(min_length=1, max_length=32)
    workout_id: str = Field(min_length=1, max_length=128)


class AnalysisRun(BaseModel):
    id: str
    user_id: str
    target_date: DateValue
    status: AnalysisRunStatus
    started_at: datetime
    completed_at: datetime | None = None
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
    error: str | None = None
    profile_revision_used: int | None = Field(default=0, ge=0)


class AnalysisResult(BaseModel):
    run: AnalysisRun
    daily: DailyProfile
    weekly: WeeklyProfile
    monthly: MonthlyProfile
    recommendation: RecommendationInstance
    training_responses: list[TrainingResponse] = Field(default_factory=list)
    personal_model: PersonalModel
    personal_associations: PersonalAssociationProfile
    open_health_insights: "OpenHealthBundle | None" = None


# Open Health Insights 1.0 is intentionally isolated from the Daily/Weekly/Monthly
# schemas. It is a shadow-only contract and must not drive DecisionEngine actions.
OPEN_HEALTH_SCHEMA_VERSION = "1.0"


class OpenHealthStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    REFUSED = "REFUSED"


class OpenHealthRefusalReason(BaseModel):
    code: str
    detail: str
    missing_inputs: list[str] = Field(default_factory=list)


class OpenHealthProvenance(BaseModel):
    source: str
    source_scope: str = "unknown"
    device_id: str | None = None
    module: str | None = None
    algorithm: str | None = None
    upstream_revision: str | None = None


class ReadinessInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: DateValue
    stream: Literal["ln_rmssd"] = "ln_rmssd"
    ln_rmssd: float | None = None
    baseline_ln_rmssd: float | None = None
    delta: float | None = None
    swc: float | None = None
    state: Literal["suppressed", "normal", "elevated", "unavailable"] = "unavailable"
    history_nights: int = Field(default=0, ge=0)
    prior_nights: int = Field(default=0, ge=0)
    rr_available: bool = True


class AnomalyInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: DateValue
    dimensions: list[Literal["ln_rmssd", "rhr", "resp"]] = Field(default_factory=list)
    score: float | None = None
    threshold: float | None = None
    flagged: bool = False
    streak_days: int = Field(default=0, ge=0)
    gap_reset: bool = False
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    method: Literal["robust_mahalanobis"] = "robust_mahalanobis"
    diagnostic: Literal[False] = False


class SleepRegularityInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_days: Literal[7, 28]
    minimum_nights: Literal[5, 14]
    available_nights: int = Field(default=0, ge=0)
    bedtime_circular_mad_minutes: float | None = None
    wake_circular_mad_minutes: float | None = None
    midpoint_circular_mad_minutes: float | None = None
    status: Literal["AVAILABLE", "REFUSED"] = "REFUSED"


class SleepInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_date: DateValue
    time_in_bed_minutes: float | None = None
    sleep_minutes: float | None = None
    efficiency: float | None = None
    bedtime: str | None = None
    wake_time: str | None = None
    midpoint: str | None = None
    regularity: list[SleepRegularityInsight] = Field(default_factory=list)
    target_minutes: int | None = None
    target_met: bool | None = None
    target_gap_minutes: float | None = None
    target_status: Literal["AVAILABLE", "REFUSED"] = "REFUSED"
    naps_known: bool = False
    nap_minutes: float | None = None


class WorkoutTrimpInsight(BaseModel):
    """One workout's Banister TRIMP result, independent of report wording."""

    model_config = ConfigDict(extra="forbid")

    workout_id: str
    source: str
    date: DateValue
    trimp: float | None = Field(default=None, ge=0)
    rhr_bpm: float | None = Field(default=None, ge=1)
    hrmax_bpm: float | None = Field(default=None, ge=1)
    selected_source_scope: str | None = None
    selected_device_id: str | None = None
    sample_count: int = Field(default=0, ge=0)
    credited_minutes: float = Field(default=0, ge=0)
    active_duration_minutes: float = Field(default=0, ge=0)
    coverage_ratio: float = Field(default=0, ge=0, le=1)
    clamped_high_hr_points: int = Field(default=0, ge=0)
    lower_bound: bool = False


class TrainingLoadDailyPoint(BaseModel):
    """A natural-calendar-day load value; unknown is distinct from zero rest."""

    model_config = ConfigDict(extra="forbid")

    date: DateValue
    status: Literal["REST", "SCORED", "UNKNOWN"]
    trimp: float | None = Field(default=None, ge=0)
    workout_count: int = Field(default=0, ge=0)
    scored_workout_count: int = Field(default=0, ge=0)
    unknown_workout_count: int = Field(default=0, ge=0)
    atl: float | None = Field(default=None, ge=0)
    ctl: float | None = Field(default=None, ge=0)
    tsb: float | None = None
    lower_bound: bool = False


class TrainingLoadInsight(BaseModel):
    """Forty-two natural days of TRIMP and Banister impulse-response load."""

    model_config = ConfigDict(extra="forbid")

    target_date: DateValue
    period_start: DateValue
    period_end: DateValue
    window_days: Literal[42] = 42
    daily_points: list[TrainingLoadDailyPoint] = Field(default_factory=list, max_length=42)
    atl: float | None = Field(default=None, ge=0)
    ctl: float | None = Field(default=None, ge=0)
    tsb: float | None = None
    atl_tau_days: Literal[7] = 7
    ctl_tau_days: Literal[42] = 42
    coverage_ratio: float = Field(default=0, ge=0, le=1)
    recent_7d_coverage_ratio: float = Field(default=0, ge=0, le=1)
    detail_coverage_ratio: float = Field(default=0, ge=0, le=1)
    unknown_dates: list[DateValue] = Field(default_factory=list, max_length=42)
    workout_trimp: list[WorkoutTrimpInsight] = Field(default_factory=list)
    lower_bound: bool = False


# Stable aliases for callers that use the shorter daily-point terminology.
DailyTrimpPoint = TrainingLoadDailyPoint
DailyLoadPoint = TrainingLoadDailyPoint
DailyTrainingLoadPoint = TrainingLoadDailyPoint


OpenHealthPayload = (
    ReadinessInsight
    | AnomalyInsight
    | SleepInsight
    | WorkoutTrimpInsight
    | TrainingLoadInsight
)


class OpenHealthInsights(BaseModel):
    schema_version: Literal["1.0"] = OPEN_HEALTH_SCHEMA_VERSION
    algorithm_id: str
    version: Literal["1.0"] = OPEN_HEALTH_SCHEMA_VERSION
    upstream_revision: str
    shadow_only: Literal[True] = True
    status: OpenHealthStatus
    tier: str
    inputs_used: list[str] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    confidence: float | ConfidenceBand = ConfidenceBand.NONE
    drivers: list[str] = Field(default_factory=list)
    note: str | None = None
    refusal_reason: OpenHealthRefusalReason | None = None
    provenance: list[OpenHealthProvenance] = Field(default_factory=list)
    profile_revision_used: int = Field(default=0, ge=0)
    payload: OpenHealthPayload | None = None


class OpenHealthBundle(BaseModel):
    schema_version: Literal["1.0"] = OPEN_HEALTH_SCHEMA_VERSION
    target_date: DateValue
    profile_revision_used: int = Field(default=0, ge=0)
    readiness: OpenHealthInsights | None = None
    anomaly: OpenHealthInsights | None = None
    sleep: OpenHealthInsights | None = None
    training_load: OpenHealthInsights | None = None


class OpenHealthCoverage(BaseModel):
    """Typed coverage summary used by period reports, not a recomputation."""

    window_days: int = Field(ge=0)
    observed_days: int = Field(ge=0)
    required_days: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)
    status: OpenHealthStatus


class OpenHealthPeriodSummary(BaseModel):
    period_start: DateValue
    period_end: DateValue
    period_days: int = Field(ge=0)
    scope: Literal["TARGET_DAY_WITH_PERIOD_COVERAGE"] = "TARGET_DAY_WITH_PERIOD_COVERAGE"
    target_date: DateValue
    status: OpenHealthStatus
    drivers: list[str] = Field(default_factory=list, max_length=4)
    refusal_reasons: list[OpenHealthRefusalReason] = Field(default_factory=list, max_length=4)


class OpenHealthSummary(BaseModel):
    """Compact Context projection; never contains the full OpenHealthBundle."""

    profile_revision_used: int = Field(default=0, ge=0)
    status: OpenHealthStatus
    readiness_state: str | None = None
    anomaly_flagged: bool | None = None
    load_status: OpenHealthStatus | None = None
    drivers: list[str] = Field(default_factory=list, max_length=2)
    refusal_reasons: list[OpenHealthRefusalReason] = Field(default_factory=list, max_length=4)
    missing_inputs: list[str] = Field(default_factory=list, max_length=8)


# Stable aliases for callers that use the generic envelope terminology.
OpenHealthInsight = OpenHealthInsights
OpenHealthEnvelope = OpenHealthInsights
InsightStatus = OpenHealthStatus
OpenHealthInsightStatus = OpenHealthStatus
OpenHealthReadiness = ReadinessInsight
OpenHealthAnomaly = AnomalyInsight
OpenHealthSleep = SleepInsight


# Resolve forward references used by the versioned report/context contracts.
DailyProfile.model_rebuild()
WeeklyProfile.model_rebuild()
MonthlyProfile.model_rebuild()
AgentContext.model_rebuild()
AnalysisResult.model_rebuild()
