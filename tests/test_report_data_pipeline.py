from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from vitalis.connectors.zepp.client import SPORTS
from vitalis.connectors.zepp.fetcher import FetchWindow
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.service import IntelligenceCommand
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
from vitalis.models import ActivityRecord, DailyMetric, MetricSample, NormalizedDaily, SleepRecord, Workout
from vitalis.services.zepp_sync_coordinator import stable_chunk_key
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage.database import get_engine
from vitalis.time import local_day_utc_bounds


TARGET = date(2026, 8, 28)


def synthetic_pipeline_example(*, morning=False, explicit_strength=True):
    """Generate examples from synthetic storage, not handwritten conclusions."""
    engine = get_engine()
    if engine.dialect.name != "sqlite" or engine.url.database not in (None, "", ":memory:"):
        raise RuntimeError("Synthetic report examples require an in-memory database")
    user = f"synthetic-report-pipeline-{morning}-{explicit_strength}"
    day_start, day_end = local_day_utc_bounds(TARGET)
    analysis_time = day_start + timedelta(hours=9, minutes=30) if morning else day_end + timedelta(minutes=10)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user)
        repo.upsert_user(user)
        for offset in range(56):
            day = TARGET - timedelta(days=offset)
            include_activity = not morning or offset != 0
            repo.save_daily(NormalizedDaily(
                user_id=user, date=day,
                sleep=SleepRecord(user_id=user, date=day, sleep_duration=450,
                                  bedtime=time(23), wake_time=time(7), awake=30),
                activity=ActivityRecord(
                    user_id=user, date=day, steps=5000, calories=400,
                    distance_km=3.5, active_minutes=0, resting_hr=57,
                    observed_fields=["steps", "calories", "distance_km", "resting_hr"],
                ) if include_activity else None,
            ))
            values = [("sleep_hrv", 64, "ms"), ("sleep_rhr", 57, "bpm")]
            if include_activity:
                values += [("steps", 8200, "steps"), ("calories", 700, "kcal"),
                           ("active_minutes", 48, "min"), ("distance", 6200, "m")]
            repo.save_daily_metrics([
                DailyMetric(user_id=user, date=day, metric=metric, value=value,
                            unit=unit, source_scope="user_fused")
                for metric, value, unit in values
            ])
        affected = set()
        for offset, kind, family, label, calories, distance in (
            (0, "strength", "strength", "力量训练", 330, None),
            (2, "running", "aerobic", "户外跑", 380, 7.1),
        ):
            if morning and offset == 0:
                continue
            day = TARGET - timedelta(days=offset)
            start, _ = local_day_utc_bounds(day)
            affected |= repo.save_workout(Workout(
                user_id=user, workout_id=f"synthetic-{kind}", type=kind,
                training_family=family, sport_mode_label=label,
                started_at=start + timedelta(hours=18), duration=45, load=50,
                calories=calories, distance_km=distance, vendor_source=kind,
                heart_rate_avg=110 if family == "strength" else 145,
                vendor_reported_sets=(4 if explicit_strength else 24) if family == "strength" else None,
                observed_fields=["calories"] + (["distance_km"] if distance is not None else []),
            ))
        repo.rebuild_training_days(user, affected)
        if not morning:
            repo.save_workout_detail(user, "synthetic-strength", {
                "schema_version": "4.0",
                "strength_sets": [
                    {"exercise_name": "卧推", "exercise_id": "bench_press", "repetitions": 8, "weight_kg": 40}
                    for _ in range(4)
                ] if explicit_strength else [],
            }, fetched_at=analysis_time - timedelta(minutes=5))
            repo.save_daily_metrics([
                DailyMetric(user_id=user, date=TARGET, metric=metric, value=value,
                            unit=unit, source_scope="user_fused")
                for metric, value, unit in (
                    ("stress", 32, "score"), ("stress_relaxed_pct", 60, "%"),
                    ("stress_normal_pct", 30, "%"), ("stress_medium_pct", 8, "%"),
                    ("stress_high_pct", 2, "%"),
                )
            ])
            repo.save_metric_samples([
                MetricSample(user_id=user, metric=metric, timestamp=day_start + timedelta(minutes=minute),
                             value=value, unit=unit, source_scope="user_fused")
                for minute in range(8 * 60, 22 * 60, 15)
                for metric, value, unit in (
                    ("heart_rate", 110 if 18 * 60 <= minute < 19 * 60 else 72 + (minute // 60) % 7, "bpm"),
                    ("stress", 32 + (minute // 60) % 5, "score"),
                )
            ])
        window = FetchWindow.local_dates(TARGET - timedelta(days=55), TARGET)
        specs = [{
            "stable_key": stable_chunk_key("workouts", sport, window.start, window.end, None),
            "stream": "workouts", "partition": sport, "ordinal": index,
            "window_start": window.start, "window_end": window.end, "allow_unavailable": True,
        } for index, sport in enumerate(SPORTS)]
        attempt = repo.create_or_reuse_sync_attempt(
            user, window_start=window.start, window_end=window.end, manifest=specs,
        )
        finished = (analysis_time - timedelta(minutes=1)).replace(tzinfo=None)
        attempt.created_at = finished - timedelta(minutes=1)
        attempt.status = "succeeded"
        attempt.finished_at = finished
        for chunk in repo.sync_chunks(attempt.id):
            chunk.status = "succeeded"
            chunk.fetch_status = "success"
            chunk.parse_status = "success" if chunk.partition in {"run", "strength"} else "empty"
            chunk.write_status = "success" if chunk.parse_status == "success" else "not_run"
            chunk.finished_at = finished

    class ExampleClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(analysis_time.timestamp(), tz=tz)

    with patch("vitalis.intelligence.service.datetime", ExampleClock), patch("vitalis.intelligence.contracts.datetime", ExampleClock):
        return IntelligenceCommand().analyze(user, TARGET)


def test_stored_observations_reach_analysis_and_all_four_reports():
    analyzed = synthetic_pipeline_example()
    activity = analyzed.daily.features.activity
    assert activity.steps.value == 8200
    assert activity.distance_km.value == 6.2
    assert activity.active_minutes.value == 48
    daily_energy = [item for item in activity.energy if item.role != "workout"]
    assert len(daily_energy) == 1 and daily_energy[0].value == 700
    assert daily_energy[0].role == "unspecified"
    workouts = analyzed.daily.features.training.recent_workouts
    assert sorted(item.calories_kcal for item in workouts) == [330, 380]
    assert analyzed.weekly.facts.training.workout_calories_kcal == 710
    assert analyzed.monthly.facts.training.unknown_days == 0
    assert analyzed.monthly.facts.training.rest_days == 26
    assert activity.stress_summary
    morning = MorningBriefingEngine().build(analyzed.daily)
    evening = EveningBriefingEngine().build(analyzed.daily)
    weekly = WeeklyBriefingEngine().build(analyzed.weekly)
    monthly = MonthlyBriefingEngine().build(analyzed.monthly)
    for report in (morning, evening, weekly, monthly):
        assert report.sections and "feedback_prompt" not in report.model_dump()
        text = report.model_dump_json()
        assert "训练后告诉我" not in text and "Zepp 厂商汇总" not in text
    text = evening.model_dump_json()
    assert "步数 8,200" in text and "活动距离 6.2" in text
    assert "700" in text and "卧推" in text and "活动时长" in text
    assert all(f"第 {order} 组：卧推；8 次；40 千克" in text for order in range(1, 5))
    assert "放松区间" in text
    assert weekly.period_end == TARGET and monthly.period_end == TARGET


def test_morning_pipeline_does_not_require_a_workout_today():
    analyzed = synthetic_pipeline_example(morning=True)
    assert analyzed.daily.features.training.today_workouts is None
    assert analyzed.daily.report_context["target_day_complete"] is False
    report = MorningBriefingEngine().build(analyzed.daily)
    assert report.action_plan.primary_session is not None
    assert report.sections[2].facts
    assert "feedback_prompt" not in report.model_dump()
