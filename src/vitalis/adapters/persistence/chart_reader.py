"""Bounded source-isolated reads for interactive charts; never starts tasks."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from itertools import islice
from types import SimpleNamespace

from vitalis.intelligence.charts import CHARGE, INTRADAY
from vitalis.intelligence.profile import ProfileLoader, RawDailyProfile, SeriesPoint, _records_by_day
from vitalis.time import local_day, local_day_utc_bounds

from .database import SessionLocal
from .repositories import HealthRepository


class SqlChartReader:
    MAX_SAMPLES = 250_000

    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def load(self, user_id: str, start: date, end: date, *, as_of: datetime,
             timezone_name: str, intraday: bool) -> RawDailyProfile:
        with self.session_factory() as db:
            repo = HealthRepository(db)
            raw = RawDailyProfile(user_id=user_id, day=end, as_of=as_of, timezone_name=timezone_name)
            raw.sleep_by_day = _records_by_day(repo.sleep_range(user_id, start, end))
            raw.activity_by_day = _records_by_day(repo.activity_range(user_id, start, end))
            raw.training_by_day = _records_by_day(repo.training_range(user_id, start, end))
            raw.chart_naps = repo.nap_range(user_id, start, end, as_of)
            loader = ProfileLoader(repo)
            loader._add_record_series(raw)
            loader._add_daily_metrics(raw, start)
            loader._add_sample_metrics(raw, start)
            left, _ = local_day_utc_bounds(start, timezone_name)
            _, right = local_day_utc_bounds(end, timezone_name)
            cutoff = min(right-timedelta(microseconds=1), as_of)
            if intraday and cutoff >= left:
                for metric in (*INTRADAY, *CHARGE):
                    if metric == "heart_rate":
                        raw.chart_samples[metric] = [SeriesPoint(metric, float(value), unit,
                            local_day(stamp, timezone_name), stamp, source, scope, device)
                            for stamp,value,source,scope,device,unit in repo.heart_rate_minute_medians(user_id,left,cutoff)]
                    else:
                        rows = list(islice(repo.metric_sample_rows(user_id,metric,left,cutoff), self.MAX_SAMPLES+1))
                        if len(rows) > self.MAX_SAMPLES:
                            raise ValueError("chart sample budget exceeded; shorten the window")
                        raw.chart_samples[metric] = [SeriesPoint(metric,row.value,row.unit,
                            local_day(row.timestamp,timezone_name),row.timestamp.replace(tzinfo=timezone.utc),
                            row.source,row.source_scope,row.device_id or None,
                            source_field=row.source_field, fetched_at=row.fetched_at, qualification=row.qualification) for row in rows]
            workouts = repo.workouts(user_id,start,end,501,timezone_name=timezone_name)
            if len(workouts)>500:
                raise ValueError("chart workout budget exceeded; shorten the window")
            exercises = repo.strength_exercises_for_workout_keys(user_id,[(r.source,r.workout_id) for r in workouts])
            keys = [(r.source,r.workout_id) for r in workouts[:12]] if intraday else []
            samples = repo.workout_metric_samples_for_workouts(user_id,keys,limit=self.MAX_SAMPLES+1) if keys else []
            if len(samples)>self.MAX_SAMPLES:
                raise ValueError("workout chart sample budget exceeded; shorten the window")
            by_workout = {}
            for s in samples:
                # Detach all fields used by the chart builder before closing SQL.
                by_workout.setdefault((s.source,s.workout_id),[]).append(SimpleNamespace(
                    timestamp=s.timestamp, metric=s.metric, value=s.value, unit=s.unit,
                    source_scope=s.source_scope, device_id=s.device_id))
            raw.workouts = [{"workout_id":r.workout_id,"source":r.source,"started_at":r.started_at,
                "local_day":local_day(r.started_at,timezone_name),"data":dict(r.data or {}),
                "detail_available":r.detail_synced,"detail":dict(r.detail or {}),
                "samples":by_workout.get((r.source,r.workout_id),[]),
                "confirmed_exercises":exercises.get((r.source,r.workout_id),[])} for r in workouts
                if r.started_at and r.started_at.replace(tzinfo=timezone.utc)<=as_of]
            snapshot = getattr(repo,"chart_input_snapshot",None)
            if callable(snapshot):
                raw.chart_inputs = snapshot(user_id,start,end,as_of=as_of,timezone_name=timezone_name)
            return raw
