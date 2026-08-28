"""Deterministic, device-isolated personal associations over daily observations."""

from datetime import date, timedelta
from hashlib import sha256
from math import sqrt
from statistics import median

from .contracts import (
    Availability,
    ConfidenceBand,
    PersonalAssociation,
    PersonalAssociationProfile,
)
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .profile import RawDailyProfile
from .trend import METRIC_LABELS, stream_daily_values


WINDOWS = (60, 90)
MINIMUM_PAIRED_DAYS = {60: 30, 90: 45}
MINIMUM_COVERAGE = 0.5
CANDIDATES = (
    ("sleep_duration", "hrv_rmssd", 1),
    ("sleep_duration", "hrv_sdnn", 1),
    ("sleep_duration", "sleep_hrv", 1),
    ("sleep_duration", "resting_hr", 1),
    ("sleep_duration", "sleep_rhr", 1),
    ("training_load", "hrv_rmssd", 1),
    ("training_load", "hrv_sdnn", 1),
    ("training_load", "sleep_hrv", 1),
    ("training_load", "resting_hr", 1),
    ("training_load", "sleep_rhr", 1),
    ("training_load", "sleep_duration", 1),
    ("steps", "sleep_duration", 0),
)


class PersonalAssociationEngine:
    def build(self, analysis_run_id: str, raw: RawDailyProfile) -> PersonalAssociationProfile:
        streams = {
            metric: stream_daily_values(raw.series.get(metric, []))
            for metric in {item for pair in CANDIDATES for item in pair[:2]}
        }
        associations = []
        for predictor_metric, outcome_metric, lag_days in CANDIDATES:
            for predictor_identity, predictor_daily in sorted(
                streams[predictor_metric].items(), key=_stream_sort_key
            ):
                for outcome_identity, outcome_daily in sorted(
                    streams[outcome_metric].items(), key=_stream_sort_key
                ):
                    for window_days in WINDOWS:
                        associations.append(self._evaluate(
                            raw,
                            predictor_metric,
                            predictor_identity,
                            predictor_daily,
                            outcome_metric,
                            outcome_identity,
                            outcome_daily,
                            lag_days,
                            window_days,
                        ))
        limitations = [
            "个人关联仅描述历史观测中的统计关系，不代表因果关系。",
            "缺失日期按配对排除，不补零、不插值，也不跨设备合并。",
        ]
        if not any(item.status == Availability.AVAILABLE for item in associations):
            limitations.append("当前没有达到样本量、覆盖率与变异度门槛的个人关联。")
        return PersonalAssociationProfile(
            analysis_run_id=analysis_run_id,
            user_id=raw.user_id,
            date=raw.day,
            associations=associations,
            limitations=limitations,
        )

    @staticmethod
    def _evaluate(
        raw: RawDailyProfile,
        predictor_metric: str,
        predictor_identity: tuple[str, str, str | None, str],
        predictor_daily: dict[date, float],
        outcome_metric: str,
        outcome_identity: tuple[str, str, str | None, str],
        outcome_daily: dict[date, float],
        lag_days: int,
        window_days: int,
    ) -> PersonalAssociation:
        start = raw.day - timedelta(days=window_days - 1)
        pairs = [
            (day, value, outcome_daily[day + timedelta(days=lag_days)])
            for day, value in sorted(predictor_daily.items())
            if start <= day <= raw.day - timedelta(days=lag_days)
            and day + timedelta(days=lag_days) in outcome_daily
        ]
        possible_days = window_days - lag_days
        paired_days = len(pairs)
        minimum = MINIMUM_PAIRED_DAYS[window_days]
        coverage = paired_days / possible_days
        predictor_values = [item[1] for item in pairs]
        outcome_values = [item[2] for item in pairs]
        meaningful_variation = (
            _has_meaningful_variation(predictor_values)
            and _has_meaningful_variation(outcome_values)
        )
        available = (
            paired_days >= minimum
            and coverage >= MINIMUM_COVERAGE
            and meaningful_variation
        )
        predictor_source, predictor_scope, predictor_device, predictor_unit = predictor_identity
        outcome_source, outcome_scope, outcome_device, outcome_unit = outcome_identity
        confounded = 0
        if predictor_metric == "training_load":
            workout_days = {
                item["local_day"] for item in raw.workouts
                if isinstance(item.get("local_day"), date)
            }
            confounded = sum(
                1 for day, _, _ in pairs
                if day + timedelta(days=lag_days) in workout_days
            )
        limitations = ["该结果是观测性关联，不用于证明因果关系。"]
        if confounded:
            limitations.append(
                f"{confounded} 个配对的结果日期还有其他训练，训练响应可能混杂。"
            )
        if paired_days < minimum:
            limitations.append(f"有效配对 {paired_days} 天，低于最低 {minimum} 天。")
        if coverage < MINIMUM_COVERAGE:
            limitations.append("有效配对覆盖率低于 50%。")
        if pairs and not meaningful_variation:
            limitations.append("至少一个变量的有效变化不足，无法稳定计算等级相关。")

        coefficient = _spearman(predictor_values, outcome_values) if available else None
        if coefficient is None:
            direction = "INSUFFICIENT_DATA"
            direction_label = "数据不足"
            strength = "INSUFFICIENT_DATA"
            strength_label = "数据不足"
            confidence = ConfidenceBand.NONE
            summary = (
                f"{METRIC_LABELS[predictor_metric]}与"
                f"{_lag_label(lag_days)}{METRIC_LABELS[outcome_metric]}的数据不足。"
            )
            status = Availability.INSUFFICIENT_DATA
        else:
            direction, direction_label = _direction(coefficient)
            strength, strength_label = _strength(coefficient)
            confidence = _confidence(coverage, paired_days, confounded)
            summary = (
                f"过去 {window_days} 天，{METRIC_LABELS[predictor_metric]}与"
                f"{_lag_label(lag_days)}{METRIC_LABELS[outcome_metric]}呈"
                f"{strength_label}{direction_label}关联（ρ={coefficient:.3f}）。"
            )
            status = Availability.AVAILABLE

        identity = "|".join(str(value or "") for value in (
            predictor_metric, *predictor_identity, outcome_metric, *outcome_identity,
            lag_days, window_days,
        ))
        return PersonalAssociation(
            id=f"association-{sha256(identity.encode()).hexdigest()[:20]}",
            status=status,
            status_label=AVAILABILITY_LABELS[status.value],
            predictor_metric=predictor_metric,
            predictor_metric_label=METRIC_LABELS[predictor_metric],
            predictor_source=predictor_source,
            predictor_source_scope=predictor_scope,
            predictor_device_id=predictor_device,
            predictor_unit=predictor_unit,
            outcome_metric=outcome_metric,
            outcome_metric_label=METRIC_LABELS[outcome_metric],
            outcome_source=outcome_source,
            outcome_source_scope=outcome_scope,
            outcome_device_id=outcome_device,
            outcome_unit=outcome_unit,
            lag_days=lag_days,
            window_days=window_days,
            paired_days=paired_days,
            minimum_paired_days=minimum,
            coverage_ratio=round(coverage, 4),
            coefficient=round(coefficient, 4) if coefficient is not None else None,
            direction=direction,
            direction_label=direction_label,
            strength=strength,
            strength_label=strength_label,
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            predictor_median=(round(float(median(predictor_values)), 3) if pairs else None),
            outcome_median=(round(float(median(outcome_values)), 3) if pairs else None),
            confounded_pair_days=confounded,
            summary=summary,
            limitations=limitations,
        )


