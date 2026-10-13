"""Persisted coverage must reach the off-peak shadow load without inventing empty days."""

from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.zepp.client import SPORTS
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import PLAN_VERSION, stable_chunk_key
from vitalis.bootstrap import get_intelligence_command, get_shadow_batch
from vitalis.domain import Workout
from vitalis.intelligence.contracts import OpenHealthBundle, UserProfilePatch
from vitalis.intelligence.profile import ProfileLoader


DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def _seed(user_id, *, covered=True):
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.patch_user_profile(user_id, UserProfilePatch(
            expected_revision=0, sex="MALE", confirmed_hrmax_bpm=190,
        ))
        if not covered:
            return
        window = FetchWindow.local_dates(DAY - timedelta(days=41), DAY)
        attempt = repo.create_or_reuse_sync_attempt(
            user_id, plan_version=PLAN_VERSION,
            window_start=window.start, window_end=window.end,
            manifest=[{
                "stable_key": stable_chunk_key("workouts", sport, window.start, window.end, None),
                "stream": "workouts", "partition": sport, "ordinal": index,
                "window_start": window.start, "window_end": window.end,
            } for index, sport in enumerate(SPORTS)],
        )
        attempt.created_at = (NOW - timedelta(minutes=2)).replace(tzinfo=None)
        attempt.status = "succeeded"
        attempt.finished_at = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
        for chunk in repo.sync_chunks(attempt.id):
            chunk.status = "succeeded"
            chunk.fetch_status = "success"
            chunk.parse_status = "empty"
            chunk.write_status = "not_run"
            chunk.finished_at = attempt.finished_at


def _shadow_load(user_id):
    get_shadow_batch(now_factory=lambda: NOW).run(user_id, DAY)
    with session_scope() as db:
        row = HealthRepository(db).latest_shadow_result(user_id, "open_health", DAY)
        return OpenHealthBundle.model_validate(row.payload).training_load


def test_daily_analysis_leaves_open_health_to_the_shadow_batch():
    user_id = "load-wiring-daily-path"
    _seed(user_id)
    result = get_intelligence_command(now_factory=lambda: NOW).analyze(user_id, DAY)
    assert result.daily.open_health_insights is None
    assert _shadow_load(user_id).status.value == "AVAILABLE"


def test_complete_42_day_ledger_reaches_saved_shadow_load():
    user_id = "load-wiring-complete"
    _seed(user_id)
    load = _shadow_load(user_id)
    assert load.status.value == "AVAILABLE"
    assert load.shadow_only is True
    assert load.coverage["upstream_coverage_verified"] is True
    assert load.payload.lower_bound is False
    assert len(load.payload.daily_points) == 42
    assert all(point.status == "REST" and point.trimp == 0 for point in load.payload.daily_points)
    assert load.payload.atl == load.payload.ctl == load.payload.tsb == 0


def test_local_records_without_ledger_do_not_become_verified_rest():
    user_id = "load-wiring-unverified"
    _seed(user_id, covered=False)
    load = _shadow_load(user_id)
    assert load.status.value == "REFUSED"
    assert load.coverage["upstream_coverage_verified"] is False
    assert all(point.status == "UNKNOWN" and point.trimp is None for point in load.payload.daily_points)


def test_workout_summary_limit_cannot_turn_omitted_training_into_rest():
    user_id = "load-wiring-summary-limit"
    _seed(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        for index in range(201):
            workout_day = DAY - timedelta(days=1) if index < 200 else DAY
            repo.save_workout(Workout(
                user_id=user_id, workout_id=f"limit-{index:03}",
                started_at=datetime.combine(workout_day, datetime.min.time(), tzinfo=timezone.utc),
                duration=20, type="running",
            ))
        raw = ProfileLoader(repo, shadow_inputs=True).load(user_id, DAY, as_of=NOW)
    assert raw.open_health_load_truncated is True
    assert raw.open_health_load_queried_days == []
    assert raw.open_health_load_upstream_coverage_verified is False
    load = _shadow_load(user_id)
    omitted_day = next(point for point in load.payload.daily_points if point.date == DAY)
    assert omitted_day.status == "UNKNOWN" and omitted_day.trimp is None


def test_load_input_excludes_workouts_after_report_cutoff():
    user_id = "load-wiring-cutoff"
    _seed(user_id, covered=False)
    cutoff = datetime(2026, 9, 20, 4, tzinfo=timezone.utc)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.save_workout(Workout(
            user_id=user_id, workout_id="future-session",
            started_at=cutoff + timedelta(hours=1), duration=20, type="running",
        ))
        raw = ProfileLoader(repo, shadow_inputs=True).load(user_id, DAY, as_of=cutoff)
    assert raw.open_health_load_workouts == []
