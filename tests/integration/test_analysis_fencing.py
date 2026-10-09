"""Worker success, retries, and account deletion against an isolated database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from vitalis import bootstrap
from vitalis.application import jobs
from vitalis.application.intelligence_service import IntelligenceCommand
from vitalis.bootstrap import get_intelligence_command
from vitalis.adapters.persistence import HealthRepository, database
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import (
    AnalysisJob, AnalysisRun, AnalysisSnapshot, HealthEventObservation,
    NotificationDelivery, RecommendationInstance, User,
)
from vitalis.intelligence.contracts import ConfidenceBand, EventSeverity, HealthEvent
from vitalis.intelligence.morning_briefing import MorningBriefingEngine


DAY = date(2026, 8, 29)


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'fencing.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(jobs, "_repository", None)
    monkeypatch.setattr(jobs, "_runner", None)
    bootstrap.configure_analysis_jobs()
    repository = jobs._repository
    with factory.begin() as db:
        db.add(User(id="owner"))
    try:
        yield factory, repository
    finally:
        engine.dispose()


def _past():
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)


def test_stolen_claim_cannot_publish_and_reclaimed_job_recovers(isolated_jobs, monkeypatch):
    factory, repository = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "reclaim")
    old_claim = repository.claim(2)
    original_build = IntelligenceCommand._build_daily_from_raw
    reclaimed = []

    def steal_before_final_transaction(*args):
        if not reclaimed:
            with factory.begin() as db:
                db.get(AnalysisJob, job_id).lease_expires_at = _past()
            reclaimed.append(repository.claim(30))
        return original_build(*args)

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(steal_before_final_transaction),
    )
    with pytest.raises(RuntimeError, match="claim is no longer current"):
        bootstrap._run_analysis(old_claim, repository=repository)

    new_claim = reclaimed[0]
    assert new_claim.id == job_id and new_claim.token != old_claim.token
    assert new_claim.epoch == old_claim.epoch + 1
    with factory() as db:
        assert db.get(AnalysisJob, job_id).status == "running"
        assert db.query(AnalysisRun).filter_by(status="FAILED").count() == 1
        assert db.query(AnalysisSnapshot).count() == 0
        assert db.query(HealthEventObservation).count() == 0
        assert db.query(RecommendationInstance).count() == 0

    run_id = bootstrap._run_analysis(new_claim, repository=repository)
    state = jobs.get_analysis_job("owner", job_id)
    assert state.status == "succeeded" and state.run_id == run_id
    assert state.attempt_count == 2
    with factory() as db:
        assert db.query(AnalysisRun).filter_by(status="SUCCEEDED").count() == 1
        assert db.query(AnalysisRun).filter_by(status="FAILED").count() == 1
        assert db.query(AnalysisSnapshot).count() >= 3
        assert {row.analysis_run_id for row in db.query(AnalysisSnapshot)} == {run_id}
        assert {row.analysis_run_id for row in db.query(RecommendationInstance)} == {run_id}
        assert {row.analysis_run_id for row in db.query(HealthEventObservation)} <= {run_id}
    assert jobs.create_analysis_job("owner", DAY, "reclaim") == job_id
    assert jobs.drain_analysis_jobs() == 0


def test_expired_unclaimed_lease_cannot_publish(isolated_jobs, monkeypatch):
    factory, repository = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "expired")
    claim = repository.claim(2)
    original_build = IntelligenceCommand._build_daily_from_raw

    def expire_before_final_transaction(*args):
        with factory.begin() as db:
            db.get(AnalysisJob, job_id).lease_expires_at = _past()
        return original_build(*args)

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(expire_before_final_transaction),
    )
    with pytest.raises(RuntimeError, match="claim is no longer current"):
        bootstrap._run_analysis(claim, repository=repository)
    assert jobs.get_analysis_job("owner", job_id).status == "running"
    with factory() as db:
        assert db.query(AnalysisRun).filter_by(status="SUCCEEDED").count() == 0
        assert db.query(AnalysisSnapshot).count() == 0
        assert db.query(RecommendationInstance).count() == 0


def test_failure_after_snapshot_write_rolls_back_job_and_outputs(isolated_jobs, monkeypatch):
    factory, _ = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "rollback")
    original_save = IntelligenceCommand._save_snapshot

    def interrupt_after_snapshot(*args):
        original_save(*args)
        raise RuntimeError("interrupted after writing a snapshot")

    monkeypatch.setattr(IntelligenceCommand, "_save_snapshot", staticmethod(interrupt_after_snapshot))
    assert jobs.drain_analysis_jobs() == 1
    state = jobs.get_analysis_job("owner", job_id)
    assert state.status == "failed" and state.error == "Analysis failed"
    assert state.run_id is None
    with factory() as db:
        assert db.query(AnalysisRun).filter_by(status="FAILED").count() == 1
        assert db.query(AnalysisRun).filter_by(status="SUCCEEDED").count() == 0
        assert db.query(AnalysisSnapshot).count() == 0
        assert db.query(HealthEventObservation).count() == 0
        assert db.query(RecommendationInstance).count() == 0


def test_job_and_snapshots_are_not_visible_until_success_commits(isolated_jobs, monkeypatch):
    factory, _ = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "visibility")
    saved, release = Event(), Event()
    original_save = IntelligenceCommand._save_snapshot

    def pause_after_snapshot(*args):
        original_save(*args)
        if not saved.is_set():
            saved.set()
            assert release.wait(10)

    monkeypatch.setattr(IntelligenceCommand, "_save_snapshot", staticmethod(pause_after_snapshot))
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(jobs.drain_analysis_jobs, lease_seconds=30)
        try:
            assert saved.wait(10)
            with factory() as db:
                assert db.get(AnalysisJob, job_id).status == "running"
                assert db.query(AnalysisSnapshot).count() == 0
                assert db.query(RecommendationInstance).count() == 0
        finally:
            release.set()
        assert worker.result(timeout=10) == 1
    with factory() as db:
        row = db.get(AnalysisJob, job_id)
        assert row.status == "succeeded" and row.run_id is not None
        assert db.query(AnalysisSnapshot).filter_by(analysis_run_id=row.run_id).count() >= 3


def test_delete_during_analysis_does_not_recreate_user_or_results(isolated_jobs, monkeypatch):
    factory, _ = isolated_jobs
    jobs.create_analysis_job("owner", DAY, "deleted-during")
    original_build = IntelligenceCommand._build_daily_from_raw

    def delete_before_final_transaction(*args):
        with factory.begin() as db:
            HealthRepository(db).delete_for_user("owner")
        return original_build(*args)

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(delete_before_final_transaction),
    )
    assert jobs.drain_analysis_jobs() == 1
    with factory() as db:
        assert db.get(User, "owner") is None
        for model in (
            AnalysisJob, AnalysisRun, AnalysisSnapshot,
            HealthEventObservation, RecommendationInstance,
        ):
            assert db.query(model).count() == 0


def test_success_requires_user_even_if_job_row_remains(isolated_jobs, monkeypatch):
    factory, _ = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "missing-user")
    original_build = IntelligenceCommand._build_daily_from_raw

    def remove_user_before_final_transaction(*args):
        # Simulate a separately deleted user with an orphaned job row.
        with factory.begin() as db:
            db.execute(delete(User).where(User.id == "owner"))
        return original_build(*args)

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(remove_user_before_final_transaction),
    )
    assert jobs.drain_analysis_jobs() == 1
    with factory() as db:
        assert db.get(User, "owner") is None
        assert db.get(AnalysisJob, job_id).status == "failed"
        assert db.query(AnalysisRun).filter_by(status="SUCCEEDED").count() == 0
        assert db.query(AnalysisSnapshot).count() == 0
        assert db.query(RecommendationInstance).count() == 0


def test_deleted_job_cannot_publish_for_existing_user(isolated_jobs, monkeypatch):
    factory, _ = isolated_jobs
    job_id = jobs.create_analysis_job("owner", DAY, "job-removed")
    original_build = IntelligenceCommand._build_daily_from_raw

    def delete_job_before_final_transaction(*args):
        with factory.begin() as db:
            db.delete(db.get(AnalysisJob, job_id))
        return original_build(*args)

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(delete_job_before_final_transaction),
    )
    assert jobs.drain_analysis_jobs() == 1
    with factory() as db:
        assert db.get(User, "owner") is not None
        assert db.get(AnalysisJob, job_id) is None
        assert db.query(AnalysisRun).filter_by(status="FAILED").count() == 1
        assert db.query(AnalysisRun).filter_by(status="SUCCEEDED").count() == 0
        assert db.query(AnalysisSnapshot).count() == 0
        assert db.query(HealthEventObservation).count() == 0
        assert db.query(RecommendationInstance).count() == 0


def test_deleted_before_worker_starts_does_not_recreate_user(isolated_jobs):
    factory, repository = isolated_jobs
    jobs.create_analysis_job("owner", DAY, "deleted-before")
    claim = repository.claim(30)
    with factory.begin() as db:
        HealthRepository(db).delete_for_user("owner")

    with pytest.raises(ValueError, match="user not found"):
        bootstrap._run_analysis(claim, repository=repository)
    with factory() as db:
        assert db.get(User, "owner") is None
        assert db.query(AnalysisJob).count() == 0
        assert db.query(AnalysisRun).count() == 0
        assert db.query(AnalysisSnapshot).count() == 0


def test_manual_analysis_needs_no_claim(isolated_jobs):
    factory, _ = isolated_jobs
    result = get_intelligence_command().analyze("manual", DAY)
    with factory() as db:
        assert db.get(User, "manual") is not None
        assert db.get(AnalysisRun, result.run.id).status == "SUCCEEDED"
        assert db.query(AnalysisJob).count() == 0
        assert db.query(AnalysisSnapshot).filter_by(analysis_run_id=result.run.id).count() >= 3
        assert db.query(NotificationDelivery).count() == 0


def test_event_acknowledged_during_analysis_is_consistent_across_snapshots(
    isolated_jobs, monkeypatch,
):
    factory, _ = isolated_jobs
    event = HealthEvent(
        id="synthetic-event", type="SLEEP_DEFICIT", type_label="持续睡眠不足",
        severity=EventSeverity.MODERATE, severity_label="中等",
        metric="sleep_duration", metric_label="睡眠时长",
        start_date=DAY - timedelta(days=2), end_date=DAY - timedelta(days=1),
        duration_days=2, confidence=ConfidenceBand.HIGH,
        confidence_label="较高", summary="合成事件",
    )
    with factory.begin() as db:
        HealthRepository(db).save_health_event("owner", event)

    original_build = IntelligenceCommand._build_daily_from_raw

    def acknowledge_after_input_read(*args):
        daily = original_build(*args)
        with factory.begin() as db:
            acknowledged = HealthRepository(db).acknowledge_health_event("owner", event.id)
            assert acknowledged is not None and acknowledged.acknowledged
        return daily

    monkeypatch.setattr(
        IntelligenceCommand, "_build_daily_from_raw",
        staticmethod(acknowledge_after_input_read),
    )
    with pytest.raises(RuntimeError, match="输入在计算期间发生变化"):
        get_intelligence_command().analyze("owner", DAY)
    result = get_intelligence_command().analyze("owner", DAY)
    assert result.daily.events[0].acknowledged
    assert result.weekly.inferences.events == []
    assert result.monthly.inferences.events == []
    assert result.morning_briefing == MorningBriefingEngine().build(result.daily)

    with factory() as db:
        rows = {
            row.profile_type: row.payload
            for row in db.query(AnalysisSnapshot).filter_by(analysis_run_id=result.run.id)
        }
    assert rows["daily"]["events"][0]["acknowledged"] is True
    assert rows["weekly"]["inferences"]["events"] == []
    assert rows["monthly"]["inferences"]["events"] == []


def test_historical_direct_analysis_resolves_only_covered_jobs_without_delivery(
    isolated_jobs, monkeypatch,
):
    from vitalis.domain import NormalizedDaily, SleepRecord

    factory, _ = isolated_jobs
    with factory.begin() as db:
        repo = HealthRepository(db)
        repo.save_daily(NormalizedDaily(user_id="owner", date=DAY, sleep=SleepRecord(
            user_id="owner", date=DAY, sleep_duration=440,
        )))
        failed = next(job for job in repo.analysis_jobs("owner") if job.target_date == DAY)
        failed.status = "failed"
        db.flush()
        queued = repo.enqueue_analysis_job(
            "owner", DAY, event_type="source_sync", source="zepp",
        )
        covered_ids = {failed.id, queued.id}
        delivery_ids = {
            repo.enqueue_analysis_job(
                "owner", DAY, event_type="explicit_analysis", source="scheduler",
                delivery_period=period,
            ).id
            for period in ("morning", "evening", "weekly", "monthly")
        }

    command = get_intelligence_command()
    historical_at = datetime(2026, 8, 29, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(command, "_now_factory", lambda: historical_at)
    result = command.analyze("owner", DAY)
    with factory.begin() as db:
        repo = HealthRepository(db)
        for job_id in covered_ids:
            job = db.get(AnalysisJob, job_id)
            assert job.created_at > historical_at.replace(tzinfo=None)
            assert job.status == "succeeded" and job.run_id == result.run.id
            assert job.finished_at > historical_at.replace(tzinfo=None)
        for job_id in delivery_ids:
            job = db.get(AnalysisJob, job_id)
            assert job.status == "queued" and job.run_id is None
        assert db.query(NotificationDelivery).count() == 0

        repo.save_daily(NormalizedDaily(user_id="owner", date=DAY, sleep=SleepRecord(
            user_id="owner", date=DAY, sleep_duration=455,
        )))
        newer_failed = next(
            job for job in repo.analysis_jobs("owner")
            if job.target_date == DAY and job.delivery_period is None and job.status == "queued"
        )
        newer_failed.status = "failed"
        db.flush()
        newer_queued = repo.enqueue_analysis_job(
            "owner", DAY, event_type="source_sync", source="zepp",
        )
        assert newer_failed.input_revision > result.run.input_revision_used
        assert newer_queued.input_revision > result.run.input_revision_used
        assert repo.resolve_analysis_jobs_for_run(
            "owner", DAY, result.run.id, input_revision=result.run.input_revision_used,
        ) == 0
        assert newer_failed.status == "failed" and newer_failed.run_id is None
        assert newer_queued.status == "queued" and newer_queued.run_id is None


@pytest.mark.parametrize("event_type", ["event_acknowledgement", None])
def test_consumed_acknowledgement_keeps_previous_run_and_snapshot_fenced(
    isolated_jobs, event_type,
):
    factory, _ = isolated_jobs
    event = HealthEvent(
        id="synthetic-consumed-event", type="SLEEP_DEFICIT", type_label="持续睡眠不足",
        severity=EventSeverity.MODERATE, severity_label="中等",
        metric="sleep_duration", metric_label="睡眠时长",
        start_date=DAY - timedelta(days=2), end_date=DAY - timedelta(days=1),
        duration_days=2, confidence=ConfidenceBand.HIGH,
        confidence_label="较高", summary="合成事件",
    )
    with factory.begin() as db:
        HealthRepository(db).save_health_event("owner", event)
    command = get_intelligence_command()
    previous = command.analyze("owner", DAY)
    with factory.begin() as db:
        repo = HealthRepository(db)
        assert repo.acknowledge_health_event("owner", event.id).acknowledged
        job = next(job for job in repo.analysis_jobs("owner") if job.target_date == DAY)
        job.event_type = event_type
        job_id = job.id
    refreshed = command.analyze("owner", DAY)
    with factory() as db:
        repo = HealthRepository(db)
        assert db.get(AnalysisJob, job_id).status == "succeeded"
        assert not repo.lock_analysis_scope("owner", previous.run.input_revision_used, DAY)
        assert repo.lock_analysis_scope("owner", refreshed.run.input_revision_used, DAY)
        assert repo.analysis_snapshot_for_run("owner", "daily", DAY, previous.run.id) is None
        assert repo.latest_analysis_snapshot("owner", "daily", DAY).analysis_run_id == refreshed.run.id
