"""A sync chunk may commit data only under its current parent and child leases."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import MetricSample as StoredSample
from vitalis.adapters.persistence.models import SyncAttempt, SyncChunk, User
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.adapters.zepp.sync_coordinator import (
    StaleSyncLease, ZeppSyncCoordinator, _ClaimedChunk, _ChunkResult,
)
from vitalis.application.sync_types import SyncLease
from vitalis.domain import AuthToken, MetricSample


NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'sync-fencing.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _claim(sessions, *, attempt_seconds=60, chunk_seconds=60):
    with sessions.begin() as db:
        repo = HealthRepository(db)
        attempt = repo.create_or_reuse_sync_attempt(
            "owner", window_start=NOW - timedelta(days=1), window_end=NOW,
            options={"source_mode": "mock"},
            manifest=[{"stable_key": "hr:0", "stream": "heart_rate",
                       "stages": {"operation": "synthetic"}}],
        )
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_attempt(
            attempt.id, "attempt-v1", now=NOW, lease_seconds=attempt_seconds,
        )
        assert repo.claim_chunk(
            chunk.id, "chunk-v1", now=NOW, lease_seconds=chunk_seconds,
            attempt_lease_token="attempt-v1", attempt_lease_epoch=1,
        )
        return attempt.id, chunk.id


def _sample():
    return MetricSample(
        user_id="owner", metric="heart_rate", timestamp=NOW,
        value=72, unit="bpm", source_record_id="synthetic-sample",
    )


@pytest.mark.parametrize("seconds", [1, 2])
def test_expired_chunk_cannot_finalize_before_any_takeover(sessions, seconds):
    attempt_id, chunk_id = _claim(sessions, chunk_seconds=1)
    later = NOW + timedelta(seconds=seconds)
    with sessions.begin() as db:
        repo = HealthRepository(db)
        assert not repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded", now=later,
            raw_records=1, records_written=1,
        )
        assert not repo.finalize_sync_chunk_retry(
            chunk_id, "chunk-v1", 1, now=later,
            next_retry_at=later + timedelta(seconds=5),
        )
    with sessions() as db:
        chunk = db.get(SyncChunk, chunk_id)
        assert chunk.status == "running" and chunk.records_written == 0
        assert db.get(SyncAttempt, attempt_id).status == "running"


@pytest.mark.parametrize("seconds", [1, 2])
def test_expired_parent_cannot_finalize_chunk_or_attempt_before_takeover(sessions, seconds):
    attempt_id, chunk_id = _claim(sessions, attempt_seconds=1)
    later = NOW + timedelta(seconds=seconds)
    with sessions.begin() as db:
        repo = HealthRepository(db)
        assert not repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded", now=later,
        )
        assert not repo.finalize_attempt(
            attempt_id, "attempt-v1", 1, "partial", now=later,
        )
        assert not repo.finalize_sync_attempt_failure(
            attempt_id, "attempt-v1", 1, now=later,
        )
    with sessions() as db:
        assert db.get(SyncAttempt, attempt_id).status == "running"
        assert db.get(SyncChunk, chunk_id).status == "running"


def test_parent_takeover_fences_old_chunk_even_with_live_child_lease(sessions):
    attempt_id, chunk_id = _claim(sessions, attempt_seconds=1, chunk_seconds=30)
    with sessions.begin() as db:
        assert HealthRepository(db).claim_attempt(
            attempt_id, "attempt-v1", now=NOW + timedelta(seconds=2),
            lease_seconds=60,
        )
    with sessions.begin() as db:
        repo = HealthRepository(db)
        assert not repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded",
            now=NOW + timedelta(seconds=3),
        )
        assert not repo.finalize_attempt(
            attempt_id, "attempt-v1", 1, "succeeded",
            now=NOW + timedelta(seconds=3),
        )
    with sessions.begin() as db:
        repo = HealthRepository(db)
        later = NOW + timedelta(seconds=31)
        assert repo.claim_chunk(
            chunk_id, "chunk-v2", now=later, lease_seconds=20,
            attempt_lease_token="attempt-v1", attempt_lease_epoch=2,
        )
        assert repo.finalize_chunk(
            chunk_id, "chunk-v2", 2, "succeeded", now=later,
            raw_records=1, records_written=1,
        )
        assert repo.finalize_attempt(
            attempt_id, "attempt-v1", 2, "succeeded", now=later,
        )
    with sessions() as db:
        attempt = db.get(SyncAttempt, attempt_id)
        chunk = db.get(SyncChunk, chunk_id)
        assert attempt.status == "succeeded" and attempt.records_written == 1
        assert chunk.status == "succeeded" and chunk.attempt_count == 2
        assert chunk.stages == {"operation": "synthetic"}


def test_released_parent_cannot_finalize_still_running_chunk(sessions):
    attempt_id, chunk_id = _claim(sessions)
    with sessions.begin() as db:
        assert HealthRepository(db).release_attempt_lease(
            attempt_id, "attempt-v1", 1, now=NOW + timedelta(seconds=1),
        )
    with sessions.begin() as db:
        assert not HealthRepository(db).finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded", now=NOW + timedelta(seconds=2),
        )
    with sessions() as db:
        assert db.get(SyncAttempt, attempt_id).status == "queued"
        assert db.get(SyncChunk, chunk_id).status == "running"


@pytest.mark.parametrize("revocation", ["cancel", "delete_owner", "delete_attempt", "missing_user"])
def test_revoked_or_deleted_owner_cannot_commit_staged_data(sessions, revocation):
    attempt_id, chunk_id = _claim(sessions)
    with sessions.begin() as db:
        repo = HealthRepository(db)
        if revocation == "cancel":
            assert repo.request_sync_cancel(attempt_id, now=NOW + timedelta(seconds=1))
        elif revocation == "delete_owner":
            repo.delete_for_user("owner")
        elif revocation == "delete_attempt":
            db.execute(delete(SyncChunk).where(SyncChunk.id == chunk_id))
            db.execute(delete(SyncAttempt).where(SyncAttempt.id == attempt_id))
        else:
            db.execute(delete(User).where(User.id == "owner"))

    with pytest.raises(StaleSyncLease), sessions.begin() as db:
        repo = HealthRepository(db)
        repo.save_metric_samples([_sample()])
        if not repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded",
            now=NOW + timedelta(seconds=2), raw_records=1, records_written=1,
        ):
            raise StaleSyncLease("stale owner")

    with sessions() as db:
        assert db.execute(select(StoredSample)).scalars().all() == []
        if revocation in {"cancel", "missing_user"}:
            chunk = db.get(SyncChunk, chunk_id)
            assert chunk.status == "running" and chunk.records_written == 0
        if revocation == "delete_owner":
            assert db.get(User, "owner") is None
            assert db.get(SyncAttempt, attempt_id) is None


def test_coordinator_rolls_back_payload_when_finalization_fails(sessions):
    attempt_id, chunk_id = _claim(sessions, attempt_seconds=1)
    later = NOW + timedelta(seconds=2)
    coordinator = ZeppSyncCoordinator(session_factory=sessions, wall_clock=lambda: later)
    claim = _ClaimedChunk(
        SyncLease(chunk_id, "chunk-v1", 1, NOW + timedelta(seconds=60)),
        {"stages": {}, "health_stream": "heart_rate/dense_archive"},
    )
    result = _ChunkResult(
        decoded=SimpleNamespace(files=[], samples=[_sample()]), raw_records=1,
    )
    with pytest.raises(StaleSyncLease):
        coordinator._finalize_success(
            {"id": attempt_id, "user_id": "owner"}, claim, result, None,
        )
    with sessions() as db:
        assert db.execute(select(StoredSample)).scalars().all() == []
        assert db.get(SyncChunk, chunk_id).status == "running"
        assert db.get(SyncAttempt, attempt_id).status == "running"


def test_concurrent_workers_old_owner_cannot_commit_after_takeover(sessions):
    attempt_id, chunk_id = _claim(sessions, attempt_seconds=1, chunk_seconds=30)
    ready, resume = Event(), Event()

    def stale_worker():
        ready.set()
        assert resume.wait(10)
        try:
            with sessions.begin() as db:
                repo = HealthRepository(db)
                repo.save_metric_samples([_sample()])
                if not repo.finalize_chunk(
                    chunk_id, "chunk-v1", 1, "succeeded",
                    now=NOW + timedelta(seconds=3), records_written=1,
                ):
                    raise StaleSyncLease("parent changed")
        except StaleSyncLease:
            return False
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(stale_worker)
        assert ready.wait(10)
        try:
            with sessions.begin() as db:
                assert HealthRepository(db).claim_attempt(
                    attempt_id, "attempt-v2", now=NOW + timedelta(seconds=2),
                    lease_seconds=60,
                )
        finally:
            resume.set()
        assert worker.result(timeout=10) is False

    with sessions() as db:
        assert db.execute(select(StoredSample)).scalars().all() == []
        assert db.get(SyncChunk, chunk_id).status == "running"
        assert db.get(SyncAttempt, attempt_id).lease_epoch == 2


def test_retry_and_partial_outcomes_still_work_under_live_leases(sessions):
    attempt_id, chunk_id = _claim(sessions)
    retry_at = NOW + timedelta(seconds=5)
    with sessions.begin() as db:
        repo = HealthRepository(db)
        assert repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "retry_wait", now=NOW + timedelta(seconds=1),
            next_retry_at=retry_at, error_kind="network",
        )
        assert repo.finalize_attempt(
            attempt_id, "attempt-v1", 1, "retry_wait",
            now=NOW + timedelta(seconds=1), next_retry_at=retry_at,
        )
    with sessions.begin() as db:
        repo = HealthRepository(db)
        assert repo.claim_attempt(attempt_id, "attempt-v2", now=retry_at)
        assert repo.claim_chunk(
            chunk_id, "chunk-v2", now=retry_at,
            attempt_lease_token="attempt-v2", attempt_lease_epoch=2,
        )
        assert repo.finalize_chunk(
            chunk_id, "chunk-v2", 2, "succeeded", now=retry_at,
            raw_records=3, records_written=2,
            stages={"fetch_status": "partial", "parse_status": "success",
                    "write_status": "success", "incomplete": True},
        )
        assert repo.finalize_attempt(
            attempt_id, "attempt-v2", 2, "partial", now=retry_at,
            error_kind="partial_coverage",
        )
    with sessions() as db:
        attempt = db.get(SyncAttempt, attempt_id)
        chunk = db.get(SyncChunk, chunk_id)
        assert attempt.status == "partial" and attempt.records_written == 2
        assert attempt.retry_count == 1 and attempt.completed_count == 1
        assert chunk.stages["incomplete"] is True
        assert "_attempt_lease_fence" not in chunk.stages


def test_reauth_after_source_revoke_fences_old_attempt(sessions):
    with sessions.begin() as db:
        db.add(User(id="owner"))
        repo = HealthRepository(db)
        repo.save_token(AuthToken(
            user_id="owner", source="zepp", source_user_id="vendor-a",
            access_token="synthetic-token",
        ))
        attempt = repo.create_or_reuse_sync_attempt(
            "owner", window_start=NOW - timedelta(days=1), window_end=NOW,
            manifest=[{"stable_key": "hr:0", "stream": "heart_rate"}],
        )
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_attempt(attempt.id, "attempt-v1", now=NOW)
        assert repo.claim_chunk(
            chunk.id, "chunk-v1", now=NOW,
            attempt_lease_token="attempt-v1", attempt_lease_epoch=1,
        )
        assert repo.revoke_source_account("owner")
        attempt_id, chunk_id = attempt.id, chunk.id

    with sessions.begin() as db:
        repo = HealthRepository(db)
        repo.save_token(AuthToken(
            user_id="owner", source="zepp", source_user_id="vendor-a",
            access_token="synthetic-token-reauth",
        ))
        account = repo.source_account("owner", "zepp")
        assert account is not None and account.fence_epoch == 2
        assert not repo.finalize_chunk(
            chunk_id, "chunk-v1", 1, "succeeded", now=NOW + timedelta(seconds=1),
            raw_records=1, records_written=1,
        )
        assert not repo.finalize_attempt(
            attempt_id, "attempt-v1", 1, "succeeded", now=NOW + timedelta(seconds=1),
        )


def test_same_vendor_refresh_cancels_old_attempt_and_fences_terminal_projection(sessions):
    digest = "r" * 64
    with sessions.begin() as db:
        db.add(User(id="refresh-owner"))
        repo = HealthRepository(db)
        repo.save_token(AuthToken(
            user_id="refresh-owner", source="zepp", source_user_id="vendor-a",
            access_token="token-a",
        ))
        repo.create_browser_link(digest, "refresh-owner")
        attempt = repo.create_or_reuse_sync_attempt(
            "refresh-owner", trigger="link_refresh", trigger_ref=digest,
            window_start=NOW - timedelta(days=1), window_end=NOW,
            manifest=[{"stable_key": "hr:0", "stream": "heart_rate"}],
        )
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_attempt(attempt.id, "attempt-v1", now=NOW)
        assert repo.claim_chunk(
            chunk.id, "chunk-v1", now=NOW,
            attempt_lease_token="attempt-v1", attempt_lease_epoch=1,
        )
        account = repo.source_account("refresh-owner", "zepp")
        assert account is not None and account.fence_epoch == 0
        repo.save_token(AuthToken(
            user_id="refresh-owner", source="zepp", source_user_id="vendor-a",
            access_token="token-b",
        ))
        account = repo.source_account("refresh-owner", "zepp")
        assert account is not None and account.fence_epoch == 1
        assert repo.sync_attempt(attempt.id).status == "cancelled"
        assert repo.sync_chunk(attempt.id, chunk.stable_key).status == "cancelled"

    coordinator = ZeppSyncCoordinator(session_factory=sessions, wall_clock=lambda: NOW)
    coordinator._apply_terminal_side_effect(attempt.id, "needs_reauth")
    with sessions() as db:
        link = HealthRepository(db).browser_link(digest)
        assert link is not None
        assert link.status == "connected"
