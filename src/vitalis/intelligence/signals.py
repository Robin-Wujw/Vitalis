"""Pure signal freshness, provenance, coverage, and calendar aggregation.

Date summaries and timestamped samples are independent measurement streams.
Missing observations are never filled, and fetched_at remains null until the
profile carries authoritative source-journal metadata.  Baseline spread is MAD
in the baseline's transform domain (ln(ms) for RMSSD), rather than a new score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from math import isfinite
from statistics import fmean, median
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from vitalis.time import local_day, local_day_utc_bounds, local_sleep_window

from .baseline import BaselineEngine
from .contracts import Availability, BaselineStats, SampleWindowSummary
from .period_activity import comparison_gate
from .profile import RawDailyProfile, SeriesPoint


SIGNAL_REGISTRY_VERSION = "1.0"
CalendarSemantics = Literal["sleep_day", "activity_day", "calendar_day"]
DecisionRole = Literal["yes", "no", "shadow"]
MeasurementKind = Literal["daily_summary", "samples", "sample_window", "unknown"]
Direction = Literal["above", "near", "below", "unknown"]
Aggregation = Literal["median", "mean_per_effective_day"]
StreamKey = tuple[str, str, str | None, str, MeasurementKind]


class SignalStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    INSUFFICIENT = "INSUFFICIENT"


class _SignalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def preserve_observation_precision(cls, value: object, info: ValidationInfo) -> object:
        if (
            info.field_name in {"observed_at", "first_observed_at", "last_observed_at"}
            and isinstance(value, str) and len(value) == 10
        ):
            return date.fromisoformat(value)
        return value


class SignalQualification(_SignalModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    minimum: float | None = None
    maximum: float | None = None
    minimum_exclusive: bool = False
    requires_source: bool = True
    requires_device_for_device_scope: bool = True
    zero_requires_verified_training_day: bool = False
    excluded_values: tuple[float, ...] = ()


class SignalCoverageRule(_SignalModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_days: int = Field(default=28, ge=1)
    baseline_window_days: int = 28
    baseline_minimum_days: int = 14
    minimum_samples_per_day: int = Field(default=1, ge=1)
    daily_reduction: Literal["median", "latest"] = "median"
    aggregation: Aggregation = "median"
    requires_closed_day: bool = False
    sample_window: bool = False
    period_comparison_rule: str = "5/7 days for a week; ceil(70% of days) otherwise"
    missing_day_rule: str = "Only observed days count; unobserved days stay unknown."


class SignalDefinition(_SignalModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    normalized_metric: str
    source_fields: tuple[str, ...] = Field(min_length=1)
    unit: str
    calendar_semantics: CalendarSemantics
    qualification: SignalQualification
    coverage_rule: SignalCoverageRule
    visible_channels: tuple[Literal["morning", "evening", "hermes", "api"], ...]
    decision_role: DecisionRole
    missing_late_policy: str = (
        "Keep missing values null; expose prior observations as stale without "
        "forward filling; late observations revise affected dates and windows."
    )


class SignalCoverage(_SignalModel):
    day: date
    sample_count: int = Field(default=0, ge=0)
    distinct_days: int = Field(default=0, ge=0, le=1)
    expected_days: int = 1
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    status: SignalStatus = SignalStatus.UNKNOWN
    complete: bool = False
    first_observed_at: datetime | date | None = None
    last_observed_at: datetime | date | None = None
    observed_minutes: int | None = Field(default=None, ge=0)
    expected_minutes: int = Field(ge=1)
    minute_coverage_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)


class SignalState(_SignalModel):
    metric: str
    normalized_metric: str
    unit: str
    calendar_semantics: CalendarSemantics
    measurement_kind: MeasurementKind = "unknown"
    source: str | None = None
    source_scope: str | None = None
    device_id: str | None = None
    source_fields: list[str] = Field(default_factory=list)
    visible_channels: tuple[str, ...] = ()
    decision_role: DecisionRole
    missing_late_policy: str
    status: SignalStatus = SignalStatus.UNKNOWN

    value: float | None = None
    baseline: float | None = None
    baseline_stats: BaselineStats | None = None
    deviation: float | None = None
    deviation_percent: float | None = None
    robust_spread: float | None = None
    robust_spread_unit: str | None = None
    robust_z: float | None = None
    direction: Direction = "unknown"
    comparison_status: SignalStatus = SignalStatus.INSUFFICIENT

    window_start: date
    window_end: date
    sample_count: int = Field(default=0, ge=0)
    distinct_days: int = Field(default=0, ge=0)
    expected_days: int = Field(default=28, ge=1)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    target_day_coverage: SignalCoverage
    observed_at: datetime | date | None = None
    fetched_at: datetime | None = None
    as_of: datetime
    last_observed_value: float | None = None
    last_observed_day: date | None = None
    limitations: list[str] = Field(default_factory=list)


class DailySignalValue(_SignalModel):
    day: date
    value: float
    sample_count: int = Field(ge=1)
    observed_at: datetime | date
    complete: bool


class PeriodReference(_SignalModel):
    start: date
    end: date
    expected_days: int = Field(ge=1)
    effective_days: int = Field(default=0, ge=0)
    distinct_days: int = Field(default=0, ge=0)
    sample_count: int = Field(default=0, ge=0)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    value: float | None = None
    median: float | None = None
    average: float | None = None
    status: SignalStatus = SignalStatus.UNKNOWN
    source: str | None = None
    source_scope: str | None = None
    device_id: str | None = None
    unit: str
    measurement_kind: MeasurementKind = "unknown"
    source_fields: list[str] = Field(default_factory=list)
    observed_at: datetime | date | None = None
    fetched_at: datetime | None = None


class PeriodSignalState(_SignalModel):
    metric: str
    normalized_metric: str
    unit: str
    calendar_semantics: CalendarSemantics
    measurement_kind: MeasurementKind = "unknown"
    source: str | None = None
    source_scope: str | None = None
    device_id: str | None = None
    source_fields: list[str] = Field(default_factory=list)
    visible_channels: tuple[str, ...] = ()
    decision_role: DecisionRole
    missing_late_policy: str
    start: date
    end: date
    status: SignalStatus = SignalStatus.UNKNOWN

    value: float | None = None
    median: float | None = None
    average: float | None = None
    aggregation: Aggregation
    baseline: float | None = None
    deviation: float | None = None
    deviation_percent: float | None = None
    robust_spread: float | None = None
    robust_spread_unit: str | None = None
    robust_z: float | None = None
    direction: Direction = "unknown"
    comparison_status: SignalStatus = SignalStatus.INSUFFICIENT
    minimum_comparison_days: int = Field(ge=1)

    effective_days: int = Field(default=0, ge=0)
    distinct_days: int = Field(default=0, ge=0)
    expected_days: int = Field(ge=1)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    sample_count: int = Field(default=0, ge=0)
    daily_values: list[DailySignalValue] = Field(default_factory=list)
    reference: PeriodReference
    target_day_coverage: SignalCoverage
    observed_at: datetime | date | None = None
    fetched_at: datetime | None = None
    as_of: datetime
    limitations: list[str] = Field(default_factory=list)


_CLOSED_DAY_METRICS = {
    "steps", "distance_km", "active_minutes", "calories", "running_distance",
    "cycling_distance", "training_load", "training_duration", "pai_daily",
    "pai_low_zone", "pai_medium_zone", "pai_high_zone", "hrv_rmssd", "hrv_sdnn",
    "stress", "stress_min", "stress_max", "stress_relaxed_pct", "stress_normal_pct",
    "stress_medium_pct", "stress_high_pct", "heart_rate",
}
_SCORE_METRICS = {
    "sleep_score", "spo2_night_score", "stress", "stress_min", "stress_max",
    "stress_relaxed_pct", "stress_normal_pct", "stress_medium_pct", "stress_high_pct",
    "readiness", "physical_readiness", "mental_readiness", "hrv_readiness",
    "rhr_readiness", "skin_temp_readiness", "ahi_readiness", "afib_readiness",
    "bio_charge", "hybrid_charge", "physical_charge", "mental_charge",
}
_CLOCK_METRICS = {"sleep_bedtime_offset_minutes", "sleep_wake_time_minutes"}
_LATEST_METRICS = {
    "readiness", "physical_readiness", "mental_readiness", "hrv_readiness",
    "rhr_readiness", "skin_temp_readiness", "ahi_readiness", "afib_readiness",
    "bio_charge", "hybrid_charge", "physical_charge", "mental_charge",
    "hrv_baseline", "rhr_baseline", "skin_temp_delta", "skin_temp_baseline_delta",
    "sleep_hrv", "sleep_rhr",
}


def _qualification(metric: str) -> SignalQualification:
    kwargs: dict = {"minimum": 0.0}
    if metric in _SCORE_METRICS:
        kwargs["maximum"] = 100.0
    elif metric in {"skin_temp_delta", "skin_temp_baseline_delta"}:
        kwargs.update(minimum=-2.0, maximum=2.0)
    elif metric == "sleep_bedtime_offset_minutes":
        kwargs.update(minimum=-720.0, maximum=720.0)
    elif metric == "sleep_wake_time_minutes":
        kwargs.update(maximum=1440.0)
    elif metric in {"hrv_rmssd", "hrv_sdnn", "sleep_hrv", "hrv_baseline"}:
        kwargs.update(minimum=1.0, maximum=400.0)
        if metric == "hrv_baseline":
            kwargs["excluded_values"] = (255.0,)
    elif metric in {"spo2", "spo2_apnea_low"}:
        kwargs.update(minimum=50.0, maximum=100.0)
    elif metric.startswith("respiratory_rate"):
        kwargs.update(minimum=4.0, maximum=60.0)
    elif metric == "heart_rate":
        kwargs.update(minimum=25.0, maximum=240.0)
    elif metric == "lactate_threshold_hr":
        kwargs.update(minimum=60.0, maximum=230.0)
    elif metric == "lactate_threshold_pace":
        kwargs.update(minimum=100.0, maximum=1800.0)
    elif metric == "speed":
        kwargs.update(minimum=0.0, maximum=20.0, minimum_exclusive=True)
    elif metric == "equivalent_pace":
        kwargs.update(minimum=60.0, maximum=3600.0)
    elif metric == "cadence":
        kwargs.update(minimum=30.0, maximum=300.0)
    elif metric == "stride_length":
        kwargs.update(minimum=10.0, maximum=300.0)
    elif metric == "distance":
        kwargs.update(minimum=0.0)
    elif metric == "altitude":
        kwargs.update(minimum=-1000.0, maximum=10_000.0)
    elif metric == "running_power":
        kwargs.update(minimum=0.0)
    elif metric == "ground_contact_time":
        kwargs.update(minimum=50.0, maximum=1000.0)
    elif metric == "vertical_oscillation":
        kwargs.update(minimum=1.0, maximum=500.0)
    elif metric == "vertical_stride_ratio":
        kwargs.update(minimum=0.1, maximum=100.0)
    elif metric in {"sleep_rhr", "resting_hr", "rhr_baseline", "device_resting_hr", "device_max_hr", "vo2max"}:
        kwargs["minimum_exclusive"] = True
    if metric in {"training_load", "training_duration"}:
        kwargs["zero_requires_verified_training_day"] = True
    return SignalQualification(
        description=(
            "Finite observation with the declared unit, source, device attribution "
            "when device-scoped, local-date semantics, and observation cutoff."
        ),
        **kwargs,
    )


def _definition(
    metric: str, unit: str, semantics: CalendarSemantics, fields: tuple[str, ...],
    role: DecisionRole = "no", *, aggregation: Aggregation = "median",
) -> SignalDefinition:
    recovery = semantics == "sleep_day" or metric in {"hrv_rmssd", "hrv_sdnn", "resting_hr"}
    return SignalDefinition(
        normalized_metric=metric,
        source_fields=fields,
        unit=unit,
        calendar_semantics=semantics,
        qualification=_qualification(metric),
        coverage_rule=SignalCoverageRule(
            requires_closed_day=metric in _CLOSED_DAY_METRICS,
            sample_window=metric in {"heart_rate", "stress"},
            daily_reduction="latest" if metric in _LATEST_METRICS else "median",
            aggregation=aggregation,
        ),
        visible_channels=("morning", "evening", "hermes", "api") if recovery else ("evening", "hermes", "api"),
        decision_role=role,
    )


def _registry() -> dict[str, SignalDefinition]:
    entries = [
        _definition("sleep_duration", "min", "sleep_day", ("sleep.sleep_duration", "slp.st", "slp.ed", "sleep.duration"), "yes", aggregation="mean_per_effective_day"),
        _definition("deep_sleep", "min", "sleep_day", ("sleep.deep_sleep", "slp.dp", "sleep.stages.deep")),
        _definition("rem_sleep", "min", "sleep_day", ("sleep.rem_sleep", "slp.rm", "slp.remMinutes", "sleep.stages.rem")),
        _definition("light_sleep", "min", "sleep_day", ("sleep.light_sleep", "slp.lt", "sleep.stages.light")),
        _definition("awake", "min", "sleep_day", ("sleep.awake", "slp.wk", "sleep.awake")),
        _definition("sleep_score", "score", "sleep_day", ("sleep.sleep_score", "slp.ss", "sleep.sleepScore")),
        _definition("sleep_wake_count", "count", "sleep_day", ("sleep.wake_count", "slp.wc")),
        _definition("sleep_bedtime_offset_minutes", "min_from_midnight", "sleep_day", ("sleep.bedtime", "slp.st", "sleep.bedTime")),
        _definition("sleep_wake_time_minutes", "min_from_midnight", "sleep_day", ("sleep.wake_time", "slp.ed", "sleep.wakeTime")),
        _definition("hrv_rmssd", "ms", "calendar_day", ("sample.hrv_rmssd", "HRV.samples.hrv", "HRV.samples.rmssd"), "yes"),
        _definition("hrv_sdnn", "ms", "calendar_day", ("sample.hrv_sdnn", "HRV.samples.sdnn"), "yes"),
        _definition("sleep_hrv", "ms", "sleep_day", ("daily.sleep_hrv", "sample.sleep_hrv", "readiness.sleepHRV"), "yes"),
        _definition("sleep_rhr", "bpm", "sleep_day", ("daily.sleep_rhr", "sample.sleep_rhr", "readiness.sleepRHR"), "yes"),
        _definition("resting_hr", "bpm", "calendar_day", ("activity.resting_hr", "daily.resting_hr", "slp.rhr", "restingHr", "restingHeartRate", "rhr"), "yes"),
        _definition("skin_temp_delta", "C", "sleep_day", ("daily.skin_temp_delta", "sample.skin_temp_delta", "readiness.skinTempCalibrated"), "shadow"),
        _definition("skin_temp_baseline_delta", "C", "sleep_day", ("daily.skin_temp_baseline_delta", "sample.skin_temp_baseline_delta", "readiness.skinTempBaseLine"), "no"),
        _definition("spo2", "%", "calendar_day", ("sample.spo2", "daily.spo2", "spo2.extra.spo2", "bloodOxygen", "blood_oxygen"), "shadow"),
        _definition("spo2_apnea_low", "%", "calendar_day", ("sample.spo2_apnea_low", "osa_event.extra.spo2_decrease", "osa_event.extra.spo2Decrease"), "shadow"),
        _definition("spo2_odi", "events/h", "sleep_day", ("daily.spo2_odi", "odi.odi"), "shadow"),
        _definition("spo2_odi_events", "count", "sleep_day", ("daily.spo2_odi_events", "odi.odiNum"), "shadow"),
        _definition("spo2_night_score", "score", "sleep_day", ("daily.spo2_night_score", "odi.score"), "no"),
        _definition("spo2_measured_minutes", "min", "sleep_day", ("daily.spo2_measured_minutes", "odi.cost"), "no"),
        _definition("steps", "steps", "activity_day", ("activity.steps", "daily.steps", "stp.ttl", "steps", "step", "stepCount", "totalSteps"), aggregation="mean_per_effective_day"),
        _definition("distance_km", "km", "activity_day", ("activity.distance_km", "daily.distance_km", "stp.dis", "distance", "totalDistance"), aggregation="mean_per_effective_day"),
        _definition("active_minutes", "min", "activity_day", ("activity.active_minutes", "daily.active_minutes", "activeMinutes", "totalBurningDuration"), aggregation="mean_per_effective_day"),
        _definition("calories", "kcal", "activity_day", ("activity.calories", "daily.calories", "stp.cal", "calories", "calorie", "totalCalories"), aggregation="mean_per_effective_day"),
        _definition("running_distance", "m", "activity_day", ("daily.running_distance", "totalRunningDistance"), aggregation="mean_per_effective_day"),
        _definition("cycling_distance", "m", "activity_day", ("daily.cycling_distance", "totalCyclingDistance"), aggregation="mean_per_effective_day"),
        _definition("training_load", "load", "activity_day", ("training.total_load", "daily.training_load", "trainingLoad", "wtlSum", "currnetDayTrainLoad", "load"), "yes", aggregation="mean_per_effective_day"),
        _definition("training_duration", "min", "activity_day", ("training.total_duration",), aggregation="mean_per_effective_day"),
        _definition("vo2max", "ml/kg/min", "calendar_day", ("daily.vo2max", "vo2max", "vo2Max", "VO2_MAX", "VO2_max", "vo2_max_run", "vo2_max_walking"), "shadow"),
        _definition("lactate_threshold_hr", "bpm", "calendar_day", ("daily.lactate_threshold_hr", "lactateThresholdHr"), "shadow"),
        _definition("lactate_threshold_pace", "s/km", "calendar_day", ("daily.lactate_threshold_pace", "lactateThresholdPace"), "shadow"),
        _definition("heart_rate", "bpm", "calendar_day", ("sample.heart_rate", "sample_window.heart_rate", "dense.heart_rate"), "shadow"),
        # Workout-detail samples are sparse, but each parser metric still needs
        # a declared unit/range and public visibility policy.
        _definition("speed", "m/s", "activity_day", ("workout.speed", "workout.samples.speed"), "shadow"),
        _definition("equivalent_pace", "s/km", "activity_day", ("workout.equivalent_pace", "workout.samples.equivalent_pace"), "shadow"),
        _definition("cadence", "spm", "activity_day", ("workout.cadence", "workout.samples.cadence"), "shadow"),
        _definition("stride_length", "cm", "activity_day", ("workout.stride_length", "workout.samples.stride_length"), "shadow"),
        _definition("distance", "m", "activity_day", ("workout.distance", "workout.samples.distance"), "shadow", aggregation="mean_per_effective_day"),
        _definition("altitude", "m", "activity_day", ("workout.altitude", "workout.samples.altitude"), "shadow"),
        _definition("running_power", "W", "activity_day", ("workout.running_power", "workout.samples.running_power"), "shadow"),
        _definition("ground_contact_time", "ms", "activity_day", ("workout.ground_contact_time", "workout.samples.ground_contact_time"), "shadow"),
        _definition("vertical_oscillation", "mm", "activity_day", ("workout.vertical_oscillation", "workout.samples.vertical_oscillation"), "shadow"),
        _definition("vertical_stride_ratio", "%", "activity_day", ("workout.vertical_stride_ratio", "workout.samples.vertical_stride_ratio"), "shadow"),
        _definition("hrv_baseline", "ms", "sleep_day", ("daily.hrv_baseline", "sample.hrv_baseline", "readiness.hrvBaseline")),
        _definition("rhr_baseline", "bpm", "sleep_day", ("daily.rhr_baseline", "sample.rhr_baseline", "readiness.rhrBaseline")),
        _definition("device_max_hr", "bpm", "activity_day", ("daily.device_max_hr", "pai.maxHr")),
        _definition("device_resting_hr", "bpm", "activity_day", ("daily.device_resting_hr", "pai.restHr")),
    ]
    for metric, field in (
        ("respiratory_rate", "measurements"),
        ("respiratory_rate_min", "measurements"),
        ("respiratory_rate_max", "measurements"),
    ):
        entries.append(_definition(metric, "brpm", "sleep_day", (f"daily.{metric}", f"respiratory_rate.{field}"), "shadow"))
    for metric, aliases in (
        ("stress", ("avgStress", "averageStress", "stress")),
        ("stress_min", ("minStress",)), ("stress_max", ("maxStress",)),
        ("stress_relaxed_pct", ("relaxPct", "relaxProportion")),
        ("stress_normal_pct", ("normalPct", "normalProportion")),
        ("stress_medium_pct", ("mediumPct", "mediumProportion")),
        ("stress_high_pct", ("highPct", "highProportion")),
    ):
        fields = (f"daily.{metric}",) + aliases
        if metric == "stress":
            fields += ("sample.stress", "sample_window.stress", "all_day_stress.data.value")
        entries.append(_definition(metric, "%" if metric.endswith("_pct") else "score", "calendar_day", fields, "shadow"))
    for metric, alias in (
        ("pai_daily", "dailyPai"), ("pai_low_zone", "lowZonePai"),
        ("pai_medium_zone", "mediumZonePai"), ("pai_high_zone", "highZonePai"),
    ):
        entries.append(_definition(metric, "pai", "activity_day", (f"daily.{metric}", f"pai.{alias}"), "shadow", aggregation="mean_per_effective_day"))
    for metric, aliases in (
        ("readiness", ("readiness", "readinessScore", "watchScore", "rdnsScore")),
        ("physical_readiness", ("phyScore",)), ("mental_readiness", ("mentScore",)),
        ("hrv_readiness", ("hrvScore",)), ("rhr_readiness", ("rhrScore",)),
        ("skin_temp_readiness", ("skinTempScore",)), ("ahi_readiness", ("ahiScore",)),
        ("afib_readiness", ("afibScore",)),
        ("bio_charge", ("bio_charge", "bioCharge", "bodyBattery", "chargeScore")),
        ("hybrid_charge", ("hybrid_charge", "hybridCharge", "hybridChargeScore", "Charge.samples.total")),
        ("physical_charge", ("Charge.samples.physical",)),
        ("mental_charge", ("Charge.samples.mental",)),
    ):
        entries.append(_definition(metric, "score", "calendar_day", (f"daily.{metric}", f"sample.{metric}") + aliases))
    return {entry.normalized_metric: entry for entry in entries}


SIGNAL_REGISTRY: Mapping[str, SignalDefinition] = MappingProxyType(_registry())


def build_signal_states(
    raw: RawDailyProfile,
    baselines: dict[str, list[BaselineStats]],
) -> dict[str, list[SignalState]]:
    """Return every registered metric, with independent provenance streams.

    Baselines supplied by the caller are reused only when identical to a
    qualified 28-day recomputation. BaselineStats has no as_of or timestamps,
    so its counts alone cannot prove cutoff or unit qualification.
    """
    output: dict[str, list[SignalState]] = {}
    for metric, definition in SIGNAL_REGISTRY.items():
        considered = _considered_points(raw, metric)
        streams = _group_streams(considered)
        states = [
            _daily_state(raw, definition, key, points, baselines.get(metric, []))
            for key, points in sorted(streams.items(), key=lambda item: _stream_sort_key(item[0]))
        ]
        summary = raw.sample_window_summaries.get(metric)
        if summary is not None:
            states.append(_summary_state(raw, definition, summary))
        if not states:
            states.append(_daily_state(raw, definition, None, [], []))
        output[metric] = states
    return output


def build_period_signals(
    raw: RawDailyProfile, start: date, end: date,
) -> dict[str, list[PeriodSignalState]]:
    """Aggregate actual daily series and the immediately preceding equal window.

    Any positive calendar length is supported, including 7/28/60/90 days.
    Current facts stay visible when comparison gates fail.  The reference has
    its own denominator and provenance, and no target-day shadow result is used.
    """
    if type(start) is not date or type(end) is not date:
        raise TypeError("period boundaries must be dates")
    if end < start:
        raise ValueError("period end must be on or after period start")
    expected = (end - start).days + 1
    reference_start = start - timedelta(days=expected)
    reference_end = start - timedelta(days=1)
    configured_reference = (raw.report_context or {}).get("reference_period")
    if isinstance(configured_reference, dict):
        try:
            candidate_start = configured_reference.get("start")
            candidate_end = configured_reference.get("end")
            if isinstance(candidate_start, str):
                candidate_start = date.fromisoformat(candidate_start[:10])
            if isinstance(candidate_end, str):
                candidate_end = date.fromisoformat(candidate_end[:10])
            if type(candidate_start) is date and type(candidate_end) is date and candidate_end >= candidate_start and candidate_end < start:
                reference_start, reference_end = candidate_start, candidate_end
        except (TypeError, ValueError):
            pass
    output: dict[str, list[PeriodSignalState]] = {}
    for metric, definition in SIGNAL_REGISTRY.items():
        considered = [
            point for point in _considered_points(raw, metric)
            if reference_start <= point.day <= end
        ]
        streams = _group_streams(considered)
        states = [
            _period_state(raw, definition, key, points, start, end, reference_start, reference_end)
            for key, points in sorted(streams.items(), key=lambda item: _stream_sort_key(item[0]))
        ]
        if not states:
            states.append(_period_state(raw, definition, None, [], start, end, reference_start, reference_end))
        output[metric] = states
    return output


def _daily_state(
    raw: RawDailyProfile, definition: SignalDefinition, key: StreamKey | None,
    considered: list[SeriesPoint], supplied: list[BaselineStats],
) -> SignalState:
    points, limitations = _qualified_points(raw, definition, considered)
    current = [point for point in points if point.day == raw.day]
    rejected_current = [
        point for point in considered
        if point.day == raw.day and _rejection(raw, definition, point) is not None
    ]
    expected = definition.coverage_rule.expected_days
    start = raw.day - timedelta(days=expected - 1)
    window = [point for point in points if start <= point.day <= raw.day]
    by_day = _group_days(window)
    value = _reduce(current, definition)
    prior = [point for point in points if point.day < raw.day]
    status = _freshness(value, rejected_current, bool(prior))
    target = _day_coverage(raw, definition, raw.day, current, status)
    if value is not None and not target.complete:
        status = target.status = SignalStatus.PARTIAL
    truncated = _metric_truncated(raw, definition.normalized_metric)
    if truncated:
        limitations.append("sample_query_truncated")
        if value is not None:
            status = target.status = SignalStatus.PARTIAL
    baseline = _baseline(raw, definition, points, supplied)
    usable_baseline = baseline if baseline and baseline.status == Availability.AVAILABLE else None
    can_compare = value is not None and target.complete and not truncated and usable_baseline is not None
    comparison = _compare(value, usable_baseline, definition, enabled=can_compare)
    if baseline is None or baseline.status != Availability.AVAILABLE:
        limitations.append("baseline_requires_14_of_28_distinct_days")
    latest = _latest_point(points)
    last_day = latest.day if latest else None
    last_value = _reduce([point for point in points if point.day == last_day], definition)
    return SignalState(
        **_identity(definition, key, considered),
        status=status, value=value, baseline_stats=baseline,
        comparison_status=SignalStatus.AVAILABLE if can_compare else SignalStatus.INSUFFICIENT,
        window_start=start, window_end=raw.day,
        sample_count=len(window), distinct_days=len(by_day), expected_days=expected,
        coverage_ratio=len(by_day) / expected, target_day_coverage=target,
        observed_at=latest.observed_at if latest else None,
        fetched_at=None, as_of=_as_utc(raw.as_of),
        last_observed_day=last_day, last_observed_value=last_value,
        limitations=sorted(set(limitations)), **comparison,
    )


def _period_state(
    raw: RawDailyProfile, definition: SignalDefinition, key: StreamKey | None,
    considered: list[SeriesPoint], start: date, end: date,
    reference_start: date, reference_end: date,
) -> PeriodSignalState:
    points, limitations = _qualified_points(raw, definition, considered)
    by_day = _group_days(points)
    current = _daily_values(raw, definition, by_day, start, end)
    previous = _daily_values(raw, definition, by_day, reference_start, reference_end)
    complete = [item.value for item in current if item.complete]
    complete_previous = [item.value for item in previous if item.complete]
    current_values = complete or [item.value for item in current]
    previous_values = complete_previous or [item.value for item in previous]
    expected = (end - start).days + 1
    reference_expected = (reference_end - reference_start).days + 1
    period_mode = (raw.report_context or {}).get("period_mode")
    minimum = comparison_gate(expected, period_mode)
    reference_minimum = comparison_gate(reference_expected, period_mode)
    value = _aggregate(current_values, definition)
    reference_value = _aggregate(previous_values, definition)
    rejected = [
        point for point in considered
        if start <= point.day <= end and _rejection(raw, definition, point) is not None
    ]
    status = _freshness(value, rejected, bool(previous))
    if value is not None and len(complete) != expected:
        status = SignalStatus.PARTIAL
        limitations.append("period_contains_missing_or_incomplete_days")
    if previous and len(complete_previous) != reference_expected:
        limitations.append("reference_contains_missing_or_incomplete_days")
    truncated = _metric_truncated(raw, definition.normalized_metric)
    if truncated:
        limitations.append("sample_query_truncated")
        if value is not None:
            status = SignalStatus.PARTIAL
    can_compare = (
        len(complete) >= minimum and len(complete_previous) >= reference_minimum and not truncated
    )
    spread = _mad(previous_values)
    deviation = value - reference_value if can_compare else None
    percent = (
        100.0 * deviation / reference_value
        if deviation is not None and reference_value != 0 and definition.normalized_metric not in _CLOCK_METRICS
        else None
    )
    robust_z = 0.6745 * deviation / spread if deviation is not None and spread not in (None, 0) else None
    reference_status = _freshness(reference_value, [], False)
    if reference_value is not None and len(complete_previous) != reference_expected:
        reference_status = SignalStatus.PARTIAL
    reference_points = [point for point in points if reference_start <= point.day <= reference_end]
    reference = PeriodReference(
        start=reference_start, end=reference_end, expected_days=reference_expected,
        effective_days=len(complete_previous), distinct_days=len(previous),
        sample_count=sum(item.sample_count for item in previous),
        coverage_ratio=len(complete_previous) / reference_expected,
        value=reference_value,
        median=float(median(previous_values)) if previous_values else None,
        average=fmean(previous_values) if previous_values else None,
        status=reference_status,
        source=key[0] or None if key else None,
        source_scope=key[1] or None if key else None,
        device_id=key[2] if key else None, unit=key[3] if key else definition.unit,
        measurement_kind=key[4] if key else "unknown",
        source_fields=_source_fields(reference_points),
        observed_at=_latest_observed(reference_points), fetched_at=None,
    )
    current_points = [point for point in points if start <= point.day <= end]
    target_points = by_day.get(end, [])
    target_rejected = [point for point in rejected if point.day == end]
    target_status = _freshness(_reduce(target_points, definition), target_rejected, bool(current_points or reference_points))
    target = _day_coverage(raw, definition, end, target_points, target_status)
    if not can_compare:
        limitations.append("period_comparison_coverage_insufficient")
    return PeriodSignalState(
        **_identity(definition, key, considered),
        start=start, end=end, status=status, value=value,
        median=float(median(current_values)) if current_values else None,
        average=fmean(current_values) if current_values else None,
        aggregation=definition.coverage_rule.aggregation,
        baseline=reference_value, deviation=deviation, deviation_percent=percent,
        robust_spread=spread, robust_spread_unit=reference.unit if spread is not None else None,
        robust_z=robust_z, direction=_direction(robust_z, percent),
        comparison_status=SignalStatus.AVAILABLE if can_compare else SignalStatus.INSUFFICIENT,
        minimum_comparison_days=minimum,
        effective_days=len(complete), distinct_days=len(current), expected_days=expected,
        coverage_ratio=len(complete) / expected,
        sample_count=sum(item.sample_count for item in current), daily_values=current,
        reference=reference, target_day_coverage=target,
        observed_at=_latest_observed(current_points or reference_points),
        fetched_at=None, as_of=_as_utc(raw.as_of), limitations=sorted(set(limitations)),
    )


def _summary_state(
    raw: RawDailyProfile, definition: SignalDefinition, summary: SampleWindowSummary,
) -> SignalState:
    """Expose existing target-day sample summaries without inventing history."""
    provenance = summary.provenance
    key: StreamKey = (provenance.source, provenance.source_scope, provenance.device_id, summary.unit, "sample_window")
    start, end = local_day_utc_bounds(raw.day, _zone(raw))
    first = summary.first_observed_at
    last = summary.last_observed_at
    in_window = (
        first is not None and last is not None
        and start <= _as_utc(first) <= _as_utc(last) < end
        and _as_utc(last) <= _as_utc(raw.as_of)
    )
    candidate = SeriesPoint(
        metric=definition.normalized_metric, value=summary.average,
        unit=summary.unit, day=raw.day, observed_at=last or raw.day,
        source=provenance.source, source_scope=provenance.source_scope,
        device_id=provenance.device_id, source_field=f"sample_window.{summary.metric}",
    )
    rejection = _rejection(raw, definition, candidate)
    valid = in_window and summary.sample_count > 0 and rejection is None
    expected_minutes = int((end - start).total_seconds() / 60)
    observed_minutes = min(summary.observed_minutes, expected_minutes)
    complete = (
        valid and observed_minutes == expected_minutes and not summary.truncated
        and _closed_day(raw, raw.day)
    )
    status = SignalStatus.AVAILABLE if complete else SignalStatus.PARTIAL if valid else SignalStatus.INSUFFICIENT
    limitations = list(summary.limitations)
    if not complete:
        limitations.append("sample_window_does_not_cover_complete_day")
    if summary.truncated:
        limitations.append("sample_query_truncated")
    if not in_window:
        limitations.append("sample_window_outside_target_or_cutoff")
    if rejection:
        limitations.append(rejection)
    coverage = SignalCoverage(
        day=raw.day, status=status, complete=bool(complete),
        sample_count=summary.sample_count if valid else 0,
        distinct_days=int(valid), coverage_ratio=float(valid),
        first_observed_at=first if valid else None, last_observed_at=last if valid else None,
        observed_minutes=observed_minutes if valid else 0, expected_minutes=expected_minutes,
        minute_coverage_ratio=observed_minutes / expected_minutes if valid else 0.0,
        limitations=sorted(set(limitations)),
    )
    return SignalState(
        **_identity(definition, key, [candidate]),
        status=status, value=float(summary.average) if valid else None,
        window_start=raw.day - timedelta(days=27), window_end=raw.day,
        sample_count=summary.sample_count if valid else 0, distinct_days=int(valid),
        expected_days=28, coverage_ratio=int(valid) / 28,
        target_day_coverage=coverage, observed_at=last if valid else None,
        fetched_at=None, as_of=_as_utc(raw.as_of),
        last_observed_day=raw.day if valid else None,
        last_observed_value=float(summary.average) if valid else None,
        limitations=sorted(set(limitations + ["historical_sample_windows_not_loaded"])),
    )


def _baseline(
    raw: RawDailyProfile, definition: SignalDefinition, points: list[SeriesPoint],
    supplied: list[BaselineStats],
) -> BaselineStats | None:
    if not points:
        return None
    grouped = _group_days(points)
    complete_days = {
        day for day, items in grouped.items()
        if _day_complete(raw, definition, day, items)
    }
    baseline_points = [point for point in points if point.day in complete_days]
    if definition.coverage_rule.daily_reduction == "latest":
        # BaselineEngine owns median/MAD and log RMSSD.  Replicate the selected
        # daily value without changing the real sample-count denominator.
        by_day = _group_days(baseline_points)
        values = {day: _reduce(items, definition) for day, items in by_day.items()}
        baseline_points = [replace(point, value=values[point.day]) for point in baseline_points]
    metric = definition.normalized_metric
    stats = BaselineEngine().build({metric: baseline_points}, raw.day).get(metric, [])
    computed = next((item for item in stats if item.window_days == 28), None)
    if computed is None:
        # A current-date identity point gives the existing engine an empty
        # historical window; incomplete historical windows never become a
        # supposedly available baseline through this warmup path.
        identity_point = replace(points[0], day=raw.day, observed_at=raw.day)
        stats = BaselineEngine().build({metric: [identity_point]}, raw.day).get(metric, [])
        computed = next((item for item in stats if item.window_days == 28), None)
    return next((item.model_copy(deep=True) for item in supplied if item == computed), computed)


def _compare(
    value: float | None, baseline: BaselineStats | None,
    definition: SignalDefinition, *, enabled: bool,
) -> dict:
    result = {
        "baseline": baseline.reference_value if baseline else None,
        "deviation": None, "deviation_percent": None,
        "robust_spread": baseline.mad if baseline else None,
        "robust_spread_unit": (
            f"ln({baseline.unit})" if baseline.transform == "natural_log" else baseline.unit
        ) if baseline else None,
        "robust_z": None, "direction": "unknown",
    }
    if not enabled:
        return result
    deviation = BaselineEngine.deviation(value, baseline)
    result.update(
        deviation=value - baseline.reference_value,
        deviation_percent=deviation.percent,
        robust_z=deviation.robust_z,
        direction=deviation.direction,
    )
    if definition.normalized_metric in _CLOCK_METRICS:
        result["deviation_percent"] = None
        result["direction"] = _direction(deviation.robust_z, None)
    return result


def _considered_points(raw: RawDailyProfile, metric: str) -> list[SeriesPoint]:
    cutoff = _as_utc(raw.as_of)
    cutoff_day = local_day(cutoff, _zone(raw))
    output = []
    for point in raw.series.get(metric, []):
        if type(point.day) is not date or point.day > raw.day or point.day > cutoff_day:
            continue
        observed = point.observed_at
        if isinstance(observed, datetime):
            if _as_utc(observed) <= cutoff:
                output.append(point)
        elif type(observed) is date and observed <= cutoff_day:
            output.append(point)
    return output


def _qualified_points(
    raw: RawDailyProfile, definition: SignalDefinition, points: list[SeriesPoint],
) -> tuple[list[SeriesPoint], list[str]]:
    eligible = []
    limitations = []
    for point in points:
        rejection = _rejection(raw, definition, point)
        if rejection is None:
            eligible.append(point)
        else:
            limitations.append(rejection)
    return eligible, limitations


def _rejection(raw: RawDailyProfile, definition: SignalDefinition, point: SeriesPoint) -> str | None:
    if point.metric != definition.normalized_metric:
        return "metric_mismatch"
    rule = definition.qualification
    if rule.requires_source and (not point.source or str(point.source).strip().lower() == "unknown"):
        return "source_unknown"
    if point.unit != definition.unit:
        return "unit_mismatch"
    if rule.requires_device_for_device_scope and point.source_scope == "device" and not point.device_id:
        return "device_attribution_missing"
    if isinstance(point.value, bool) or not isinstance(point.value, (int, float)) or not isfinite(point.value):
        return "value_not_finite_numeric"
    value = float(point.value)
    if rule.minimum is not None and (value < rule.minimum or (rule.minimum_exclusive and value == rule.minimum)):
        return "value_out_of_range"
    if rule.maximum is not None and value > rule.maximum:
        return "value_out_of_range"
    if value in rule.excluded_values:
        return "vendor_missing_sentinel"
    if not _date_matches(raw, definition, point):
        return "observation_date_mismatch"
    if point.source_field and point.source_field.startswith("sleep."):
        record = raw.sleep_by_day.get(point.day, {})
        window = local_sleep_window(point.day, record.get("bedtime"), record.get("wake_time"), _zone(raw))
        if window is not None and window[1].astimezone(timezone.utc) > _as_utc(raw.as_of):
            return "sleep_window_after_cutoff"
    if rule.zero_requires_verified_training_day and value == 0:
        # Explicit vendor daily metrics can report a real zero. Normalized
        # training records and unidentified legacy zero summaries require the
        # persisted ledger before they can represent a confirmed rest day.
        if not (point.source_field or "").startswith("daily.") and not _training_day_verified(raw, point.day):
            return "training_zero_day_unverified"
    return None


def _date_matches(raw: RawDailyProfile, definition: SignalDefinition, point: SeriesPoint) -> bool:
    observed = point.observed_at
    if type(observed) is date:
        return observed == point.day
    if not isinstance(observed, datetime):
        return False
    observation_day = local_day(observed, _zone(raw))
    if observation_day == point.day:
        return True
    if definition.calendar_semantics != "sleep_day":
        return False
    record = raw.sleep_by_day.get(point.day, {})
    if record.get("source") not in (None, point.source):
        return False
    if record.get("device_id") not in (None, point.device_id):
        return False
    window = local_sleep_window(point.day, record.get("bedtime"), record.get("wake_time"), _zone(raw))
    return window is not None and window[0].astimezone(timezone.utc) <= _as_utc(observed) < window[1].astimezone(timezone.utc)


def _training_day_verified(raw: RawDailyProfile, day: date) -> bool:
    coverage = raw.training_history_coverage
    if coverage.get("truncated") or coverage.get("budget_exhausted"):
        return False
    return day.isoformat() in {str(value)[:10] for value in coverage.get("verified_days", [])}


def _group_streams(points: list[SeriesPoint]) -> dict[StreamKey, list[SeriesPoint]]:
    output = defaultdict(list)
    for point in points:
        kind: MeasurementKind = "samples" if isinstance(point.observed_at, datetime) else "daily_summary"
        output[(point.source, point.source_scope, point.device_id, point.unit, kind)].append(point)
    return dict(output)


def _identity(definition: SignalDefinition, key: StreamKey | None, points: list[SeriesPoint]) -> dict:
    return {
        "metric": definition.normalized_metric, "normalized_metric": definition.normalized_metric,
        "unit": key[3] if key else definition.unit,
        "calendar_semantics": definition.calendar_semantics,
        "measurement_kind": key[4] if key else "unknown",
        "source": (key[0] or None) if key else None,
        "source_scope": (key[1] or None) if key else None,
        "device_id": key[2] if key else None, "source_fields": _source_fields(points),
        "visible_channels": definition.visible_channels, "decision_role": definition.decision_role,
        "missing_late_policy": definition.missing_late_policy,
    }


def _group_days(points: list[SeriesPoint]) -> dict[date, list[SeriesPoint]]:
    output = defaultdict(list)
    for point in points:
        output[point.day].append(point)
    return dict(output)


def _daily_values(
    raw: RawDailyProfile, definition: SignalDefinition, by_day: dict[date, list[SeriesPoint]],
    start: date, end: date,
) -> list[DailySignalValue]:
    return [
        DailySignalValue(
            day=day, value=_reduce(points, definition), sample_count=len(points),
            observed_at=_latest_observed(points), complete=_day_complete(raw, definition, day, points),
        )
        for day, points in sorted(by_day.items()) if start <= day <= end
    ]


def _day_coverage(
    raw: RawDailyProfile, definition: SignalDefinition, day: date,
    points: list[SeriesPoint], status: SignalStatus,
) -> SignalCoverage:
    start, end = local_day_utc_bounds(day, _zone(raw))
    expected_minutes = int((end - start).total_seconds() / 60)
    sampled = [point for point in points if isinstance(point.observed_at, datetime)]
    minutes = _observed_minutes(sampled) if sampled else None
    complete = bool(points) and _day_complete(raw, definition, day, points)
    limitations = []
    if points and not complete:
        status = SignalStatus.PARTIAL
        limitations.append("sample_window_incomplete" if definition.coverage_rule.sample_window else "local_day_incomplete")
    return SignalCoverage(
        day=day, sample_count=len(points), distinct_days=int(bool(points)),
        coverage_ratio=float(bool(points)), status=status, complete=complete,
        first_observed_at=min((point.observed_at for point in points), key=_observed_key) if points else None,
        last_observed_at=_latest_observed(points), observed_minutes=minutes,
        expected_minutes=expected_minutes,
        minute_coverage_ratio=min(minutes / expected_minutes, 1.0) if minutes is not None else None,
        limitations=limitations,
    )


def _day_complete(raw: RawDailyProfile, definition: SignalDefinition, day: date, points: list[SeriesPoint]) -> bool:
    if not points:
        return False
    rule = definition.coverage_rule
    if len(points) < rule.minimum_samples_per_day:
        return False
    if rule.requires_closed_day and not _closed_day(raw, day):
        return False
    if rule.sample_window and any(isinstance(point.observed_at, datetime) for point in points):
        start, end = local_day_utc_bounds(day, _zone(raw))
        return _observed_minutes(points) >= int((end - start).total_seconds() / 60)
    return True


def _closed_day(raw: RawDailyProfile, day: date) -> bool:
    return day < local_day(_as_utc(raw.as_of), _zone(raw))


def _observed_minutes(points: list[SeriesPoint]) -> int:
    return len({
        _as_utc(point.observed_at).replace(second=0, microsecond=0)
        for point in points if isinstance(point.observed_at, datetime)
    })


def _metric_truncated(raw: RawDailyProfile, metric: str) -> bool:
    if raw.data_quality is None:
        return False
    return any(
        flag.code == "SAMPLE_LIMIT_REACHED" and metric in flag.detail
        for flag in raw.data_quality.flags
    )


def _reduce(points: list[SeriesPoint], definition: SignalDefinition) -> float | None:
    if not points:
        return None
    if definition.coverage_rule.daily_reduction == "latest":
        latest_time = max(_observed_key(point.observed_at) for point in points)
        latest_values = [point.value for point in points if _observed_key(point.observed_at) == latest_time]
        return float(median(latest_values))
    return float(median(point.value for point in points))


def _aggregate(values: list[float], definition: SignalDefinition) -> float | None:
    if not values:
        return None
    return fmean(values) if definition.coverage_rule.aggregation == "mean_per_effective_day" else float(median(values))


def _freshness(value: float | None, rejected: list[SeriesPoint], prior: bool) -> SignalStatus:
    if value is not None:
        return SignalStatus.AVAILABLE
    if rejected:
        return SignalStatus.INSUFFICIENT
    return SignalStatus.STALE if prior else SignalStatus.UNKNOWN


def _mad(values: list[float]) -> float | None:
    if not values:
        return None
    center = median(values)
    return float(median(abs(value - center) for value in values))


def _direction(robust_z: float | None, percent: float | None) -> Direction:
    comparison = robust_z if robust_z is not None else percent / 10 if percent is not None else None
    if comparison is None:
        return "unknown"
    return "above" if comparison > 0.75 else "below" if comparison < -0.75 else "near"


def _latest_point(points: list[SeriesPoint]) -> SeriesPoint | None:
    return max(points, key=lambda point: (_observed_key(point.observed_at), point.day)) if points else None


def _latest_observed(points: list[SeriesPoint]) -> datetime | date | None:
    point = _latest_point(points)
    return point.observed_at if point else None


def _source_fields(points: list[SeriesPoint]) -> list[str]:
    return sorted({point.source_field for point in points if point.source_field})


def _stream_sort_key(key: StreamKey) -> tuple[str, ...]:
    return tuple(value or "" for value in key)


def _observed_key(value: datetime | date) -> datetime:
    return _as_utc(value) if isinstance(value, datetime) else datetime.combine(value, time.min, timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _zone(raw: RawDailyProfile) -> str:
    # UTC is the storage frame when no reporting zone was supplied; this pure
    # module never imports the ambient settings or consults a wall clock.
    return raw.timezone_name or "UTC"
