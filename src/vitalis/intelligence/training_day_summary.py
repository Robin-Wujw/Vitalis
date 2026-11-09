"""Recorded daily training totals, computed once during analysis."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from math import isfinite

from .activity import workout_distance_km
from .contracts import RecordedTrainingTotal, TrainingDaySummary, TrainingGroupSummary
from .profile import RawDailyProfile
from .strength import reported_set_count


def _duration(data: dict) -> float | None:
    value = data.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    # Normalizers may have defaulted an unobserved duration to zero.
    fields = data.get("observed_fields") or []
    observed = bool(fields.get("duration")) if isinstance(fields, dict) else "duration" in fields
    if value == 0 and not observed and data.get("duration_observed") is not True:
        return None
    return float(value)


def _total(values: list[float | int | None], unit: str) -> RecordedTrainingTotal:
    known = [value for value in values if value is not None and isfinite(value)]
    complete = len(known) == len(values)
    return RecordedTrainingTotal(
        value=round(sum(known), 6) if complete else None,
        unit=unit, sample_count=len(known), total_count=len(values),
        status="AVAILABLE" if complete else "PARTIAL" if known else "UNKNOWN",
    )


def training_day_summaries(raw: RawDailyProfile) -> list[TrainingDaySummary]:
    days = defaultdict(list)
    seen = set()
    for workout in raw.workouts:
        day = workout.get("local_day")
        if day is None or not raw.day - timedelta(days=6) <= day <= raw.day:
            continue
        key = (workout.get("source"), workout.get("workout_id"))
        if all(key):
            if key in seen:
                continue
            seen.add(key)
        days[day].append(workout)
    output = []
    for day, workouts in sorted(days.items(), reverse=True):
        grouped = defaultdict(list)
        labels = {}
        for workout in workouts:
            data = workout.get("data") or {}
            kind = str(data.get("type") or "unknown").lower()
            if kind in {"strength", "strength_training", "weight_training", "functional_strength"}:
                kind, label = "strength", "力量"
            elif kind == "running":
                label = "跑步"
            else:
                kind = "other:" + str(data.get("sport_mode") or kind)
                label = str(data.get("sport_mode_label") or "其他运动")
            grouped[kind].append(workout)
            labels[kind] = label
        groups = []
        for kind in sorted(grouped, key=lambda key: (key not in {"running", "strength"}, key)):
            records = sorted(grouped[kind], key=lambda row: (str(row.get("started_at") or ""), str(row.get("source") or ""), str(row.get("workout_id") or "")))
            data = [row.get("data") or {} for row in records]
            groups.append(TrainingGroupSummary(
                kind=kind, label=labels[kind], session_count=len(records),
                duration=_total([_duration(item) for item in data], "min"),
                distance=_total([workout_distance_km(item) for item in data], "km") if kind == "running" else None,
                sets=_total([reported_set_count(item) for item in data], "sets") if kind == "strength" else None,
                records=[{
                    "source": row.get("source"), "workout_id": row.get("workout_id"),
                    "source_scope": "workout_summary", "observed_at": row.get("started_at"),
                    "fetched_at": row.get("fetched_at") or (row.get("data") or {}).get("fetched_at"),
                } for row in records],
            ))
        output.append(TrainingDaySummary(
            date=day, timezone=raw.timezone_name, as_of=raw.as_of,
            session_count=len(workouts), duration=_total([_duration(row.get("data") or {}) for row in workouts], "min"),
            groups=groups,
        ))
    return output
