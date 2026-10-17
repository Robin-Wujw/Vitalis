"""Compare saved training facts with the prior evening's immutable delivery input."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any


_MEASURED_FIELDS = (
    "duration_minutes", "vendor_load", "distance_km", "calories_kcal",
    "heart_rate_avg_bpm", "heart_rate_max_bpm", "vendor_reported_sets", "detail_available",
)


def compare_evening_training(
    *, user_id: str, day: date, as_of: datetime,
    workouts: list[dict[str, Any]], previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Report only added/changed observations; missing records never imply rest."""
    yesterday = (day - timedelta(days=1)).isoformat()
    if (not previous or previous.get("user_id") != user_id
            or previous.get("date") != yesterday
            or not previous.get("analysis_run_id")
            or not isinstance(previous.get("workouts"), list)):
        return None
    try:
        prior_as_of = datetime.fromisoformat(str(previous.get("as_of")).replace("Z", "+00:00"))
        if prior_as_of.tzinfo is None or prior_as_of > as_of:
            return None
    except (ValueError, TypeError):
        return None

    def records(items):
        return {
            (item["source"], item["workout_id"]): item
            for item in items
            if isinstance(item, dict) and item.get("date") == yesterday
            and item.get("source") and item.get("workout_id")
        }

    before, current = records(previous["workouts"]), records(workouts)
    changes = []
    for key, item in sorted(current.items()):
        old = before.get(key)
        if old is not None and all(old.get(field) == item.get(field) for field in _MEASURED_FIELDS):
            continue
        changes.append({
            "change": "added" if old is None else "updated",
            "workout": deepcopy(item), "previous": deepcopy(old),
        })
    if not changes:
        return None
    return {
        "date": yesterday, "calendar_semantics": "activity_day",
        "previous_analysis_run_id": previous["analysis_run_id"],
        "previous_as_of": prior_as_of.isoformat(), "as_of": as_of.isoformat(),
        "previous_sample_count": len(before), "sample_count": len(current),
        "coverage": "recorded_workouts_only", "items": changes,
    }
