"""Deterministic post-workout response analysis with device-isolated signals."""

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .baseline import BaselineEngine
from .contracts import (
    Availability,
    ConfidenceBand,
    RecoveryOutcome,
    ResponseMetricObservation,
    SubjectiveFeedback,
    TrainingResponse,
    TrainingResponseDay,
    WorkoutExposure,
)
from .localization import CONFIDENCE_LABELS
from .profile import RawDailyProfile, SeriesPoint


RESPONSE_METRICS = (
    "hrv_rmssd",
    "hrv_sdnn",
    "sleep_hrv",
    "resting_hr",
    "sleep_rhr",
    "sleep_duration",
)
RECOVERY_LABELS = {
    RecoveryOutcome.RETURNED_TO_BASELINE: "已回到个人基线",
    RecoveryOutcome.NOT_RETURNED: "三天内尚未回到个人基线",
    RecoveryOutcome.CONFOUNDED: "恢复窗口存在其他训练干扰",
    RecoveryOutcome.INSUFFICIENT_DATA: "恢复数据不足",
}


class TrainingResponseEngine:
    def build(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        feedback: list[SubjectiveFeedback],
        recommendation_by_workout: dict[str, str],
        history_days: int = 90,
    ) -> list[TrainingResponse]:
        start = raw.day - timedelta(days=history_days - 1)
        workouts = [
            item for item in raw.workouts
            if item.get("workout_id")
            and isinstance(item.get("local_day"), date)
            and start <= item["local_day"] < raw.day
        ]
        workout_ids_by_day: dict[date, list[str]] = defaultdict(list)
        for item in workouts:
            workout_ids_by_day[item["local_day"]].append(item["workout_id"])
        feedback_by_workout: dict[str, list[SubjectiveFeedback]] = defaultdict(list)
        for item in feedback:
            if item.workout_id:
                feedback_by_workout[item.workout_id].append(item)

        return [
            self._one(
                analysis_run_id,
                raw,
                workout,
                workout_ids_by_day,
                feedback_by_workout.get(workout["workout_id"], []),
                recommendation_by_workout.get(workout["workout_id"]),
            )
            for workout in sorted(workouts, key=lambda item: (item["local_day"], item["workout_id"]))
        ]

    def _one(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        workout: dict,
        workout_ids_by_day: dict[date, list[str]],
        feedback: list[SubjectiveFeedback],
        recommendation_id: str | None,
    ) -> TrainingResponse:
        workout_day = workout["local_day"]
        baselines = BaselineEngine().build(raw.series, workout_day)
        eligible_baselines = {
            (item.metric, item.source, item.source_scope, item.device_id, item.unit): item
            for metric in RESPONSE_METRICS
            for item in baselines.get(metric, [])
            if item.window_days == 28 and item.status == Availability.AVAILABLE
        }
        response_days: list[TrainingResponseDay] = []
        missing_windows: list[str] = []
        all_overlaps: set[str] = set()
        expected = 0
        observed = 0

        for offset in (1, 2, 3):
            response_day = workout_day + timedelta(days=offset)
            overlaps = sorted(
                item for item in workout_ids_by_day.get(response_day, [])
                if item != workout["workout_id"]
            )
            all_overlaps.update(overlaps)
            observations: list[ResponseMetricObservation] = []
            if response_day > raw.day:
                missing_windows.append(f"T+{offset} 尚未到达")
            else:
                expected += len(eligible_baselines)
                observations = _observations(raw, response_day, eligible_baselines)
                observed += sum(
                    item.status == Availability.AVAILABLE for item in observations
                )
                if not any(item.status == Availability.AVAILABLE for item in observations):
                    missing_windows.append(f"T+{offset} 缺少生理观测")
            response_days.append(TrainingResponseDay(
                day_offset=offset,
                date=response_day,
                observations=observations,
                overlapping_workout_ids=overlaps,
            ))

        coverage = observed / expected if expected else 0.0
        recovery_status, recovery_hours = _recovery(response_days, bool(all_overlaps))
        confidence = _confidence(coverage, bool(all_overlaps))
        limitations = []
        if all_overlaps:
            limitations.append("T+1 至 T+3 恢复窗口包含其他训练，不能归因于单次训练。")
        if missing_windows:
            limitations.append("部分训练后观察窗口尚未到达或缺少数据。")
        if not eligible_baselines:
            limitations.append("训练前 28 天没有足够的设备级生理基线。")
        data = workout.get("data") or {}
        return TrainingResponse(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            exposure=WorkoutExposure(
                workout_id=workout["workout_id"],
                date=workout_day,
                type=str(data.get("type") or "other"),
                sport_mode=str(data.get("sport_mode") or "unknown"),
                sport_mode_label=str(data.get("sport_mode_label") or "未知运动"),
                training_family=str(data.get("training_family") or "skill"),
                training_family_label=str(data.get("training_family_label") or "技巧训练"),
                duration_minutes=int(data.get("duration") or 0),
                vendor_load=float(data.get("load") or 0),
                heart_rate_avg_bpm=_positive_int(data.get("heart_rate_avg")),
                heart_rate_max_bpm=_positive_int(data.get("heart_rate_max")),
            ),
            recommendation_id=recommendation_id,
            feedback=feedback,
            response_days=response_days,
            missing_windows=missing_windows,
            overlapping_workout_ids=sorted(all_overlaps),
            recovery_status=recovery_status,
            recovery_status_label=RECOVERY_LABELS[recovery_status],
            recovery_hours=recovery_hours,
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            limitations=limitations,
        )


