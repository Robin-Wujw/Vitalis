"""Synthetic workout observations for the new-database CLI demonstration."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.zepp.client import SPORTS
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import PLAN_VERSION, stable_chunk_key
from vitalis.domain import Workout, WorkoutRpe
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
                    session_rpe=WorkoutRpe(
                        value=7, source="zepp", source_scope="workout_summary", source_field="rpe",
                        observed_at=day_start + timedelta(hours=18),
                    ),
                    observed_fields=["calories", "duration", "load", "vendor_reported_sets", "session_rpe"],
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
        _seed_chart_observations(repository, start, end, as_of, timezone_name)
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


def _seed_chart_observations(repository, start: date, end: date, as_of: datetime, timezone_name: str) -> None:
    """Invented curves with deliberate gaps; used only in the isolated demo DB."""
    from math import sin
    from vitalis.domain import DailyMetric, MetricSample
    from vitalis.adapters.persistence import models as orm
    from sqlalchemy import select

    metrics = []
    current = start
    while current <= end:
        offset = (current-start).days
        for metric, value, unit in (
            ("vendor_load_7d", 245+offset%12*12, "load"),
            ("vendor_load_optimal_min", 230, "load"),
            ("vendor_load_optimal_max", 380, "load"),
            ("vendor_load_overreaching", 470, "load"),
            ("sleep_hrv", 52+8*sin(offset/4), "ms"),
            ("sleep_rhr", 58+3*sin(offset/5), "bpm"),
            ("skin_temp_delta", 0.2*sin(offset/6), "C"),
            ("respiratory_rate", 15+sin(offset/5), "brpm"),
        ):
            metrics.append(DailyMetric(user_id="demo",date=current,metric=metric,value=round(value,2),unit=unit,
                source_scope="synthetic_chart",qualification="synthetic",fetched_at=as_of-timedelta(minutes=5)))
        current += timedelta(days=1)
    repository.save_daily_metrics(metrics)
    midnight, _ = local_day_utc_bounds(end, timezone_name)
    samples = []
    last_minute = int((as_of-midnight).total_seconds()/60)
    for minute in range(last_minute):
        if 10*60 <= minute < 10*60+40:
            continue
        stamp = midnight+timedelta(minutes=minute)
        base = 57 if minute<7*60 else 73
        value = base+6*sin(minute/18)+(38 if 18*60<=minute<18*60+45 else 0)
        samples.append(MetricSample(user_id="demo",metric="heart_rate",timestamp=stamp,value=round(value),unit="bpm",
            source_scope="device",device_id="synthetic-chart-device",fetched_at=as_of-timedelta(minutes=2),qualification="synthetic"))
        if minute%6==0:
            samples.append(MetricSample(user_id="demo",metric="stress",timestamp=stamp,
                value=round(25+10*sin(minute/40)+(30 if 14*60<=minute<16*60 else 0)),unit="score",
                source_scope="all_day_stress",qualification="synthetic",fetched_at=as_of-timedelta(minutes=2)))
        if minute%5==0:
            for metric, charge in (("hybrid_charge",75-minute/24), ("physical_charge",82-minute/20), ("mental_charge",72-minute/35)):
                if metric=="hybrid_charge" and 12*60<=minute<12*60+20:
                    continue
                samples.append(MetricSample(user_id="demo",metric=metric,timestamp=stamp,value=round(max(0,charge),1),unit="score",
                    source_scope="device",device_id="synthetic-chart-device",qualification="vendor_reference",fetched_at=as_of-timedelta(minutes=2)))
    repository.save_metric_samples(samples)
    sleep = repository.db.execute(select(orm.SleepRecord).where(orm.SleepRecord.user_id=="demo",orm.SleepRecord.date==end)).scalar_one_or_none()
    if sleep is not None:
        payload = dict(sleep.data)
        stage_start = midnight-timedelta(minutes=50)
        stages = []
        elapsed = 0
        for stage,minutes in (("light",35),("deep",80),("light",90),("rem",45),("awake",15),("light",100),("rem",70),("light",35)):
            stages.append({"stage":stage,"start_time":(stage_start+timedelta(minutes=elapsed)).isoformat(),
                "end_time":(stage_start+timedelta(minutes=elapsed+minutes)).isoformat()})
            elapsed += minutes
        payload["stages"] = stages
        sleep.data = payload
    nap_start = midnight+timedelta(hours=13)
    repository.save_naps("demo",end,[{"started_at":nap_start.isoformat(),"ended_at":(nap_start+timedelta(minutes=25)).isoformat(),
        "sleep_minutes":25,"awake_minutes":0,"source":"zepp","stages":[{"stage":"light","start_time":nap_start.isoformat(),
            "end_time":(nap_start+timedelta(minutes=25)).isoformat()}]}],fetched_at=as_of-timedelta(minutes=5))
