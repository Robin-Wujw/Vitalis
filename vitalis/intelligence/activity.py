"""Activity features over explicitly observed, source-isolated measurements."""

from collections import defaultdict
from datetime import date, datetime
from statistics import median

from vitalis.time import local_day

from .baseline import BaselineEngine
from .contracts import (
    ActivityFeatures,
    ActivityMetric,
    Availability,
    BaselineStats,
    EnergyObservation,
    MeasurementFact,
    Provenance,
    SampleWindowSummary,
)
from .profile import RawDailyProfile, SeriesPoint


_SCOPE_PRIORITY = {
    "user_fused": 4,
    "device": 3,
    "normalized_daily_record": 2,
    "workout_summary": 2,
    "unknown": 1,
}


def stream_priority(stream_key_tuple: tuple) -> tuple:
    """Return a deterministic priority for a complete source stream key.

    Keys may be ``(source, scope, device_id)`` or
    ``(source, scope, device_id, unit)``.  Unit remains part of the tie-breaker;
    streams with different units are never merged by this helper.
    """
    source = str(stream_key_tuple[0] or "unknown")
    scope = str(stream_key_tuple[1] or "unknown")
    device_id = stream_key_tuple[2] or None
    unit = str(stream_key_tuple[3] or "") if len(stream_key_tuple) > 3 else ""
    return (
        _SCOPE_PRIORITY.get(scope, 0),
        int(source == "zepp"),
        int(device_id is None),
        source,
        scope,
        str(device_id or ""),
        unit,
    )


def workout_calories_kcal(data: object) -> float | None:
    """Read the normalized workout ``calories`` field in kcal.

    Positive legacy values remain usable.  Zero is usable only when the
    normalized record carries explicit observation evidence.
    """
    return _observed_normalized_value(data, "calories")


def workout_distance_km(data: object) -> float | None:
    """Read the normalized workout ``distance_km`` field, without unit guessing."""
    return _observed_normalized_value(data, "distance_km")


def _observed_normalized_value(data: object, field: str) -> float | None:
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    observed_fields = data.get("observed_fields")
    explicitly_observed = (
        field in observed_fields
        if isinstance(observed_fields, (list, tuple, set))
        else bool(observed_fields.get(field))
        if isinstance(observed_fields, dict)
        else False
    ) or data.get(f"{field}_observed") is True
    if value == 0 and not explicitly_observed:
        return None
    return float(value)