def _observations(raw, day, baselines) -> list[ResponseMetricObservation]:
    grouped: dict[tuple[str, str, str, str | None, str], list[SeriesPoint]] = defaultdict(list)
    for metric in RESPONSE_METRICS:
        for point in raw.series.get(metric, []):
            if point.day == day:
                grouped[(metric, point.source, point.source_scope, point.device_id, point.unit)].append(point)
    output = []
    for key, baseline in sorted(baselines.items(), key=lambda item: tuple(str(v or "") for v in item[0])):
        points = grouped.get(key, [])
        if not points:
            output.append(ResponseMetricObservation(
                metric=baseline.metric,
                device_id=baseline.device_id,
                unit=baseline.unit,
                status=Availability.INSUFFICIENT_DATA,
                baseline_reference=baseline.reference_value,
            ))
            continue
        value = float(median(point.value for point in points))
        deviation = BaselineEngine.deviation(value, baseline)
        output.append(ResponseMetricObservation(
            metric=baseline.metric,
            device_id=baseline.device_id,
            unit=baseline.unit,
            status=Availability.AVAILABLE,
            baseline_reference=baseline.reference_value,
            value=round(value, 3),
            deviation_percent=deviation.percent,
            direction=deviation.direction,
        ))
    return output


def _recovery(days, confounded):
    if confounded:
        return RecoveryOutcome.CONFOUNDED, None
    evaluable = False
    for item in days:
        available = [
            observation for observation in item.observations
            if observation.status == Availability.AVAILABLE
        ]
        hrv = [obs for obs in available if obs.metric in {"hrv_rmssd", "hrv_sdnn", "sleep_hrv"}]
        rhr = [obs for obs in available if obs.metric in {"resting_hr", "sleep_rhr"}]
        sleep = [obs for obs in available if obs.metric == "sleep_duration"]
        if not hrv or not rhr:
            continue
        evaluable = True
        if (
            any(obs.direction in {"near", "above"} for obs in hrv)
            and any(obs.direction in {"near", "below"} for obs in rhr)
            and not any(obs.direction == "below" for obs in sleep)
        ):
            return RecoveryOutcome.RETURNED_TO_BASELINE, item.day_offset * 24
    if not evaluable:
        return RecoveryOutcome.INSUFFICIENT_DATA, None
    return RecoveryOutcome.NOT_RETURNED, None


def _confidence(coverage: float, confounded: bool) -> ConfidenceBand:
    if coverage >= 0.8:
        confidence = ConfidenceBand.HIGH
    elif coverage >= 0.6:
        confidence = ConfidenceBand.MODERATE
    elif coverage > 0:
        confidence = ConfidenceBand.LOW
    else:
        confidence = ConfidenceBand.NONE
    if confounded and confidence in {ConfidenceBand.HIGH, ConfidenceBand.MODERATE}:
        return ConfidenceBand.LOW
    return confidence


def _positive_int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value > 0 else None
