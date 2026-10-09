"""Deterministic daily, running and strength trends with explicit coverage."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import isfinite
from statistics import median

from .contracts import Availability, ConfidenceBand, TrendDirection, TrendFeature
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .profile import RawDailyProfile, SeriesPoint
from .running import RunningAnalyzer
from .strength import StrengthAnalyzer


TREND_WINDOWS = (7, 28, 60, 90)
MINIMUM_DAYS = {7: 4, 28: 14, 60: 30, 90: 45}
TREND_METRICS = {
    "sleep_duration", "resting_hr", "sleep_rhr", "hrv_rmssd", "hrv_sdnn", "sleep_hrv",
    "training_load", "training_duration", "steps", "active_minutes", "stress",
    "respiratory_rate", "spo2", "weight", "vo2max", "pai_daily", "pai_low_zone",
    "pai_medium_zone", "pai_high_zone", "lactate_threshold_hr", "lactate_threshold_pace",
}
METRIC_LABELS = {
    "sleep_duration": "睡眠时长", "resting_hr": "静息心率", "sleep_rhr": "睡眠静息心率",
    "hrv_rmssd": "心率变异性 RMSSD", "hrv_sdnn": "心率变异性 SDNN", "sleep_hrv": "睡眠心率变异性",
    "training_load": "训练负荷", "training_duration": "训练时长", "steps": "步数",
    "active_minutes": "活动分钟", "stress": "压力", "respiratory_rate": "呼吸率",
    "spo2": "血氧饱和度", "weight": "体重", "vo2max": "最大摄氧量",
    "pai_daily": "PAI 日得分", "pai_low_zone": "PAI 低强度", "pai_medium_zone": "PAI 中强度",
    "pai_high_zone": "PAI 高强度", "lactate_threshold_hr": "乳酸阈心率",
    "lactate_threshold_pace": "乳酸阈值配速", "running_pace": "跑步配速",
    "running_heart_rate": "跑步心率", "running_cardiac_drift": "跑步心率漂移",
    "strength_sets": "力量组数", "strength_repetitions": "力量次数", "strength_volume": "力量训练量",
    "training_status": "厂商训练状态",
}
TREND_LABELS = {
    TrendDirection.RISING: "上升", TrendDirection.STABLE: "稳定",
    TrendDirection.FALLING: "下降", TrendDirection.INSUFFICIENT_DATA: "数据不足",
}
EXPECTED_UNITS = {
    "sleep_duration": {"min"}, "resting_hr": {"bpm"}, "sleep_rhr": {"bpm"},
    "hrv_rmssd": {"ms"}, "hrv_sdnn": {"ms"}, "sleep_hrv": {"ms"},
    "training_load": {"load"}, "training_duration": {"min"}, "steps": {"steps"},
    "active_minutes": {"min"}, "stress": {"score"}, "respiratory_rate": {"brpm"},
    "spo2": {"%"}, "weight": {"kg", "lb"}, "vo2max": {"ml/kg/min"},
    "pai_daily": {"pai"}, "pai_low_zone": {"pai"}, "pai_medium_zone": {"pai"},
    "pai_high_zone": {"pai"}, "lactate_threshold_hr": {"bpm"},
    "lactate_threshold_pace": {"s/km"},
}
CUMULATIVE_METRICS = {"steps", "active_minutes", "training_load", "training_duration", "pai_daily", "pai_low_zone", "pai_medium_zone", "pai_high_zone"}
SLEEP_METRICS = {"sleep_duration", "sleep_hrv", "sleep_rhr"}
ACTIVITY_METRICS = {"steps", "active_minutes", "training_load", "training_duration", "pai_daily", "pai_low_zone", "pai_medium_zone", "pai_high_zone"}


class TrendEngine:
    # No parser supplies an observed vendor training-status enum yet. The
    # TrainingRecord default is not an observation and must never enter trends.
    unavailable_sources = {
        "training_status": {"status": "UNKNOWN", "source": "unavailable", "reason": "vendor_status_not_observed"},
    }

    def calculate(
        self,
        raw: RawDailyProfile,
        windows: tuple[int, ...] = TREND_WINDOWS,
    ) -> list[TrendFeature]:
        output: list[TrendFeature] = []
        target_day_complete = bool(
            (getattr(raw, "report_context", None) or {}).get("target_day_complete", True)
        )
        for metric in sorted(TREND_METRICS & raw.series.keys()):
            for identity, daily in sorted(
                _stream_daily_records(raw.series.get(metric, []), metric=metric, as_of=raw.as_of, target_day=raw.day).items(),
                key=lambda item: tuple(value or "" for value in item[0]),
            ):
                for window in windows:
                    if window not in MINIMUM_DAYS:
                        continue
                    output.append(self._trend(
                        metric, identity, daily, raw.day, window,
                        target_day_complete=target_day_complete, as_of=raw.as_of,
                    ))
        output.extend(self._workout_trends(raw, windows))
        return output

    @classmethod
    def _workout_trends(cls, raw: RawDailyProfile, windows: tuple[int, ...]) -> list[TrendFeature]:
        records = RunningAnalyzer().trend_points(raw) + StrengthAnalyzer().trend_points(raw)
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for item in records:
            identity = (
                item.get("source", "unknown"), item.get("source_scope", "unknown"),
                item.get("device_id"), item.get("unit"), item.get("exercise_id"),
                item.get("weight_basis"), item["metric"], item.get("qualification"),
            )
            grouped[identity].append(item)
        output = []
        for identity, items in sorted(grouped.items(), key=lambda item: tuple(str(value or "") for value in item[0])):
            metric = identity[-2]
            for window in windows:
                if window not in MINIMUM_DAYS:
                    continue
                output.append(cls._trend_records(raw, metric, identity, items, window))
        return output

    @staticmethod
    def _trend(
        metric: str,
        identity: tuple[str, str, str | None, str],
        daily: dict[date, tuple[float, object]],
        target_day: date,
        window_days: int,
        *,
        target_day_complete: bool = True,
        as_of: datetime | None = None,
    ) -> TrendFeature:
        filtered = {
            day: value for day, value in daily.items()
            if target_day_complete or metric not in CUMULATIVE_METRICS or day < target_day
        }
        return TrendEngine._trend_daily_records(
            metric, identity, filtered, target_day, window_days, as_of=as_of,
        )

    @staticmethod
    def _trend_daily_records(
        metric: str,
        identity: tuple[str, str, str | None, str],
        daily: dict[date, tuple[float, object]],
        target_day: date,
        window_days: int,
        *,
        as_of: datetime | None = None,
    ) -> TrendFeature:
        source, scope, device_id, unit = identity
        current_start = target_day - timedelta(days=window_days - 1)
        previous_start = current_start - timedelta(days=window_days)
        current = sorted((day, item) for day, item in daily.items() if current_start <= day <= target_day)
        previous = sorted((day, item) for day, item in daily.items() if previous_start <= day < current_start)
        return TrendEngine._make_trend(
            metric, METRIC_LABELS.get(metric, metric), source, scope, device_id, unit,
            current, previous, target_day, window_days, minimum=MINIMUM_DAYS[window_days],
            sample_unit="day", calendar_semantics=_metric_semantics(metric), as_of=as_of,
        )

    @staticmethod
    def _trend_records(raw, metric: str, identity: tuple, records: list[dict], window_days: int) -> TrendFeature:
        source, scope, device_id, unit, exercise_id, weight_basis, _, _qualification = identity
        target_day = raw.day
        current_start = target_day - timedelta(days=window_days - 1)
        previous_start = current_start - timedelta(days=window_days)
        grouped = defaultdict(list)
        for item in records:
            grouped[item["day"]].append(item)
        # Multiple qualified sessions on one date remain multiple samples for
        # coverage, but the day's displayed value is their median.
        current = []
        previous = []
        for day, items in sorted(grouped.items()):
            values = [float(item["value"]) for item in items if _finite(item.get("value"))]
            if not values:
                continue
            record = (float(median(values)), max((item.get("observed_at") for item in items), key=_time_key))
            if current_start <= day <= target_day:
                current.append((day, record))
            elif previous_start <= day < current_start:
                previous.append((day, record))
        current_records = [item for item in records if current_start <= item["day"] <= target_day]
        previous_records = [item for item in records if previous_start <= item["day"] < current_start]
        return TrendEngine._make_trend(
            metric, METRIC_LABELS.get(metric, metric), source, scope, device_id, unit,
            current, previous, target_day, window_days, minimum={7: 3, 28: 3, 60: 4, 90: 6}[window_days],
            sample_unit="session", calendar_semantics="activity_day", as_of=raw.as_of,
            current_samples=sum(_finite(item.get("value")) for item in current_records),
            previous_samples=sum(_finite(item.get("value")) for item in previous_records),
            current_expected=len(current_records), previous_expected=len(previous_records),
            exercise_id=exercise_id, weight_basis=weight_basis,
            qualification="; ".join(sorted({str(item.get("qualification", "")) for item in records if item.get("qualification")})),
        )

    @staticmethod
    def _make_trend(
        metric: str, metric_label: str, source: str, source_scope: str, device_id: str | None,
        unit: str, current: list[tuple[date, tuple[float, object]]], previous: list[tuple[date, tuple[float, object]]],
        target_day: date, window_days: int, *, minimum: int, sample_unit: str,
        calendar_semantics: str, as_of: datetime | None,
        current_samples: int | None = None, previous_samples: int | None = None,
        current_expected: int | None = None, previous_expected: int | None = None,
        exercise_id: str | None = None, weight_basis: str | None = None,
        qualification: str | None = None,
    ) -> TrendFeature:
        current_values = [value for _, (value, _) in current]
        previous_values = [value for _, (value, _) in previous]
        current_start = target_day - timedelta(days=window_days - 1)
        previous_start = current_start - timedelta(days=window_days)
        current_median = float(median(current_values)) if current_values else None
        previous_median = float(median(previous_values)) if previous_values else None
        current_expected = current_expected if current_expected is not None else window_days
        previous_expected = previous_expected if previous_expected is not None else window_days
        current_samples = current_samples if current_samples is not None else len(current_values)
        previous_samples = previous_samples if previous_samples is not None else len(previous_values)
        current_coverage = len(current_values) / window_days
        previous_coverage = len(previous_values) / window_days
        sample_coverage = current_samples / current_expected if current_expected else 0
        previous_sample_coverage = previous_samples / previous_expected if previous_expected else 0
        current_available = len(current_values) >= minimum and sample_coverage >= 0.5
        previous_available = len(previous_values) >= minimum and previous_sample_coverage >= 0.5
        comparison_available = current_available and previous_available
        change = _percent_change(current_median, previous_median) if comparison_available else None
        absolute = (
            round(current_median - previous_median, 3)
            if comparison_available and current_median is not None and previous_median is not None else None
        )
        direction = _direction(change, absolute) if comparison_available else TrendDirection.INSUFFICIENT_DATA
        status = Availability.AVAILABLE if current_available else Availability.INSUFFICIENT_DATA
        confidence = _confidence(sample_coverage, previous_sample_coverage, comparison_available)
        if sample_unit == "session" and confidence == ConfidenceBand.HIGH and min(len(current), len(previous)) < 6:
            confidence = ConfidenceBand.MODERATE
        latest_observed = max((observed for _, (_, observed) in current), key=_time_key, default=None)
        return TrendFeature(
            metric=metric, metric_label=metric_label, window_days=window_days,
            source=source, source_scope=source_scope, device_id=device_id, unit=unit,
            status=status, status_label=AVAILABILITY_LABELS[status.value],
            current_distinct_days=len(current), previous_distinct_days=len(previous), minimum_days=minimum,
            coverage_ratio=round(current_coverage, 4), period_start=current_start, period_end=target_day,
            reference_period_start=previous_start, reference_period_end=current_start - timedelta(days=1),
            expected_days=window_days, previous_expected_days=window_days,
            previous_coverage_ratio=round(previous_coverage, 4), comparison_available=comparison_available,
            comparison_basis="preceding_window" if comparison_available else "unavailable",
            current_value=round(current[-1][1][0], 3) if current else None,
            current_median=round(current_median, 3) if current_median is not None else None,
            previous_median=round(previous_median, 3) if previous_median is not None else None,
            change_percent=round(change, 1) if change is not None else None,
            change_absolute=absolute, slope_per_day=round(_linear_trend(current), 4) if len(current) >= 2 else None,
            variability_mad=round(float(median(abs(value - current_median) for value in current_values)), 3) if current_values and current_median is not None else None,
            current_sample_count=current_samples, previous_sample_count=previous_samples,
            current_expected_samples=current_expected, previous_expected_samples=previous_expected,
            sample_coverage_ratio=round(sample_coverage, 4), previous_sample_coverage_ratio=round(previous_sample_coverage, 4),
            sample_unit=sample_unit, calendar_semantics=calendar_semantics, qualification=qualification,
            weight_basis=weight_basis, exercise_id=exercise_id, observed_at=latest_observed, as_of=as_of,
            direction=direction, direction_label=TREND_LABELS[direction], confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
        )


def stream_daily_values(
    points: list[SeriesPoint],
    *,
    metric: str | None = None,
    as_of: datetime | None = None,
    target_day: date | None = None,
) -> dict[tuple[str, str, str | None, str], dict[date, float]]:
    """Group finite, qualified points by source/device/unit and local day."""
    grouped: dict[tuple[str, str, str | None, str], dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        if not _valid_point(point, metric=metric, as_of=as_of, target_day=target_day):
            continue
        grouped[(point.source, point.source_scope, point.device_id, point.unit)][point.day].append(float(point.value))
    return {
        stream: {day: float(median(values)) for day, values in by_day.items()}
        for stream, by_day in grouped.items()
    }


def _stream_daily_records(points: list[SeriesPoint], *, metric: str, as_of: datetime | None, target_day: date | None):
    grouped: dict[tuple, dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        if _valid_point(point, metric=metric, as_of=as_of, target_day=target_day):
            grouped[(point.source, point.source_scope, point.device_id, point.unit)][point.day].append(point)
    return {
        stream: {
            day: (
                float(median(item.value for item in values)),
                max((item.observed_at for item in values), key=_time_key),
            )
            for day, values in by_day.items()
        }
        for stream, by_day in grouped.items()
    }


def _valid_point(point: SeriesPoint, *, metric: str | None, as_of: datetime | None, target_day: date | None) -> bool:
    if type(point.day) is not date or (target_day is not None and point.day > target_day):
        return False
    if not _finite(point.value) or not str(point.source or "").strip() or point.source == "unknown":
        return False
    if not str(point.source_scope or "").strip() or point.source_scope == "unknown":
        return False
    if point.source_scope == "device" and not point.device_id:
        return False
    if not str(point.unit or "").strip() or point.unit == "unknown":
        return False
    selected_metric = metric or point.metric
    if metric is not None and point.metric != metric:
        return False
    if point.unit not in EXPECTED_UNITS.get(selected_metric, {point.unit}):
        return False
    if selected_metric in {"hrv_rmssd", "hrv_sdnn", "sleep_hrv", "resting_hr", "sleep_rhr", "vo2max", "lactate_threshold_hr", "lactate_threshold_pace"} and point.value <= 0:
        return False
    if selected_metric in {"sleep_duration", "steps", "active_minutes", "training_load", "training_duration", "pai_daily", "pai_low_zone", "pai_medium_zone", "pai_high_zone"} and point.value < 0:
        return False
    if as_of is not None and not _observed_before(point.observed_at, as_of):
        return False
    return isinstance(point.observed_at, (datetime, date))


def _observed_before(observed_at, as_of: datetime) -> bool:
    if isinstance(observed_at, datetime):
        candidate = observed_at
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=as_of.tzinfo)
        cutoff = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=candidate.tzinfo)
        return candidate <= cutoff
    return type(observed_at) is date and observed_at <= as_of.date()


def _metric_semantics(metric: str) -> str:
    if metric in SLEEP_METRICS:
        return "sleep_day"
    if metric in ACTIVITY_METRICS:
        return "activity_day"
    return "calendar_day"


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and isfinite(float(value))


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * 100


def _direction(change: float | None, absolute: float | None) -> TrendDirection:
    if change is not None:
        if change > 5:
            return TrendDirection.RISING
        if change < -5:
            return TrendDirection.FALLING
        return TrendDirection.STABLE
    if absolute is None or abs(absolute) <= 0:
        return TrendDirection.STABLE
    return TrendDirection.RISING if absolute > 0 else TrendDirection.FALLING


def _confidence(current_coverage: float, previous_coverage: float, comparison_available: bool) -> ConfidenceBand:
    if not comparison_available:
        return ConfidenceBand.NONE
    minimum = min(current_coverage, previous_coverage)
    if minimum >= 0.8:
        return ConfidenceBand.HIGH
    if minimum >= 0.6:
        return ConfidenceBand.MODERATE
    return ConfidenceBand.LOW


def _time_key(value) -> tuple:
    if isinstance(value, datetime):
        return (1, value)
    if type(value) is date:
        return (0, datetime.combine(value, datetime.min.time()))
    return (-1, datetime.min)


def _linear_trend(points: list[tuple[date, tuple[float, object]]]) -> float:
    if len(points) < 2:
        return 0.0
    origin = points[0][0]
    xs = [(day - origin).days for day, _ in points]
    ys = [value for _, (value, _) in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