def _stream_sort_key(item):
    return tuple(value or "" for value in item[0])


def _has_meaningful_variation(values: list[float]) -> bool:
    if len(set(values)) < 3:
        return False
    center = median(values)
    return median(abs(value - center) for value in values) > 0


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average_rank = ((cursor + 1) + end) / 2
        for index in range(cursor, end):
            ranks[indexed[index][0]] = average_rank
        cursor = end
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    ranked_x = _average_ranks(xs)
    ranked_y = _average_ranks(ys)
    mean_x = sum(ranked_x) / len(ranked_x)
    mean_y = sum(ranked_y) / len(ranked_y)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(ranked_x, ranked_y))
    denominator = sqrt(
        sum((x - mean_x) ** 2 for x in ranked_x)
        * sum((y - mean_y) ** 2 for y in ranked_y)
    )
    return numerator / denominator if denominator else None


def _direction(coefficient: float) -> tuple[str, str]:
    if abs(coefficient) < 0.05:
        return "NEUTRAL", "近中性"
    if coefficient > 0:
        return "POSITIVE", "正向"
    return "NEGATIVE", "负向"


def _strength(coefficient: float) -> tuple[str, str]:
    magnitude = abs(coefficient)
    if magnitude < 0.2:
        return "WEAK", "弱"
    if magnitude < 0.4:
        return "MODEST", "较弱"
    if magnitude < 0.6:
        return "MODERATE", "中等"
    return "STRONG", "较强"


def _confidence(coverage: float, paired_days: int, confounded: int) -> ConfidenceBand:
    confounded_ratio = confounded / paired_days if paired_days else 0
    if coverage >= 0.8 and confounded_ratio < 0.25:
        return ConfidenceBand.HIGH
    if coverage >= 0.6 and confounded_ratio < 0.5:
        return ConfidenceBand.MODERATE
    return ConfidenceBand.LOW


def _lag_label(lag_days: int) -> str:
    return "同日" if lag_days == 0 else "次日"
