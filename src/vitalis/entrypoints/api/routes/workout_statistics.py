"""Source-qualified workout analytics and opt-in detail sections."""
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from vitalis.entrypoints.api.deps import require_user_id
from vitalis.time import local_today

router = APIRouter(tags=["workouts"])


def _service():
    from vitalis.bootstrap import get_workout_statistics
    return get_workout_statistics()


@router.get("/workouts/catalog", operation_id="get_workout_catalog")
def catalog(user_id: str = Depends(require_user_id)):
    return _service().catalog()


@router.get("/workouts/statistics", operation_id="get_workout_statistics")
def statistics(
    from_date: date | None = Query(None, alias="from"),
    to_date: date | None = Query(None, alias="to"),
    group_by: Literal["week", "month", "year", "sport", "family", "sport_family", "none"] = "month",
    metric: Literal["distance", "moving_time", "pace", "swim_pace", "avg_heart_rate", "max_heart_rate",
        "cadence", "exercise_load", "aerobic_training_effect", "vo2_max", "calories", "swolf", "strokes",
        "distance_per_stroke", "stride_length", "ground_contact_time", "vertical_oscillation", "power",
        "elevation_gain", "total_reps"] | None = None,
    sport: str | None = Query(None, max_length=128),
    family: str | None = Query(None, max_length=64),
    workout_type: str | None = Query(None, alias="type", max_length=64),
    source: str | None = Query(None, max_length=32),
    user_id: str = Depends(require_user_id),
):
    from vitalis.config import settings
    end = to_date or local_today()
    start = from_date or end - timedelta(days=89)
    if start > end:
        raise HTTPException(status_code=422, detail="from must not be after to")
    return _service().statistics(user_id, start=start, end=end, timezone_name=settings.timezone,
        group_by=group_by, metric=metric, sport=sport, family=family, workout_type=workout_type, source=source)


@router.get("/workouts/{workout_id}/sections", operation_id="get_workout_sections")
def sections(
    workout_id: str,
    source: str = Query(..., min_length=1, max_length=32),
    include: list[Literal["summary", "laps", "splits", "streams", "gps"]] | None = Query(None),
    user_id: str = Depends(require_user_id),
):
    result = _service().detail(user_id, workout_id, source, sections=include)
    if result is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    return result
