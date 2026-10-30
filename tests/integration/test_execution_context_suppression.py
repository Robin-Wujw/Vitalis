"""Synthetic isolation contracts for conversation-owned background work."""
from datetime import date

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.bootstrap import get_intelligence_command

@pytest.fixture(autouse=True)
def _refresh_synthetic_dates(monkeypatch):
    from vitalis.adapters.persistence import repositories
    monkeypatch.setattr(repositories, "ANALYSIS_REFRESH_DAYS", 100_000)


DAY = date(2026, 10, 28)
CONTEXT = {"origin": "conversation", "root_id": "synthetic-root", "notification_policy": "suppress", "policy_revision": "synthetic-v1"}


def test_context_partitions_coalescing_and_blocks_promotion():
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("context-partition")
        db.flush()
        def enqueue(context):
            return repo.enqueue_analysis_job("context-partition", DAY, event_type="sync_completion", source="zepp", execution_context=context)
        ordinary = enqueue({})
        silent = enqueue(CONTEXT)
        assert ordinary.id != silent.id
        assert enqueue(dict(reversed(list(CONTEXT.items())))).id == silent.id
        assert enqueue({**CONTEXT, "root_id": "other-root"}).id != silent.id
        assert enqueue({**CONTEXT, "policy_revision": "v2"}).id != silent.id
        assert repo.promote_queued_sync_analysis_job("context-partition", DAY, "evening", reason="silent", execution_context=CONTEXT) is None
        assert ordinary.delivery_period is None
        assert repo.promote_queued_sync_analysis_job("context-partition", DAY, "evening", reason="schedule").id == ordinary.id
        assert repo.promote_queued_sync_analysis_job("context-partition", DAY, "morning", reason="schedule") is None


def test_transaction_context_is_copied_and_explicit_empty_overrides():
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("context-session")
        db.flush()
        context = dict(CONTEXT)
        db.info["execution_context"] = context
        silent = repo.enqueue_analysis_job("context-session", DAY, event_type="sync_completion", source="zepp")
        ordinary = repo.enqueue_analysis_job("context-session", DAY, event_type="sync_completion", source="zepp", execution_context={})
        context["root_id"] = "mutated"
        assert silent.execution_context == CONTEXT
        assert ordinary.execution_context == {}
    with session_scope() as db:
        assert "execution_context" not in db.info


def test_suppressed_run_cannot_enqueue_repoint_or_rearm_notifications():
    user = "context-notification"
    normal = get_intelligence_command().analyze(user, DAY)
    with session_scope() as db:
        repo = HealthRepository(db)
        delivery = repo.enqueue_notification_delivery(user, normal.run.id, "evening", DAY)
        delivery.status = "deferred"
        delivery.last_error = "snapshot_unavailable"
        delivery_id = delivery.id
    silent = get_intelligence_command().analyze(user, DAY, execution_context=CONTEXT)
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.analysis_run(user, silent.run.id).execution_context == CONTEXT
        existing = repo.notification_delivery(delivery_id, user)
        assert existing.analysis_run_id == normal.run.id
        assert existing.status == "deferred"
        assert repo.rearm_unavailable_notification_deliveries(user, silent.run.id, DAY) == 0
        assert repo.rearm_morning_deferred_notification_delivery(user, silent.run.id, DAY) == 0
        assert repo.refresh_existing_calendar_notification_deliveries(user, silent.run.id, DAY) == 0
        assert repo._repoint_notification_delivery(existing, silent.run.id).analysis_run_id == normal.run.id
        with pytest.raises(ValueError, match="suppressed"):
            repo.enqueue_notification_delivery(user, silent.run.id, "evening", DAY)


def test_leased_delivery_does_not_repoint_to_silent_snapshot():
    user = "context-leased"
    normal = get_intelligence_command().analyze(user, DAY)
    with session_scope() as db:
        repo = HealthRepository(db)
        delivery = repo.enqueue_notification_delivery(user, normal.run.id, "evening", DAY)
        delivery_id = delivery.id
    get_intelligence_command().analyze(user, DAY, execution_context=CONTEXT)
    with session_scope() as db:
        repo = HealthRepository(db)
        delivery = repo.claim_notification_delivery(delivery_id=delivery_id)
        snapshot = repo.notification_delivery_snapshot(delivery_id, user, normal.run.id, delivery.lease_token)
        assert snapshot is not None
        assert snapshot.analysis_run_id == normal.run.id
        assert delivery.analysis_run_id == normal.run.id


