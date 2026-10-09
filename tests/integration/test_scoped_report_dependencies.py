"""Scope changes must refresh every saved target that actually depends on them."""

from datetime import date, timedelta

import vitalis.adapters.persistence.repositories as persistence_repositories
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.bootstrap import get_intelligence_command, get_intelligence_query
from vitalis.domain import NormalizedDaily, SleepRecord


DAY = date(2026, 8, 28)


def _sleep(user_id, day, minutes):
    return NormalizedDaily(user_id=user_id, date=day, sleep=SleepRecord(
        user_id=user_id, date=day, sleep_duration=minutes,
    ))


def test_late_sleep_queues_existing_dependent_target_and_keeps_older_history_current():
    user_id = "scope-dependency-late-sleep"
    target = DAY + timedelta(days=7)
    history = DAY - timedelta(days=1)
    command, query = get_intelligence_command(), get_intelligence_query()
    old = command.analyze(user_id, history)
    saved = command.analyze(user_id, target)
    with session_scope() as db:
        HealthRepository(db).save_daily(_sleep(user_id, DAY, 440))
    state = query.report_state(user_id, "daily", target)
    assert state["state"] == "queued"
    assert state["last_good_snapshot"]["analysis_run_id"] == saved.run.id
    assert query.report_state(user_id, "daily", history)["state"] == "current"
    assert query.daily(user_id, history).analysis_run_id == old.run.id
    with session_scope() as db:
        assert target in {job.target_date for job in HealthRepository(db).analysis_jobs(user_id)}
    command.analyze(user_id, DAY)
    assert query.report_state(user_id, "daily", target)["state"] == "queued"
    command.analyze(user_id, target)
    assert query.report_state(user_id, "daily", target)["state"] == "current"


def test_calendar_state_tracks_requested_target_not_only_the_period_end():
    user_id = "scope-dependency-calendar"
    target = date(2026, 10, 8)
    changed_day = date(2026, 9, 20)
    command, query = get_intelligence_command(), get_intelligence_query()
    saved = command.analyze(user_id, target)
    with session_scope() as db:
        HealthRepository(db).save_daily(_sleep(user_id, changed_day, 430))
    state = query.report_state(user_id, "monthly", target)
    assert state["state"] == "queued"
    with session_scope() as db:
        job = next(job for job in HealthRepository(db).analysis_jobs(user_id) if job.id == state["job_id"])
        assert job.target_date == target
    assert state["last_good_snapshot"]["analysis_run_id"] == saved.run.id


def test_consumed_invalidation_never_makes_an_older_run_deliverable_again():
    user_id = "scope-dependency-consumed"
    command = get_intelligence_command()
    saved = command.analyze(user_id, DAY)
    with session_scope() as db:
        HealthRepository(db).save_daily(_sleep(user_id, DAY, 455))
    refreshed = command.analyze(user_id, DAY)
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.latest_analysis_snapshot(user_id, "daily", DAY).analysis_run_id == refreshed.run.id
        assert repo.analysis_snapshot_for_run(user_id, "daily", DAY, saved.run.id) is None


def test_config_change_is_stale_without_job_and_keeps_payload_run_consistent(monkeypatch):
    user_id = "scope-dependency-config"
    command, query = get_intelligence_command(), get_intelligence_query()
    saved = command.analyze(user_id, DAY)

    for profile_type in ("daily", "weekly", "monthly"):
        state = query.report_state(user_id, profile_type, DAY)
        report = getattr(query, profile_type)(user_id, DAY)
        assert state["state"] == "current"
        assert state["stale_since"] is None
        assert state["analysis_run_id"] == state["last_good_snapshot"]["analysis_run_id"] == saved.run.id
        assert report.analysis_run_id == saved.run.id
        assert report.report_context["report_state"]["last_good_snapshot"]["analysis_run_id"] == saved.run.id
        assert report.report_context["report_state"]["state"] == "current"

    monkeypatch.setattr(
        persistence_repositories,
        "_current_analysis_config_digest",
        lambda: "changed-config-digest",
    )
    with session_scope() as db:
        assert HealthRepository(db).analysis_jobs_for_target(user_id, DAY) == []

    for profile_type in ("daily", "weekly", "monthly"):
        state = query.report_state(user_id, profile_type, DAY)
        report = getattr(query, profile_type)(user_id, DAY)
        assert state["state"] == "stale"
        assert state["stale_since"] is None
        assert state["job_id"] is None
        assert state["failure_code"] is None
        assert state["next_action"] == "retry_analysis"
        assert state["analysis_run_id"] == state["last_good_snapshot"]["analysis_run_id"] == saved.run.id
        assert report.analysis_run_id == saved.run.id
        assert report.report_context["report_state"]["last_good_snapshot"]["analysis_run_id"] == saved.run.id
        assert report.report_context["report_state"]["state"] == "stale"
        assert report.report_context["report_state"]["stale_since"] is None
        assert state["facts"] == getattr(saved, profile_type).model_dump(mode="json")["facts"]
        assert report.model_dump(mode="json")["facts"] == state["facts"]
