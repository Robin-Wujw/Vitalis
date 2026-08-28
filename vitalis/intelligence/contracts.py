"""Versioned contracts shared by the intelligence engine and its consumers."""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


DateValue = date


DAILY_SCHEMA_VERSION = "3.0"
WEEKLY_SCHEMA_VERSION = "2.0"
INTELLIGENCE_VERSION = "3.0"
DECISION_POLICY_VERSION = "3.0"
EVIDENCE_VERSION = "2026-08"
TRAINING_RESPONSE_SCHEMA_VERSION = "1.0"
PERSONAL_MODEL_SCHEMA_VERSION = "1.0"


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


class SubjectiveFeedbackInput(BaseModel):
    date: DateValue | None = None
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
        if self.recommendation_id and not self.workout_id:
            raise ValueError("建议反馈必须同时关联已完成训练")
        return self


class SubjectiveFeedback(BaseModel):
    id: str
    user_id: str
    date: DateValue
    workout_id: str | None = None
    recommendation_id: str | None = None
    session_rpe: float | None = Field(default=None, ge=1, le=10)
    physical_fatigue: int | None = Field(default=None, ge=1, le=5)
    mental_state: int | None = Field(default=None, ge=1, le=5)
    muscle_soreness: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DailyProfile(BaseModel):
    schema_version: str = DAILY_SCHEMA_VERSION
    analysis_run_id: str
    intelligence_version: str = INTELLIGENCE_VERSION
    decision_policy_version: str = DECISION_POLICY_VERSION
    evidence_version: str = EVIDENCE_VERSION
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
    suggested_type_labels: list[str] = Field(default_factory=list, max_length=3)
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


class ContextPersonal(BaseModel):
    patterns: list[ContextPattern] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=3)


class AgentContext(BaseModel):
    schema_version: Literal["3.0"] = "3.0"
    user_id: str
    date: DateValue
    current: ContextCurrent
    recent: ContextRecent
    trend: list[ContextTrend] = Field(default_factory=list, max_length=12)
    personal: ContextPersonal


class TimelineItem(BaseModel):
    id: str
    type: Literal[
        "analysis",
        "recommendation",
        "workout",
        "feedback",
        "event_transition",
        "training_response",
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
    linked_workout_id: str | None = None
    completion_status: RecommendationStatus = RecommendationStatus.PLANNED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class WorkoutExposure(BaseModel):
    workout_id: str
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
    limitations: list[str] = Field(default_factory=list)


class LinkRecommendationInput(BaseModel):
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


class AnalysisResult(BaseModel):
    run: AnalysisRun
    daily: DailyProfile
    weekly: WeeklyProfile
    recommendation: RecommendationInstance
    training_responses: list[TrainingResponse] = Field(default_factory=list)
    personal_model: PersonalModel