class ActivityAnalyzer:
    """Build daily activity facts without inventing totals or health thresholds."""

    METRICS = (
        ("steps", "steps"),
        ("distance_km", "km"),
        ("active_minutes", "min"),
    )

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> ActivityFeatures:
        features: dict[str, ActivityMetric | None] = {
            metric: self._metric(raw, baselines, metric, unit)
            for metric, unit in self.METRICS
        }
        energy = self._energy_for_day(raw)
        heart_rate = raw.sample_window_summaries.get("heart_rate")
        stress = raw.sample_window_summaries.get("stress")
        stress_summary = self._stress_summary(raw)
        if stress is not None and stress_summary and stress.provenance != stress_summary[0].provenance:
            stress = stress.model_copy(update={"limitations": [
                *stress.limitations,
                "压力日摘要与采样记录来源不同，采样覆盖不能代表该日摘要的覆盖。",
            ]})
        available = any(features.values()) or bool(energy or stress_summary) or heart_rate is not None or stress is not None
        limitations: list[str] = []
        if not any(features.values()):
            limitations.append("target_day_activity_missing")
        if any(features.values()) and not _target_day_complete(raw):
            if any(
                self._has_available_baseline(raw, baselines, metric, unit)
                for metric, unit in self.METRICS
            ):
                limitations.append("target_day_incomplete_baseline_comparison_skipped")
        return ActivityFeatures(
            status=Availability.AVAILABLE if available else Availability.INSUFFICIENT_DATA,
            steps=features["steps"],
            distance_km=features["distance_km"],
            active_minutes=features["active_minutes"],
            energy=energy,
            heart_rate=heart_rate,
            stress=stress,
            stress_summary=stress_summary,
            limitations=limitations,
        )

    @staticmethod
    def _stress_summary(raw: RawDailyProfile) -> list[MeasurementFact]:
        names = ("stress", "stress_min", "stress_max", "stress_relaxed_pct",
                 "stress_normal_pct", "stress_medium_pct", "stress_high_pct")
        streams = defaultdict(dict)
        for metric in names:
            for fact in raw.facts.get(metric, []):
                if fact.unit != ("%" if metric.endswith("_pct") else "score"):
                    continue
                source = fact.provenance
                streams[(source.source, source.source_scope, source.device_id)][metric] = fact
        if not streams:
            return []
        _, selected = max(streams.items(), key=lambda item: (
            int("stress" in item[1]), stream_priority(item[0]), len(item[1]),
        ))
        return [selected[name] for name in names if name in selected]

    def _metric(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
        metric: str,
        unit: str,
    ) -> ActivityMetric | None:
        points = [
            point for point in raw.series.get(metric, [])
            if point.day == raw.day
            and point.unit == unit
            and isinstance(point.value, (int, float))
            and not isinstance(point.value, bool)
            and point.value >= 0
        ]
        if not points:
            return None
        streams: dict[tuple[str, str, str | None, str], list[SeriesPoint]] = defaultdict(list)
        for point in points:
            streams[(point.source, point.source_scope, point.device_id, point.unit)].append(point)
        complete_day = _target_day_complete(raw)
        candidates = []
        for stream, stream_points in streams.items():
            baseline = _baseline_for(baselines, metric, stream)
            candidates.append((
                int(complete_day and baseline is not None and baseline.status == Availability.AVAILABLE),
                baseline.distinct_days if baseline and complete_day else 0,
                stream_priority(stream),
                stream,
                stream_points,
                baseline,
            ))
        _, _, _, stream, stream_points, baseline = max(candidates, key=lambda item: item[:3])
        value = float(median(point.value for point in stream_points))
        latest = max(stream_points, key=lambda point: _observed_key(point.observed_at))
        deviation = (
            BaselineEngine.deviation(value, baseline)
            if complete_day and baseline is not None
            else None
        )
        return ActivityMetric(
            metric=metric,
            value=round(value, 3),
            unit=stream[3],
            observed_at=latest.observed_at,
            provenance=Provenance(
                source=stream[0], source_scope=stream[1], device_id=stream[2],
            ),
            baseline_reference=(
                baseline.reference_value
                if complete_day and baseline is not None else None
            ),
            baseline_window_days=(
                baseline.window_days if complete_day and baseline is not None else None
            ),
            baseline_distinct_days=(
                baseline.distinct_days if complete_day and baseline is not None else 0
            ),
            deviation=deviation,
        )

    def _has_available_baseline(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
        metric: str,
        unit: str,
    ) -> bool:
        return any(
            point.day == raw.day
            and point.unit == unit
            and (baseline := _baseline_for(
                baselines,
                metric,
                (point.source, point.source_scope, point.device_id, point.unit),
            )) is not None
            and baseline.status == Availability.AVAILABLE
            for point in raw.series.get(metric, [])
        )

    @staticmethod
    def _energy_for_day(raw: RawDailyProfile) -> list[EnergyObservation]:
        observations = [
            item for item in raw.energy_observations
            if _observation_day(item.observed_at) == raw.day
        ]
        if observations:
            return observations

        by_day: dict[date, dict[tuple, list[SeriesPoint]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for point in raw.series.get("calories", []):
            by_day[point.day][(
                point.source, point.source_scope, point.device_id, point.unit,
            )].append(point)
        fallback: list[EnergyObservation] = []
        streams = by_day.get(raw.day, {})
        if streams:
            stream = max(streams, key=stream_priority)
            points = streams[stream]
            latest = max(points, key=lambda point: _observed_key(point.observed_at))
            fallback.append(EnergyObservation(
                metric="calories",
                value=round(float(median(point.value for point in points)), 3),
                unit=stream[3],
                observed_at=latest.observed_at,
                provenance=Provenance(
                    source=stream[0], source_scope=stream[1], device_id=stream[2],
                ),
                role="unspecified",
                source_field=next(
                    (point.source_field for point in points if point.source_field),
                    "series.calories",
                ),
                estimated=True,
                limitations=["energy_role_unverified"],
            ))
        for workout in raw.workouts:
            if workout.get("local_day") != raw.day:
                continue
            value = workout_calories_kcal(workout.get("data") or {})
            if value is None:
                continue
            started_at = workout.get("started_at")
            fallback.append(EnergyObservation(
                metric="calories",
                value=value,
                unit="kcal",
                observed_at=started_at if isinstance(started_at, datetime) else raw.day,
                provenance=Provenance(
                    source=str(workout.get("source") or "unknown"),
                    source_scope="workout_summary",
                    device_id=None,
                ),
                role="workout",
                source_field="workout.calories",
                estimated=True,
                limitations=["vendor_energy_estimate"],
            ))
        return fallback


def _baseline_for(
    baselines: dict[str, list[BaselineStats]],
    metric: str,
    stream: tuple[str, str, str | None, str],
) -> BaselineStats | None:
    source, scope, device_id, unit = stream
    candidates = [
        item for item in baselines.get(metric, [])
        if item.window_days == 28
        and item.source == source
        and item.source_scope == scope
        and item.device_id == device_id
        and item.unit == unit
    ]
    return candidates[0] if candidates else None


def _observed_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.combine(value, datetime.min.time())


def _observation_day(value: datetime | date) -> date:
    return local_day(value) if isinstance(value, datetime) else value


def _target_day_complete(raw: RawDailyProfile) -> bool:
    context = raw.report_context or {}
    if "target_day_complete" in context:
        return bool(context["target_day_complete"])
    return raw.day < local_day(raw.as_of)