def test_worker_claim_propagates_context_and_preserves_normal_jobs():
    from sqlalchemy.orm import sessionmaker
    from vitalis.adapters.persistence import get_engine
    from vitalis.adapters.persistence.analysis_jobs import SqlAnalysisJobRepository

    user = "context-worker"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        db.flush()
        silent = repo.enqueue_analysis_job(user, DAY, event_type="manual", source="test", execution_context=CONTEXT, delivery_period="evening")
        silent_id = silent.id
        ordinary = repo.enqueue_analysis_job(user, DAY, event_type="manual", source="test")
        ordinary_id = ordinary.id
    jobs = SqlAnalysisJobRepository(sessionmaker(bind=get_engine(), expire_on_commit=False))
    claim = jobs.claim(300, user_id=user)
    assert claim.id == silent_id
    assert claim.execution_context == CONTEXT
    result = get_intelligence_command().analyze(user, DAY, job_claim=claim, job_repository=jobs)
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.analysis_run(user, result.run.id).execution_context == CONTEXT
        assert not repo.notification_deliveries(user)
        assert next(row for row in repo.analysis_jobs_for_target(user, DAY) if row.id == ordinary_id).status == "queued"


def test_direct_run_resolves_only_its_context():
    user = "context-direct"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        db.flush()
        ordinary = repo.enqueue_analysis_job(user, DAY, event_type="manual", source="test")
        silent = repo.enqueue_analysis_job(user, DAY, event_type="manual", source="test", execution_context=CONTEXT)
        ordinary_id, silent_id = ordinary.id, silent.id
    get_intelligence_command().analyze(user, DAY, execution_context=CONTEXT)
    with session_scope() as db:
        rows = {row.id: row for row in HealthRepository(db).analysis_jobs_for_target(user, DAY)}
        assert rows[silent_id].status == "succeeded"
        assert rows[ordinary_id].status == "queued"
    get_intelligence_command().analyze(user, DAY)
    with session_scope() as db:
        rows = {row.id: row for row in HealthRepository(db).analysis_jobs_for_target(user, DAY)}
        assert rows[ordinary_id].status == "succeeded"


def test_sync_source_terminal_and_coverage_jobs_keep_context():
    from datetime import datetime, timezone
    from vitalis.adapters.zepp.fetcher import FetchWindow
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

    class Connector:
        def fetch_sport_history(self, sport, start, stop, need_sub_data):
            return {"data": {"items": [{
                "trackid": 1793170800, "type": 1,
                "start_time": "2026-10-28T07:00:00Z",
                "end_time": "2026-10-28T08:00:00Z",
                "source": "run", "distance": 1000,
            }], "next": -1}}

        def fetch_sport_detail(self, workout_id, source):
            return {"data": {"trackid": workout_id, "source": source}}

    user = "context-sync"
    coordinator = ZeppSyncCoordinator(connector=Connector(), wall_clock=lambda: datetime(2026, 10, 29, tzinfo=timezone.utc))
    attempt = coordinator.create_attempt(user, window=FetchWindow.local_dates(DAY, DAY), options={"workout_only": True, "execution_context": CONTEXT})
    coordinator.run_attempt(attempt.id)
    with session_scope() as db:
        from vitalis.adapters.persistence import models as orm
        from sqlalchemy import select
        jobs = db.execute(select(orm.AnalysisJob).where(orm.AnalysisJob.user_id == user)).scalars().all()
        assert jobs
        assert all(row.execution_context == CONTEXT for row in jobs)
        from vitalis.adapters.persistence.input_events import InputEvent
        events = db.execute(select(InputEvent).where(InputEvent.user_id == user)).scalars().all()
        assert {"source_sync", "training_coverage", "sync_completion"} <= {row.event_type for row in events}
        from vitalis.adapters.persistence.input_events import AnalysisInvalidation
        links = db.execute(select(AnalysisInvalidation).where(AnalysisInvalidation.user_id == user)).scalars().all()
        assert {row.id for row in events} <= {row.event_id for row in links}
        assert {row.job_id for row in links} <= {row.id for row in jobs}
