"""Synthetic workout observations for the new-database CLI demonstration."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.zepp.client import SPORTS
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import PLAN_VERSION, stable_chunk_key
from vitalis.domain import Workout
from vitalis.time import local_day_utc_bounds


def seed_demo_workouts(start: date, end: date, as_of: datetime, timezone_name: str) -> None:
    """Add declared synthetic observations; never contacts a vendor or sends a report."""
    with session_scope() as db:
        repository = HealthRepository(db)
        day = start
        while day <= end:
            if day.weekday() in {2, 6} or day == end:
                day_start, _ = local_day_utc_bounds(day, timezone_name)
                workout_id = f"synthetic-strength-{day.isoformat()}"
                repository.save_workout(Workout(
                    user_id="demo", workout_id=workout_id, type="strength",
                    training_family="strength", sport_mode_label="力量训练",
                    started_at=day_start + timedelta(hours=18), duration=45, load=50,
                    calories=210, heart_rate_avg=108, vendor_reported_sets=12,
                    observed_fields=["calories", "duration", "load", "vendor_reported_sets"],
                ))
                curls = (12, 10, 8) if day == end else (10, 8, 6)
                sets = []
                for name, identity, weight, basis, repetitions in (
                    ("二头肌弯举", "biceps_curl", 10, "per_hand", curls),
                    ("高位下拉", "lat_pulldown", 40, "machine", (12, 12, 10)),
                    ("坐姿划船", "seated_row", 35, "machine", (12, 12, 12)),
                    ("哑铃侧平举", "lateral_raise", 5, "per_hand", (15, 15, 15)),
                ):
                    for repetitions_value in repetitions:
                        sets.append({
                            "order": len(sets) + 1, "exercise_name": name,
                            "exercise_id": identity, "repetitions": repetitions_value,
                            "weight_kg": weight, "weight_value": weight,
                            "weight_unit": "kg", "weight_basis": basis,
                            "source": "strength_sets",
                        })
                repository.save_workout_detail("demo", workout_id, {
                    "schema_version": "4.0", "strength_sets": sets,
                }, fetched_at=as_of - timedelta(minutes=5))
            if day.weekday() == 1:
                day_start, _ = local_day_utc_bounds(day, timezone_name)
                repository.save_workout(Workout(
                    user_id="demo", workout_id=f"synthetic-walk-{day.isoformat()}",
                    type="walking", training_family="aerobic", sport_mode_label="步行",
                    started_at=day_start + timedelta(hours=12, minutes=30), duration=32,
                    load=12, distance_km=2.4, calories=120, heart_rate_avg=96,
                    observed_fields=["duration", "load", "distance_km", "calories"],
                ))
            day += timedelta(days=1)
        repository.rebuild_training_days("demo", {
            start + timedelta(days=offset) for offset in range((end - start).days + 1)
        })
        # This is explicitly a synthetic coverage proof, matching the existing
        # mock source contract. It is confined to the fresh demo database.
        window = FetchWindow.local_dates(start, end)
        manifest = [{
            "stable_key": stable_chunk_key("workouts", sport, window.start, window.end, None),
            "stream": "workouts", "partition": sport, "ordinal": ordinal,
            "window_start": window.start, "window_end": window.end,
            "allow_unavailable": True,
        } for ordinal, sport in enumerate(SPORTS)]
        attempt = repository.create_or_reuse_sync_attempt(
            "demo", plan_version=PLAN_VERSION, window_start=window.start,
            window_end=window.end, manifest=manifest,
        )
        finished = (as_of - timedelta(minutes=1)).replace(tzinfo=None)
        attempt.created_at = finished - timedelta(minutes=1)
        attempt.status = "succeeded"
        attempt.finished_at = finished
        for chunk in repository.sync_chunks(attempt.id):
            chunk.status = "succeeded"
            chunk.fetch_status = "success"
            chunk.parse_status = "success"
            chunk.write_status = "success"
            chunk.finished_at = finished
