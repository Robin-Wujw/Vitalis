"""Deterministic, device-isolated period trends over normalized daily values."""

from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from .contracts import (
    Availability,
    ConfidenceBand,
    TrendDirection,
    TrendFeature,
)
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .profile import RawDailyProfile, SeriesPoint


TREND_WINDOWS = (7, 28, 90)
MINIMUM_DAYS = {7: 4, 28: 14, 90: 30}
TREND_METRICS = {
    "sleep_duration",
    "resting_hr",
    "sleep_rhr",
    "hrv_rmssd",
    "hrv_sdnn",
    "sleep_hrv",
    "training_load",
    "training_duration",
    "steps",
    "active_minutes",
    "stress",
    "respiratory_rate",
    "spo2",
    "weight",
}
METRIC_LABELS = {
    "sleep_duration": "睡眠时长",
    "resting_hr": "静息心率",
    "sleep_rhr": "睡眠静息心率",
    "hrv_rmssd": "心率变异性 RMSSD",
    "hrv_sdnn": "心率变异性 SDNN",
    "sleep_hrv": "睡眠心率变异性",
    "training_load": "训练负荷",
    "training_duration": "训练时长",
    "steps": "步数",
    "active_minutes": "活动分钟",
    "stress": "压力",
    "respiratory_rate": "呼吸率",
    "spo2": "血氧饱和度",
    "weight": "体重",
}
TREND_LABELS = {
    TrendDirection.RISING: "上升",
    TrendDirection.STABLE: "稳定",
    TrendDirection.FALLING: "下降",
    TrendDirection.INSUFFICIENT_DATA: "数据不足",
}


class TrendEngine:
    def calculate(
        self,
        raw: RawDailyProfile,
        windows: tuple[int, ...] = TREND_WINDOWS,
    ) -> list[TrendFeature]:
        output: list[TrendFeature] = []
        for metric in sorted(TREND_METRICS & raw.series.keys()):
            streams = stream_daily_values(raw.series[metric])
            for (source, scope, device_id, unit), daily in sorted(
                streams.items(),
                key=lambda item: tuple(value or "" for value in item[0]),
            ):
                for window in windows:
                    if window not in MINIMUM_DAYS:
                        continue
                    output.append(self._trend(
                        metric,
                        source,
                        scope,
                        device_id,
                        unit,
                        daily,
                        raw.day,
                        window,
                    ))
        return output

    @staticmethod
    def _trend(
        metric: str,
        source: str,
        source_scope: str,
        device_id: str | None,
        unit: str,
        daily: dict[date, float],
        target_day: date,
        window_days: int,
    ) -> TrendFeature:
        minimum = MINIMUM_DAYS[window_days]
        current_start = target_day - timedelta(days=window_days - 1)
        previous_start = current_start - timedelta(days=window_days)
        current = sorted(
            (day, value) for day, value in daily.items()
            if current_start <= day <= target_day
        )
        previous = sorted(
            (day, value) for day, value in daily.items()
            if previous_start <= day < current_start
        )
        available = len(current) >= minimum
        if not available:
            return TrendFeature(
                metric=metric,
                metric_label=METRIC_LABELS[metric],
                window_days=window_days,
                source=source,
                source_scope=source_scope,
                device_id=device_id,
                unit=unit,
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                current_distinct_days=len(current),
                previous_distinct_days=len(previous),
                minimum_days=minimum,
                coverage_ratio=round(len(current) / window_days, 4),
                direction=TrendDirection.INSUFFICIENT_DATA,
                direction_label=TREND_LABELS[TrendDirection.INSUFFICIENT_DATA],
                confidence=ConfidenceBand.NONE,
                confidence_label=CONFIDENCE_LABELS[ConfidenceBand.NONE.value],
            )

        values = [value for _, value in current]
        center = median(values)
        previous_center = median(value for _, value in previous) if len(previous) >= minimum else None
        change = (
            100 * (center - previous_center) / abs(previous_center)
            if previous_center not in (None, 0)
            else None
        )
        slope = _linear_trend(current)
        projected_percent = 100 * slope * max(len(current) - 1, 1) / abs(center) if center else 0
        comparison = change if change is not None else projected_percent
        if comparison > 5:
            direction = TrendDirection.RISING
        elif comparison < -5:
            direction = TrendDirection.FALLING
        else:
            direction = TrendDirection.STABLE
        coverage = len(current) / window_days
        confidence = ConfidenceBand.HIGH if coverage >= 0.8 else ConfidenceBand.MODERATE
        return TrendFeature(
            metric=metric,
            metric_label=METRIC_LABELS[metric],
            window_days=window_days,
            source=source,
            source_scope=source_scope,
            device_id=device_id,
            unit=unit,
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            current_distinct_days=len(current),
            previous_distinct_days=len(previous),
            minimum_days=minimum,
            coverage_ratio=round(coverage, 4),
            current_median=round(float(center), 3),
            previous_median=round(float(previous_center), 3) if previous_center is not None else None,
            change_percent=round(change, 1) if change is not None else None,
            slope_per_day=round(slope, 4),
            variability_mad=round(float(median(abs(value - center) for value in values)), 3),
            direction=direction,
            direction_label=TREND_LABELS[direction],
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
        )


def stream_daily_values(
    points: list[SeriesPoint],
) -> dict[tuple[str, str, str | None, str], dict[date, float]]:
    grouped: dict[tuple[str, str, str | None, str], dict[date, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for point in points:
        grouped[(point.source, point.source_scope, point.device_id, point.unit)][point.day].append(
            point.value
        )
    return {
        stream: {day: float(median(values)) for day, values in by_day.items()}
        for stream, by_day in grouped.items()
    }


def _linear_trend(points: list[tuple[date, float]]) -> float:
    if len(points) < 2:
        return 0.0
    origin = points[0][0]
    xs = [(day - origin).days for day, _ in points]
    ys = [value for _, value in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
