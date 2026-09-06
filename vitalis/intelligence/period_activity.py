"""Pure period activity aggregation over provenance-qualified observations."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from math import isfinite
from statistics import median

from vitalis.time import local_day

from .contracts import PeriodActivityMetric, Provenance


ACTIVITY_METRICS = ("steps", "distance_km", "active_minutes")
MIN_COMPARISON_DAYS = {7: 4, 28: 14}


def build_period_activity_metrics(
    raw,
    period_start: date,
    period_days: int,
    previous_start: date | None = None,
) -> list[PeriodActivityMetric]:
    """Build one canonical stream per activity/energy metric family.

    Daily values are reduced within a provenance-qualified stream first.  No
    values are merged across devices, scopes, energy roles, or source fields.
    The target date is incomplete when the loader says it is still live.
    """
    if period_days < 1:
        raise ValueError("period_days must be positive")
    previous_start = previous_start or period_start - timedelta(days=period_days)
    target_day = raw.day
    minimum_days = MIN_COMPARISON_DAYS.get(period_days, max(1, period_days // 2))
    output: list[PeriodActivityMetric] = []

    series = getattr(raw, "series", {}) or {}
    for metric in ACTIVITY_METRICS:
        points = _series_points(series.get(metric, []))
        if not points:
            continue
        selected = _select_stream(points, period_start, target_day, previous_start, period_days)
        if selected is None:
            continue
        stream_key, by_day = selected
        output.append(_metric_from_daily(
            metric=metric,
            unit=stream_key[3],
            provenance=_provenance(stream_key),
            by_day=by_day,
            period_start=period_start,
            period_days=period_days,
            previous_start=previous_start,
            minimum_days=minimum_days,
            target_day=target_day,
            target_day_complete=_target_day_complete(raw),
        ))

    for group in _energy_groups(getattr(raw, "energy_observations", []) or []):
        metric, role, source_field, candidates = group
        selected = _select_stream(
            candidates,
            period_start,
            target_day,
            previous_start,
            period_days,
            sum_daily=role == "workout",
        )
        if selected is None:
            continue
        stream_key, by_day = selected
        output.append(_metric_from_daily(
            metric=metric,
            unit=stream_key[3],
            provenance=_provenance(stream_key),
            by_day=by_day,
            period_start=period_start,
            period_days=period_days,
            previous_start=previous_start,
            minimum_days=minimum_days,
            target_day=target_day,
            target_day_complete=_target_day_complete(raw),
            role=role,
            source_field=source_field,
        ))

    output.sort(key=lambda item: (
        item.metric,
        item.role or "",
        item.source_field or "",
        item.provenance.source,
        item.provenance.source_scope,
        item.provenance.device_id or "",
        item.unit,
    ))
    return output


def _series_points(points) -> list[tuple[date, float, tuple[str, str, str | None, str]]]:
    output = []
    for point in points:
        value = _number(_value(point, "value"))
        day = _value(point, "day")
        if value is None or value < 0 or not isinstance(day, date):
            continue
        stream = (
            str(_value(point, "source") or "unknown"),
            str(_value(point, "source_scope") or "unknown"),
            _value(point, "device_id") or None,
            str(_value(point, "unit") or ""),
        )
        output.append((day, value, stream))
    return output


def _energy_groups(observations):
    grouped = defaultdict(list)
    for observation in observations:
        value = _number(_value(observation, "value"))
        observed_at = _value(observation, "observed_at")
        day = _observation_day(observed_at)
        if value is None or value < 0 or day is None:
            continue
        provenance = _value(observation, "provenance")
        source = _value(provenance, "source") or "unknown"
        scope = _value(provenance, "source_scope") or "unknown"
        device_id = _value(provenance, "device_id") or None
        unit = str(_value(observation, "unit") or "")
        metric = str(_value(observation, "metric") or "energy")
        role = _value(observation, "role")
        source_field = _value(observation, "source_field")
        stream = (str(source), str(scope), device_id, unit)
        grouped[(metric, role, source_field)].append((day, value, stream))
    return [
        (metric, role, source_field, values)
        for (metric, role, source_field), values in grouped.items()
    ]


def _select_stream(points, period_start, target_day, previous_start, period_days, *, sum_daily=False):
    streams = defaultdict(list)
    for day, value, stream in points:
        if previous_start <= day <= target_day:
            streams[stream].append((day, value))
    if not streams:
        return None

    def key(item):
        stream, values = item
        current_days = len({day for day, _ in values if period_start <= day <= target_day})
        previous_days = len({day for day, _ in values if previous_start <= day < period_start})
        try:
            priority = tuple(_stream_priority(stream))
        except (ImportError, TypeError, ValueError):
            priority = ()
        return (
            priority,
            current_days,
            previous_days,
            len(values),
            tuple(value or "" for value in stream),
        )

    stream, values = max(streams.items(), key=key)
    by_day = defaultdict(list)
    for day, value in values:
        by_day[day].append(value)
    # The loader supplies one energy observation per stored workout.
    reduce_daily = sum if sum_daily else median
    return stream, {day: float(reduce_daily(items)) for day, items in by_day.items()}


def _metric_from_daily(
    *,
    metric,
    unit,
    provenance,
    by_day,
    period_start,
    period_days,
    previous_start,
    minimum_days,
    target_day,
    target_day_complete,
    role=None,
    source_field=None,
):
    period_end = period_start + timedelta(days=period_days - 1)
    previous_end = period_start - timedelta(days=1)
    current = {
        day: value for day, value in by_day.items()
        if period_start <= day <= period_end
    }
    previous = {
        day: value for day, value in by_day.items()
        if previous_start <= day <= previous_end
    }
    complete_current = {
        day for day in current
        if day < target_day or target_day_complete
    }
    complete_previous = set(previous)
    current_total = sum(current.values()) if current else None
    previous_total = sum(previous.values()) if previous else None
    current_average = _mean(current.values())
    previous_average = _mean(previous.values())
    comparison_current_values = [
        value for day, value in current.items() if day in complete_current
    ]
    comparison_current_average = _mean(comparison_current_values)
    limitations = []
    if len(current) < period_days:
        limitations.append("周期存在未观测日，总量为已记录下界。")
    if complete_current != set(current):
        limitations.append("周期末端本地日尚未结束，不计为完整日。")
    if (
        len(comparison_current_values) < minimum_days
        or len(previous) < minimum_days
    ):
        limitations.append(f"两期完整有效日不足 {minimum_days} 天，均值变化不比较。")

    average_change = None
    if comparison_current_average is not None and previous_average not in (None, 0):
        if (
            len(comparison_current_values) >= minimum_days
            and len(previous) >= minimum_days
        ):
            average_change = _percent_change(
                comparison_current_average, previous_average
            )
    total_change = None
    totals_are_partial = len(complete_current) < period_days
    if (
        current_total is not None
        and previous_total not in (None, 0)
        and len(complete_current) == period_days
        and len(complete_previous) == period_days
    ):
        total_change = _percent_change(current_total, previous_total)
    return PeriodActivityMetric(
        metric=metric,
        unit=unit,
        provenance=provenance,
        role=role,
        source_field=source_field,
        period_days=period_days,
        available_days=len(current),
        complete_days=len(complete_current),
        previous_available_days=len(previous),
        previous_complete_days=len(complete_previous),
        total=_rounded(current_total),
        average=_rounded(current_average),
        previous_total=_rounded(previous_total),
        previous_average=_rounded(previous_average),
        change_percent=_rounded(average_change),
        total_change_percent=_rounded(total_change),
        totals_are_partial=totals_are_partial,
        limitations=limitations,
    )


def _provenance(stream) -> Provenance:
    return Provenance(
        source=stream[0],
        source_scope=stream[1],
        device_id=stream[2],
    )


def _target_day_complete(raw) -> bool:
    context = getattr(raw, "report_context", None) or {}
    return bool(context.get("target_day_complete", True))


def _stream_priority(stream_key):
    from .activity import stream_priority
    return stream_priority(stream_key)


def _observation_day(value) -> date | None:
    if isinstance(value, datetime):
        return local_day(value)
    return value if isinstance(value, date) else None


def _value(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _number(value) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if isfinite(numeric) else None


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _percent_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return 100 * (current - previous) / abs(previous)


def period_training_details(raw, period_start: date, period_end: date) -> dict:
    """Summarize verified analyzer outputs without inventing intensity."""
    workouts = [
        item for item in (getattr(raw, "workouts", []) or [])
        if isinstance(item.get("local_day"), date)
        and period_start <= item["local_day"] <= period_end
    ]
    limitations: list[str] = []
    running = [
        item for item in workouts
        if str((item.get("data") or {}).get("type") or "").lower() == "running"
    ]
    strength = [
        item for item in workouts
        if str((item.get("data") or {}).get("training_family") or "") == "strength"
    ]

    def duration(item):
        value = (item.get("data") or {}).get("duration")
        try:
            return max(float(value), 0) if value is not None else None
        except (TypeError, ValueError):
            return None

    if running:
        durations = [duration(item) for item in running]
        known_durations = [value for value in durations if value is not None]
        running_duration = sum(known_durations) if known_durations else None
        if len(known_durations) != len(running):
            limitations.append("部分跑步缺少时长，跑步时长为已记录下界。")
        from .activity import workout_distance_km
        distances = [workout_distance_km(item.get("data") or {}) for item in running]
        known_distances = [value for value in distances if value is not None and value >= 0]
        running_distance = sum(known_distances) if known_distances else None
        if len(known_distances) != len(running):
            limitations.append("部分跑步缺少距离，跑步距离为已记录下界。")
    else:
        running_duration = None
        running_distance = None

    classification_counts = {}
    if running:
        from .running import RunningAnalyzer
        running_analyzer = RunningAnalyzer()
        threshold = running_analyzer._lactate_threshold(raw)
        history = running_analyzer._runs(
            raw.workouts, raw.day - timedelta(days=179), raw.day
        )
        historical_durations = []
        analyses = []
        analysis_runs = running_analyzer._runs(
            raw.workouts, min(period_start, period_end - timedelta(days=27)), period_end
        )
        for workout in sorted(analysis_runs, key=running_analyzer._workout_sort_key):
            prior_runs = [
                item for item in history
                if running_analyzer._workout_sort_key(item)
                < running_analyzer._workout_sort_key(workout)
            ]
            session = running_analyzer._session(
                workout, threshold, historical_durations, prior_runs
            )
            if period_start <= session.date <= period_end:
                analyses.append(session)
            historical_durations.append(session.duration_minutes)
        for session in analyses:
            classification = session.classification if session.confidence in {"HIGH", "MODERATE"} else "UNCLASSIFIED"
            classification_counts[classification] = classification_counts.get(classification, 0) + 1

    if strength:
        strength_durations = [duration(item) for item in strength]
        known_strength_durations = [value for value in strength_durations if value is not None]
        strength_duration = sum(known_strength_durations) if known_strength_durations else None
        if len(known_strength_durations) != len(strength):
            limitations.append("部分力量训练缺少时长，力量时长为已记录下界。")
        from .running import RunningAnalyzer
        from .strength import StrengthAnalyzer
        strength_analyzer = StrengthAnalyzer()
        threshold = RunningAnalyzer._lactate_threshold(raw)
        strength_sessions = [
            strength_analyzer._session(raw, workout, threshold)
            for workout in sorted(strength, key=RunningAnalyzer._workout_sort_key)
        ]
        explicit_sessions = [
            session for session in strength_sessions
            if session.explicit_exercises
        ]
        vendor_values = [session.vendor_reported_sets for session in strength_sessions if session.vendor_reported_sets is not None]
        vendor_sets = sum(vendor_values) if vendor_values else None
        vendor_sets_sessions = len(vendor_values)
        explicit_count = len(explicit_sessions)
        set_values = [
            session.total_sets for session in explicit_sessions
            if session.total_sets is not None
        ]
        strength_sets = sum(set_values) if set_values else None
        if len(set_values) != explicit_count:
            limitations.append("部分力量训练缺少明确组数，组数合计仅包括已记录部分。")
    else:
        strength_duration = None
        explicit_count = None
        strength_sets = None
        vendor_sets = None
        vendor_sets_sessions = 0

    if workouts:
        from .activity import workout_calories_kcal
        calories = [workout_calories_kcal(item.get("data") or {}) for item in workouts]
        known_calories = [value for value in calories if value is not None and value >= 0]
        workout_calories = sum(known_calories) if known_calories else None
        calories_sessions = len(known_calories)
        if len(known_calories) != len(workouts):
            limitations.append("部分训练未提供消耗热量，小计仅包括有记录的训练。")
    else:
        workout_calories = None
        calories_sessions = 0

    return {
        "running_sessions": len(running) if running else None,
        "running_distance_km": _rounded(running_distance),
        "running_duration_minutes": _rounded(running_duration),
        "running_classification_counts": dict(sorted(classification_counts.items())),
        "strength_duration_minutes": _rounded(strength_duration),
        "strength_explicit_sessions": explicit_count,
        "strength_sets": strength_sets,
        "vendor_reported_sets": vendor_sets,
        "vendor_sets_sessions": vendor_sets_sessions,
        "workout_calories_kcal": _rounded(workout_calories),
        "workout_calories_sessions": calories_sessions,
        "limitations": list(dict.fromkeys(limitations)),
    }


def _rounded(value):
    return round(float(value), 3) if value is not None else None
