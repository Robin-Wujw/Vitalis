"""Device-specific robust baselines for normalized physiological series."""

from collections import defaultdict
from datetime import date, timedelta
from math import exp, log
from statistics import median

from .contracts import Availability, BaselineStats, Deviation
from .profile import SeriesPoint


WINDOW_POLICIES = {7: 3, 28: 14}


class BaselineEngine:
    def build(
        self,
        series: dict[str, list[SeriesPoint]],
        target_day: date,
    ) -> dict[str, list[BaselineStats]]:
        output: dict[str, list[BaselineStats]] = {}
        for metric, points in sorted(series.items()):
            streams: dict[tuple[str, str, str | None, str], list[SeriesPoint]] = defaultdict(list)
            for point in points:
                streams[(point.source, point.source_scope, point.device_id, point.unit)].append(point)
            output[metric] = []
            for (source, scope, device_id, unit), stream in sorted(
                streams.items(), key=lambda item: tuple(value or "" for value in item[0])
            ):
                for window, minimum_days in WINDOW_POLICIES.items():
                    output[metric].append(self._stats(
                        metric, source, scope, device_id, unit, stream,
                        target_day, window, minimum_days,
                    ))
        return output

    def _stats(
        self,
        metric: str,
        source: str,
        source_scope: str,
        device_id: str | None,
        unit: str,
        points: list[SeriesPoint],
        target_day: date,
        window_days: int,
        minimum_days: int,
    ) -> BaselineStats:
        start = target_day - timedelta(days=window_days)
        daily = _daily_medians(point for point in points if start <= point.day < target_day)
        transform = "natural_log" if metric == "hrv_rmssd" else "identity"
        valid_daily = [
            (day, value) for day, value in daily
            if transform == "identity" or value > 0
        ]
        original_values = [value for _, value in valid_daily]
        values = [log(value) for value in original_values] if transform == "natural_log" else original_values
        available = len(values) >= minimum_days
        kwargs = {}
        if available:
            center = median(values)
            kwargs = {
                "median": round(center, 6),
                "mad": round(median(abs(value - center) for value in values), 6),
                "percentile_25": round(_percentile(values, 0.25), 6),
                "percentile_75": round(_percentile(values, 0.75), 6),
                "trend_per_day": round(_linear_trend([day for day, _ in valid_daily], values), 6),
                "reference_value": round(exp(center) if transform == "natural_log" else center, 3),
            }
        return BaselineStats(
            status=Availability.AVAILABLE if available else Availability.INSUFFICIENT_DATA,
            metric=metric,
            source=source,
            source_scope=source_scope,
            device_id=device_id,
            unit=unit,
            window_days=window_days,
            transform=transform,
            sample_count=sum(1 for point in points if start <= point.day < target_day),
            distinct_days=len(values),
            minimum_days=minimum_days,
            coverage_ratio=round(len(values) / window_days, 4),
            **kwargs,
        )

    @staticmethod
    def deviation(value: float, baseline: BaselineStats) -> Deviation:
        if baseline.status != Availability.AVAILABLE or baseline.reference_value is None:
            return Deviation(metric=baseline.metric, baseline_window_days=baseline.window_days)
        transformed = log(value) if baseline.transform == "natural_log" and value > 0 else value
        robust_z = None
        if baseline.median is not None and baseline.mad not in (None, 0):
            robust_z = 0.6745 * (transformed - baseline.median) / baseline.mad
        if baseline.reference_value == 0 and robust_z is None:
            return Deviation(
                metric=baseline.metric,
                baseline_window_days=baseline.window_days,
            )
        percent = (
            100 * (value - baseline.reference_value) / baseline.reference_value
            if baseline.reference_value != 0 else None
        )
        comparison = robust_z if robust_z is not None else float(percent) / 10
        direction = "near"
        if comparison > 0.75:
            direction = "above"
        elif comparison < -0.75:
            direction = "below"
        return Deviation(
            metric=baseline.metric,
            baseline_window_days=baseline.window_days,
            percent=round(percent, 1),
            robust_z=round(robust_z, 2) if robust_z is not None else None,
            direction=direction,
        )


def preferred_baseline(
    baselines: dict[str, list[BaselineStats]],
    metric: str,
    device_id: str | None,
    window_days: int = 28,
) -> BaselineStats | None:
    candidates = [
        item for item in baselines.get(metric, [])
        if item.device_id == device_id and item.window_days == window_days
    ]
    return candidates[0] if candidates else None


def daily_stream_values(points: list[SeriesPoint], day: date) -> dict[tuple[str, str, str | None], float]:
    grouped: dict[tuple[str, str, str | None], list[float]] = defaultdict(list)
    for point in points:
        if point.day == day:
            grouped[(point.source, point.source_scope, point.device_id)].append(point.value)
    return {key: median(values) for key, values in grouped.items()}


def _daily_medians(points) -> list[tuple[date, float]]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for point in points:
        grouped[point.day].append(point.value)
    return [(day, median(values)) for day, values in sorted(grouped.items())]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _linear_trend(days: list[date], values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    origin = days[0]
    xs = [(day - origin).days for day in days]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(values) / len(values)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    if denominator == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denominator
