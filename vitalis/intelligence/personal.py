"""Personal Model v1 built from robust descriptive statistics only."""

from collections import defaultdict
from statistics import median

from .contracts import (
    Availability,
    ConfidenceBand,
    PersonalMetricStats,
    PersonalModel,
    PersonalResponsePattern,
    RecoveryOutcome,
    TrainingResponse,
)
from .localization import CONFIDENCE_LABELS


class PersonalModelEngine:
    def build(self, analysis_run_id, daily, responses: list[TrainingResponse]) -> PersonalModel:
        baselines = [
            item
            for metric in daily.baselines.values()
            for item in metric
            if item.window_days == 28 and item.status == Availability.AVAILABLE
        ]
        trends = [
            item for item in daily.trends
            if item.window_days == 90 and item.status == Availability.AVAILABLE
        ]
        patterns = []
        for group_type, key_name, label_name in (
            ("training_family", "training_family", "training_family_label"),
            ("sport_mode", "sport_mode", "sport_mode_label"),
        ):
            grouped = defaultdict(list)
            labels = {}
            for response in responses:
                key = getattr(response.exposure, key_name)
                grouped[key].append(response)
                labels[key] = getattr(response.exposure, label_name)
            for key, items in sorted(grouped.items()):
                metrics = _metric_stats(items)
                coverage = median([metric.coverage_ratio for metric in metrics]) if metrics else 0
                confidence = _pattern_confidence(len(items), coverage)
                patterns.append(PersonalResponsePattern(
                    group_type=group_type,
                    group_key=key,
                    group_label=labels[key],
                    response_count=len(items),
                    metrics=metrics,
                    confidence=confidence,
                    confidence_label=CONFIDENCE_LABELS[confidence.value],
                ))
        limitations = []
        if not patterns:
            limitations.append("尚无可用于建立训练响应模式的历史训练。")
        elif all(item.confidence in {ConfidenceBand.NONE, ConfidenceBand.LOW} for item in patterns):
            limitations.append("训练响应样本量或覆盖率有限，个人模式仍处于早期阶段。")
        return PersonalModel(
            analysis_run_id=analysis_run_id,
            user_id=daily.user_id,
            date=daily.date,
            baselines=baselines,
            long_term_trends=trends,
            training_response_patterns=patterns,
            limitations=limitations,
        )


def _metric_stats(responses: list[TrainingResponse]) -> list[PersonalMetricStats]:
    values: dict[tuple[str, str | None, str], list[float]] = defaultdict(list)
    eligible = len(responses)
    for response in responses:
        first = next((item for item in response.response_days if item.day_offset == 1), None)
        if first:
            for observation in first.observations:
                if observation.deviation_percent is not None:
                    values[(
                        f"{observation.metric}_t1_deviation_percent",
                        observation.device_id,
                        "percent",
                    )].append(observation.deviation_percent)
        if (
            response.recovery_status == RecoveryOutcome.RETURNED_TO_BASELINE
            and response.recovery_hours is not None
        ):
            values[("recovery_hours", None, "hours")].append(float(response.recovery_hours))
    output = []
    for (metric, device_id, unit), samples in sorted(
        values.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        center = float(median(samples))
        output.append(PersonalMetricStats(
            metric=metric,
            device_id=device_id,
            unit=unit,
            median=round(center, 3),
            mad=round(float(median(abs(value - center) for value in samples)), 3),
            sample_count=len(samples),
            eligible_count=eligible,
            coverage_ratio=round(len(samples) / eligible, 4) if eligible else 0,
        ))
    return output


def _pattern_confidence(sample_count: int, coverage: float) -> ConfidenceBand:
    if sample_count >= 8 and coverage >= 0.75:
        return ConfidenceBand.HIGH
    if sample_count >= 4 and coverage >= 0.6:
        return ConfidenceBand.MODERATE
    if sample_count >= 2 and coverage > 0:
        return ConfidenceBand.LOW
    return ConfidenceBand.NONE
