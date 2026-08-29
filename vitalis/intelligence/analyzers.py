"""Deterministic feature analyzers over normalized profile data."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from math import log
from statistics import median

from .baseline import BaselineEngine, daily_stream_values
from .contracts import (
    Availability,
    BaselineStats,
    ConfidenceBand,
    HeartRateCoverageFeature,
    HrvFeatures,
    HrvStreamFeature,
    LoadState,
    ProfileStates,
    RecoveryFeatures,
    RecoveryState,
    SleepFeatures,
    SleepState,
    TrainingFeatures,
    WorkoutFeature,
)
from .profile import (
    RawDailyProfile,
    SeriesPoint,
    device_label,
    device_measurement_site,
)
from vitalis.connectors.zepp.sport_types import CATEGORY_LABELS, FAMILY_LABELS
from .localization import (
    AVAILABILITY_LABELS,
    CONFIDENCE_LABELS,
    DRIVER_LABELS,
    LIMITATION_LABELS,
    LOAD_LABELS,
    RECOVERY_LABELS,
    SLEEP_LABELS,
    labels,
)


@dataclass(frozen=True)
class _HrvCandidate:
    metric: str
    metric_priority: int
    source: str
    scope: str
    device_id: str | None
    device_label: str
    measurement_site: str
    value: float
    sample_count_today: int
    baseline: BaselineStats | None

    @property
    def selection_key(self) -> tuple:
        baseline_available = int(bool(
            self.baseline and self.baseline.status == Availability.AVAILABLE
        ))
        baseline_days = self.baseline.distinct_days if self.baseline else 0
        arm_worn = int(self.measurement_site == "upper_arm")
        return (
            baseline_available,
            self.metric_priority,
            arm_worn,
            min(self.sample_count_today, 1_440),
            baseline_days,
            self.source,
            self.scope,
            self.device_id or "",
        )


class SleepAnalyzer:
    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> tuple[SleepFeatures, SleepState]:
        record = raw.sleep_by_day.get(raw.day)
        if not record:
            return (
                SleepFeatures(
                    status=Availability.INSUFFICIENT_DATA,
                    status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                    limitations=["target_day_sleep_missing"],
                    limitation_labels=[LIMITATION_LABELS["target_day_sleep_missing"]],
                ),
                SleepState.INSUFFICIENT_DATA,
            )
        duration = int(record["sleep_duration"])
        baseline = _baseline_for(
            baselines, "sleep_duration", "zepp", "normalized_daily_record", None
        )
        deviation = BaselineEngine.deviation(duration, baseline) if baseline else None
        if duration < 420 or (deviation and deviation.direction == "below"):
            state = SleepState.BELOW_BASELINE
        elif deviation and deviation.direction == "above":
            state = SleepState.ABOVE_BASELINE
        else:
            state = SleepState.NEAR_BASELINE

        bedtimes = [
            _clock_minutes(item.get("bedtime"))
            for day, item in raw.sleep_by_day.items()
            if raw.day - timedelta(days=7) <= day <= raw.day and item.get("bedtime")
        ]
        bedtimes = [value for value in bedtimes if value is not None]
        regularity = median(abs(value - median(bedtimes)) for value in bedtimes) if len(bedtimes) >= 3 else None
        limitations = ["sleep_stages_are_trend_only"]
        if baseline is None or baseline.status == Availability.INSUFFICIENT_DATA:
            limitations.append("sleep_28d_baseline_insufficient")
        if regularity is None:
            limitations.append("sleep_regularity_history_insufficient")
        rem_sleep = record.get("rem_sleep")
        return SleepFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            duration_minutes=duration,
            bedtime=str(record["bedtime"]) if record.get("bedtime") else None,
            wake_time=str(record["wake_time"]) if record.get("wake_time") else None,
            deep_minutes=int(record.get("deep_sleep", 0)),
            rem_minutes=int(rem_sleep) if rem_sleep is not None else None,
            awake_minutes=int(record.get("awake", 0)),
            vendor_sleep_score=record.get("sleep_score"),
            duration_deviation=deviation,
            regularity_minutes=round(float(regularity), 1) if regularity is not None else None,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        ), state


class HrvAnalyzer:
    METRIC_PRIORITY = {"hrv_rmssd": 3, "sleep_hrv": 2, "hrv_sdnn": 1}

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> HrvFeatures:
        candidates: list[_HrvCandidate] = []
        for metric, priority in self.METRIC_PRIORITY.items():
            for stream, value in daily_stream_values(raw.series.get(metric, []), raw.day).items():
                source, scope, device_id = stream
                baseline = _baseline_for(baselines, metric, source, scope, device_id)
                sample_count = sum(
                    1
                    for point in raw.series.get(metric, [])
                    if point.day == raw.day
                    and (point.source, point.source_scope, point.device_id) == stream
                )
                candidates.append(_HrvCandidate(
                    metric=metric,
                    metric_priority=priority,
                    source=source,
                    scope=scope,
                    device_id=device_id,
                    device_label=device_label(raw, device_id),
                    measurement_site=device_measurement_site(raw, device_id),
                    value=value,
                    sample_count_today=sample_count,
                    baseline=baseline,
                ))
        if not candidates:
            return HrvFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                limitations=["target_day_hrv_missing"],
                limitation_labels=[LIMITATION_LABELS["target_day_hrv_missing"]],
            )
        selected = max(candidates, key=lambda item: item.selection_key)
        device_id = selected.device_id
        metric = selected.metric
        value = selected.value
        baseline = selected.baseline
        deviation = BaselineEngine.deviation(value, baseline) if baseline else None
        metric_candidates = [item for item in candidates if item.metric == metric]
        streams = [
            HrvStreamFeature(
                metric=item.metric,
                device_id=item.device_id,
                device_label=item.device_label,
                measurement_site=item.measurement_site,
                value_ms=round(item.value, 2),
                ln_rmssd=(
                    round(log(item.value), 4)
                    if item.metric == "hrv_rmssd" and item.value > 0
                    else None
                ),
                deviation=(
                    BaselineEngine.deviation(item.value, item.baseline)
                    if item.baseline else None
                ),
                sample_count_today=item.sample_count_today,
                baseline_distinct_days=(
                    item.baseline.distinct_days if item.baseline else 0
                ),
                selected=item == selected,
            )
            for item in sorted(
                metric_candidates,
                key=lambda item: (item.device_label, item.source, item.scope),
            )
        ]
        (
            fusion_direction,
            fusion_confidence,
            fusion_summary,
            fused_device_count,
        ) = _fuse_hrv_streams(metric_candidates)

        rhr_candidates = []
        for rhr_metric in ("sleep_rhr", "resting_hr"):
            for stream, rhr in daily_stream_values(raw.series.get(rhr_metric, []), raw.day).items():
                rhr_baseline = _baseline_for(baselines, rhr_metric, *stream)
                same_device = int(stream[2] == device_id and device_id is not None)
                available = int(bool(rhr_baseline and rhr_baseline.status == Availability.AVAILABLE))
                rhr_candidates.append((same_device, available, rhr_metric == "sleep_rhr", rhr, rhr_baseline))
        rhr_value = None
        rhr_deviation = None
        if rhr_candidates:
            _, _, _, rhr_value, rhr_baseline = max(rhr_candidates, key=lambda item: item[:3])
            rhr_deviation = BaselineEngine.deviation(rhr_value, rhr_baseline) if rhr_baseline else None

        limitations = []
        if baseline is None or baseline.status == Availability.INSUFFICIENT_DATA:
            limitations.append("hrv_28d_baseline_insufficient")
        if fusion_direction == "mixed":
            limitations.append("multi_device_hrv_disagreement")
        if rhr_value is None:
            limitations.append("target_day_rhr_missing")
        elif rhr_deviation is None or rhr_deviation.direction == "unknown":
            limitations.append("rhr_28d_baseline_insufficient")
        heart_rate_coverage = []
        for coverage_device_id, item in sorted(
            raw.dense_heart_rate_coverage.items(),
            key=lambda value: device_label(raw, value[0]),
        ):
            heart_rate_coverage.append(HeartRateCoverageFeature(
                device_id=coverage_device_id,
                device_label=device_label(raw, coverage_device_id),
                measurement_site=device_measurement_site(raw, coverage_device_id),
                today_coverage_minutes=round(
                    item["today_coverage_seconds"] / 60, 1
                ),
                coverage_hours_28d=round(
                    item["coverage_seconds_28d"] / 3_600, 1
                ),
                covered_days_28d=len(item["covered_days"]),
                file_count_28d=item["file_count_28d"],
                payload_decoded=item["payload_decoded"],
            ))
        if heart_rate_coverage and not any(
            item.payload_decoded for item in heart_rate_coverage
        ):
            limitations.append("dense_heart_rate_payload_not_decoded")
        return HrvFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            preferred_metric=metric,
            preferred_device_id=device_id,
            preferred_device_label=selected.device_label,
            value_ms=round(value, 2),
            ln_rmssd=round(log(value), 4) if metric == "hrv_rmssd" and value > 0 else None,
            deviation=deviation,
            fusion_method="device_specific_baseline_consensus",
            fusion_direction=fusion_direction,
            fusion_confidence=fusion_confidence,
            fusion_confidence_label=CONFIDENCE_LABELS[fusion_confidence.value],
            fusion_summary=fusion_summary,
            fused_device_count=fused_device_count,
            rhr_bpm=round(rhr_value, 1) if rhr_value is not None else None,
            rhr_deviation=rhr_deviation,
            streams=streams,
            heart_rate_coverage=heart_rate_coverage,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


def _fuse_hrv_streams(
    candidates: list[_HrvCandidate],
) -> tuple[str, ConfidenceBand, str, int]:
    interpreted = []
    for item in candidates:
        if not item.baseline or item.baseline.status != Availability.AVAILABLE:
            continue
        deviation = BaselineEngine.deviation(item.value, item.baseline)
        if deviation.direction != "unknown":
            interpreted.append((item, deviation.direction))
    if not interpreted:
        return "unknown", ConfidenceBand.NONE, "没有足够的设备内基线用于融合", 0

    counts = Counter(direction for _, direction in interpreted)
    direction, count = counts.most_common(1)[0]
    device_count = len({item.device_id or item.device_label for item, _ in interpreted})
    direction_label = {"above": "高于", "near": "接近", "below": "低于"}[direction]
    labels_text = "、".join(item.device_label for item, _ in interpreted)
    if len(counts) == 1:
        confidence = (
            ConfidenceBand.HIGH if device_count >= 2 else ConfidenceBand.MODERATE
        )
        return (
            direction,
            confidence,
            f"{device_count} 台设备相对各自 28 天基线方向一致：{direction_label}基线"
            f"（{labels_text}）",
            device_count,
        )
    if count / len(interpreted) >= 2 / 3:
        return (
            direction,
            ConfidenceBand.MODERATE,
            f"多数设备指向{direction_label}基线，但仍有设备不一致（{labels_text}）",
            device_count,
        )
    return (
        "mixed",
        ConfidenceBand.LOW,
        f"设备间方向不一致，HRV 不参与强化恢复结论（{labels_text}）",
        device_count,
    )


class TrainingAnalyzer:
    STRENGTH_TYPES = {"strength", "strength_training", "weight_training", "functional_strength"}

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> TrainingFeatures:
        if not raw.training_by_day and not raw.workouts:
            return TrainingFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                limitations=["training_history_missing"],
                limitation_labels=[LIMITATION_LABELS["training_history_missing"]],
            )
        today = raw.training_by_day.get(raw.day)
        recent_7 = [
            item for day, item in raw.training_by_day.items()
            if raw.day - timedelta(days=6) <= day <= raw.day
        ]
        recent_28 = [
            item for day, item in raw.training_by_day.items()
            if raw.day - timedelta(days=27) <= day <= raw.day
        ]
        baseline = _baseline_for(
            baselines, "training_load", "zepp", "normalized_daily_record", None
        )
        today_load = float(today.get("total_load", 0)) if today else 0.0
        deviation = BaselineEngine.deviation(today_load, baseline) if baseline else None
        if deviation and deviation.direction == "above":
            load_state = LoadState.ELEVATED
        elif deviation and deviation.direction == "below":
            load_state = LoadState.LOW
        elif deviation:
            load_state = LoadState.NORMAL
        else:
            load_state = LoadState.INSUFFICIENT_DATA

        aerobic_minutes = 0
        strength_sessions = 0
        strength_days: list[date] = []
        type_counts: dict[str, int] = {}
        type_labels: dict[str, str] = {}
        mode_counts: dict[str, int] = {}
        recent_workouts: list[WorkoutFeature] = []
        for workout in raw.workouts:
            workout_day = workout.get("local_day")
            if not workout_day or workout_day < raw.day - timedelta(days=6):
                continue
            data = workout.get("data", {})
            workout_type = str(data.get("type", "")).lower()
            sport_mode = str(data.get("sport_mode") or "unknown")
            sport_mode_label = str(data.get("sport_mode_label") or "未知运动")
            family = str(data.get("training_family") or "skill")
            type_counts[workout_type] = type_counts.get(workout_type, 0) + 1
            type_labels[workout_type] = CATEGORY_LABELS.get(workout_type, "其他运动")
            mode_counts[sport_mode_label] = mode_counts.get(sport_mode_label, 0) + 1
            if family == "aerobic":
                aerobic_minutes += int(data.get("duration", 0) or 0)
            if workout_type in self.STRENGTH_TYPES:
                strength_sessions += 1
                strength_days.append(workout_day)
            recent_workouts.append(WorkoutFeature(
                date=workout_day,
                type=workout_type,
                type_label=CATEGORY_LABELS.get(workout_type, "其他运动"),
                sport_mode=sport_mode,
                sport_mode_label=sport_mode_label,
                training_family=family,
                training_family_label=FAMILY_LABELS.get(family, "未知"),
                recognition_confidence=str(data.get("recognition_confidence", "NONE")),
                recognition_confidence_label=str(data.get("recognition_confidence_label", "无法识别")),
                recognition_source=str(data.get("recognition_source", "missing_vendor_type")),
                recognition_source_label=str(data.get("recognition_source_label", "缺少厂商运动类型")),
                vendor_type_id=data.get("vendor_type_id"),
                duration_minutes=int(data.get("duration", 0) or 0),
                vendor_load=float(data.get("load", 0) or 0),
                heart_rate_avg_bpm=int(data.get("heart_rate_avg", 0) or 0) or None,
                heart_rate_max_bpm=int(data.get("heart_rate_max", 0) or 0) or None,
                detail_available=bool(workout.get("detail_available")),
            ))
        limitations = ["training_load_is_vendor_derived"]
        if baseline is None or baseline.status == Availability.INSUFFICIENT_DATA:
            limitations.append("training_load_28d_baseline_insufficient")
        limitations.append("session_rpe_unavailable")
        limitations.append("aerobic_intensity_classification_unavailable")
        return TrainingFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            today_duration_minutes=int(today.get("total_duration", 0)) if today else 0,
            today_load=today_load,
            today_workouts=int(today.get("workout_count", 0)) if today else 0,
            duration_7d=sum(int(item.get("total_duration", 0)) for item in recent_7),
            load_7d=round(sum(float(item.get("total_load", 0)) for item in recent_7), 1),
            load_28d=round(sum(float(item.get("total_load", 0)) for item in recent_28), 1),
            aerobic_minutes_7d=aerobic_minutes,
            strength_sessions_7d=strength_sessions,
            days_since_last_strength=(raw.day - max(strength_days)).days if strength_days else None,
            workout_type_counts_7d=dict(sorted(type_counts.items())),
            workout_type_labels_7d=dict(sorted(type_labels.items())),
            sport_mode_counts_7d=dict(sorted(mode_counts.items())),
            recent_workouts=sorted(recent_workouts, key=lambda item: item.date, reverse=True),
            load_deviation=deviation,
            load_state=load_state,
            load_state_label=LOAD_LABELS[load_state.value],
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


class RecoveryAnalyzer:
    def analyze(
        self,
        raw: RawDailyProfile,
        sleep: SleepFeatures,
        sleep_state: SleepState,
        hrv: HrvFeatures,
        training: TrainingFeatures,
    ) -> RecoveryFeatures:
        positive = []
        negative = []
        hrv_direction = hrv.fusion_direction
        if hrv_direction == "unknown" and hrv.deviation:
            hrv_direction = hrv.deviation.direction
        if hrv_direction == "above":
            positive.append("HRV_ABOVE_BASELINE")
        elif hrv_direction == "below":
            negative.append("HRV_BELOW_BASELINE")
        if hrv.rhr_deviation and hrv.rhr_deviation.direction == "below":
            positive.append("RHR_BELOW_BASELINE")
        elif hrv.rhr_deviation and hrv.rhr_deviation.direction == "above":
            negative.append("RHR_ABOVE_BASELINE")
        if sleep_state == SleepState.ABOVE_BASELINE:
            positive.append("SLEEP_ABOVE_BASELINE")
        elif sleep_state == SleepState.BELOW_BASELINE:
            negative.append("SLEEP_BELOW_BASELINE")
        if training.load_state == LoadState.ELEVATED:
            negative.append("TRAINING_LOAD_ELEVATED")

        interpreted = len(positive) + len(negative)
        if interpreted < 2:
            state = RecoveryState.INSUFFICIENT_DATA
            status = Availability.INSUFFICIENT_DATA
        elif len(negative) >= 2:
            state = RecoveryState.SUPPRESSED
            status = Availability.AVAILABLE
        elif len(positive) >= 2 and not negative:
            state = RecoveryState.GOOD
            status = Availability.AVAILABLE
        else:
            state = RecoveryState.NORMAL
            status = Availability.AVAILABLE

        readiness = _current_median(raw.series.get("readiness", []), raw.day)
        charge = _current_median(
            raw.series.get("hybrid_charge", []) or raw.series.get("bio_charge", []), raw.day
        )
        limitations = list(sleep.limitations)
        if interpreted < 2:
            limitations.append("fewer_than_two_baseline_interpretable_signals")
        if readiness is not None:
            limitations.append("vendor_readiness_is_context_only")
        if charge is not None:
            limitations.append("vendor_charge_is_context_only")
        return RecoveryFeatures(
            status=status,
            status_label=AVAILABILITY_LABELS[status.value],
            state=state,
            state_label=RECOVERY_LABELS[state.value],
            positive_signals=positive,
            positive_signal_labels=labels(positive, DRIVER_LABELS),
            negative_signals=negative,
            negative_signal_labels=labels(negative, DRIVER_LABELS),
            vendor_readiness=readiness,
            vendor_charge=charge,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


def build_states(sleep: SleepState, recovery: RecoveryFeatures, training: TrainingFeatures) -> ProfileStates:
    return ProfileStates(
        sleep=sleep,
        recovery=recovery.state,
        training_load=training.load_state,
        sleep_label=SLEEP_LABELS[sleep.value],
        recovery_label=RECOVERY_LABELS[recovery.state.value],
        training_load_label=LOAD_LABELS[training.load_state.value],
    )


def _baseline_for(
    baselines: dict[str, list[BaselineStats]],
    metric: str,
    source: str,
    scope: str,
    device_id: str | None,
) -> BaselineStats | None:
    candidates = [
        item for item in baselines.get(metric, [])
        if item.window_days == 28
        and item.source == source
        and item.source_scope == scope
        and item.device_id == device_id
    ]
    return candidates[0] if candidates else None


def _current_median(points: list[SeriesPoint], day: date) -> float | None:
    values = [point.value for point in points if point.day == day]
    return round(float(median(values)), 2) if values else None


def _clock_minutes(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        hours, minutes, *_ = value.split(":")
        result = int(hours) * 60 + int(minutes)
    else:
        result = value.hour * 60 + value.minute
    return result - 1440 if result >= 12 * 60 else result
