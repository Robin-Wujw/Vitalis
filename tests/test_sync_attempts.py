"""Durable synchronization ledger tests."""

from datetime import datetime, timedelta, timezone

from vitalis.storage import HealthRepository, init_db, session_scope


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _attempt(repo, user_id, *, manifest=None, trigger="manual"):
    return repo.create_or_reuse_sync_attempt(
        user_id,
        trigger=trigger,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        timezone_name="Asia/Shanghai",
        options={"decode_dense_files": False},
        manifest=manifest,
    )


def test_interrupted_pairing_claim_can_be_recovered_after_processing_lease():
    init_db()
    pairing_id = "pairing-processing-lease"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("pairing-lease-user")
        repo.create_pairing_session(
            pairing_id, "pairing-lease-user",
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        assert repo.claim_pairing_session(pairing_id, processing_lease_seconds=120)

    with session_scope() as db:
        repo = HealthRepository(db)
        assert not repo.claim_pairing_session(pairing_id, processing_lease_seconds=120)
        row = repo.pairing_session(pairing_id)
        assert row is not None
        row.processing_started_at = datetime.utcnow() - timedelta(seconds=121)

    with session_scope() as db:
        assert HealthRepository(db).claim_pairing_session(
            pairing_id, processing_lease_seconds=120
        )


def test_stable_manifest_insert_is_idempotent():
    init_db()
    manifest = [
        {"stable_key": "hr:0", "stream": "heart_rate", "ordinal": 0},
        {"stable_key": "hr:1", "stream": "heart_rate", "ordinal": 1},
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-manifest")
        first = _attempt(repo, "ledger-manifest", manifest=manifest)
        second = _attempt(repo, "ledger-manifest", manifest=manifest)
        chunks = repo.sync_chunks(first.id)

    assert first.id == second.id
    assert len(chunks) == 2
    assert [chunk.stable_key for chunk in chunks] == ["hr:0", "hr:1"]
    assert first.chunk_count == 2


def test_detail_only_reuse_does_not_append_a_new_manifest_chunk():
    init_db()
    manifest = [
        {"stable_key": "detail:one", "stream": "workout_detail", "partition": "one"},
        {"stable_key": "detail:two", "stream": "workout_detail", "partition": "two"},
    ]
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-detail-freeze")
        first = repo.create_or_reuse_sync_attempt(
            "ledger-detail-freeze", window_start=NOW - timedelta(days=1), window_end=NOW,
            options={"detail_only": True, "detail_limit": 1}, manifest=manifest[:1],
        )
        first_chunk = repo.sync_chunks(first.id)[0]
        first_chunk.status = "succeeded"
        first_chunk.fetch_status = "success"
        first_chunk.parse_status = "success"
        first_chunk.write_status = "success"
        reused = repo.create_or_reuse_sync_attempt(
            "ledger-detail-freeze", window_start=NOW - timedelta(days=1), window_end=NOW,
            options={"detail_only": True, "detail_limit": 1}, manifest=manifest[1:],
        )
        explicit = repo.create_or_reuse_sync_attempt(
            "ledger-detail-freeze", window_start=NOW - timedelta(days=1), window_end=NOW,
            options={"detail_only": True, "detail_limit": 1},
            manifest=manifest[1:], attempt_id=first.id,
        )
        assert reused.id == explicit.id == first.id
        assert [chunk.stable_key for chunk in repo.sync_chunks(first.id)] == ["detail:one"]
        next_limit = repo.create_or_reuse_sync_attempt(
            "ledger-detail-freeze", window_start=NOW - timedelta(days=1), window_end=NOW,
            options={"detail_only": True, "detail_limit": 2}, manifest=manifest[1:],
        )
        assert next_limit.id != first.id
        assert [chunk.stable_key for chunk in repo.sync_chunks(next_limit.id)] == ["detail:two"]


def test_detail_only_integrity_race_cannot_extend_manifest(monkeypatch):
    init_db()
    user_id = "ledger-detail-race"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        options = {"detail_only": True, "detail_limit": 1}
        kwargs = dict(
            window_start=NOW - timedelta(days=1), window_end=NOW,
            options=options,
        )
        first = repo.create_or_reuse_sync_attempt(
            user_id, **kwargs,
            manifest=[{"stable_key": "detail:one", "stream": "workout_detail"}],
        )
        original_execute = db.execute
        missed = False

        class EmptyResult:
            def scalars(self):
                return self

            def first(self):
                return None

        def hide_active_once(statement, *args, **kwargs):
            nonlocal missed
            if not missed and "sync_attempts.request_key" in str(statement):
                missed = True
                return EmptyResult()
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", hide_active_once)
        reused = repo.create_or_reuse_sync_attempt(
            user_id, **kwargs,
            manifest=[{"stable_key": "detail:two", "stream": "workout_detail"}],
        )
        assert missed is True
        assert reused.id == first.id
        assert len(repo.sync_attempts(user_id)) == 1
        assert [chunk.stable_key for chunk in repo.sync_chunks(first.id)] == ["detail:one"]


def test_identical_attempt_is_reused_and_different_request_is_queued():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-active")
        first = _attempt(repo, "ledger-active")
        reused = _attempt(repo, "ledger-active")
        queued = _attempt(repo, "ledger-active", trigger="scheduled")
        assert reused.id == first.id
        assert queued.id != first.id
        assert len(repo.sync_attempts("ledger-active")) == 2
        assert repo.claim_attempt(first.id, "attempt-token", now=NOW)
        assert not repo.claim_attempt(queued.id, "other-token", now=NOW)


def test_double_chunk_claim_and_expired_takeover_are_conditional():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-claim")
        attempt = _attempt(repo, "ledger-claim", manifest=[
            {"stable_key": "one", "stream": "heart_rate"},
        ])
        assert repo.claim_attempt(attempt.id, "attempt-token", now=NOW)
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_chunk(chunk.id, "chunk-token", now=NOW, lease_seconds=10)
        assert not repo.claim_chunk(chunk.id, "other-token", now=NOW)
        assert repo.claim_chunk(
            chunk.id, "takeover-token", now=NOW + timedelta(seconds=11), lease_seconds=10
        )
        assert chunk.lease_epoch == 2


def test_stale_attempt_owner_cannot_claim_another_chunk_after_takeover():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-attempt-fence")
        attempt = _attempt(repo, "ledger-attempt-fence", manifest=[
            {"stable_key": "one", "stream": "heart_rate"},
            {"stable_key": "two", "stream": "sleep"},
        ])
        assert repo.claim_attempt(
            attempt.id, "old-attempt", now=NOW, lease_seconds=1
        )
        old_epoch = attempt.lease_epoch
        assert repo.claim_attempt(
            attempt.id, "new-attempt", now=NOW + timedelta(seconds=2),
            lease_seconds=30,
        )
        chunk = repo.sync_chunks(attempt.id)[0]
        assert not repo.claim_sync_chunk(
            chunk.id, "old-chunk", now=NOW + timedelta(seconds=2),
            attempt_lease_token="old-attempt", attempt_lease_epoch=old_epoch,
        )


def test_stale_token_finalize_is_rejected_and_retry_is_counted():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-retry")
        attempt = _attempt(repo, "ledger-retry", manifest=[
            {"stable_key": "one", "stream": "heart_rate"},
        ])
        assert repo.claim_attempt(attempt.id, "attempt-token", now=NOW)
        chunk = repo.sync_chunks(attempt.id)[0]
        assert repo.claim_chunk(chunk.id, "chunk-token", now=NOW)
        assert repo.finalize_chunk(
            chunk.id, "stale-token", 1, "succeeded", raw_records=1, records_written=1
        ) is False
        retry_at = NOW + timedelta(minutes=5)
        assert repo.finalize_chunk(
            chunk.id, "chunk-token", 1, "retry_wait", next_retry_at=retry_at,
            error_kind="network", error="temporary",
        )
        assert chunk.attempt_count == 1
        assert chunk.next_retry_at == retry_at.replace(tzinfo=None)
        assert repo.claim_chunk(chunk.id, "chunk-token-2", now=NOW) is False
        assert repo.claim_chunk(chunk.id, "chunk-token-2", now=retry_at)
        assert chunk.attempt_count == 2


def test_specific_finalize_helpers_cover_success_and_failure():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-finalize")
        success = _attempt(repo, "ledger-finalize")
        assert repo.claim_attempt(success.id, "success-token", now=NOW)
        assert repo.finalize_sync_attempt_success(
            success.id, "success-token", 1, now=NOW
        )
        assert success.status == "succeeded"

        failure = _attempt(repo, "ledger-finalize", manifest=[
            {"stable_key": "one", "stream": "heart_rate"},
        ])
        assert repo.claim_attempt(failure.id, "failure-attempt-token", now=NOW)
        chunk = repo.sync_chunks(failure.id)[0]
        assert repo.claim_chunk(chunk.id, "failure-chunk-token", now=NOW)
        assert repo.finalize_sync_chunk_failure(
            chunk.id, "failure-chunk-token", 1, error_kind="network", error="offline"
        )
        assert repo.finalize_sync_attempt_failure(
            failure.id, "failure-attempt-token", 1, error_kind="network", error="offline"
        )
        assert failure.status == "failed"


def test_cancel_aggregate_projection_isolation_and_delete_for_user():
    init_db()
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user("ledger-lifecycle")
        first = _attempt(repo, "ledger-lifecycle", manifest=[
            {"stable_key": "one", "stream": "heart_rate", "allow_unavailable": True},
        ])
        assert repo.claim_attempt(first.id, "a-token", now=NOW)
        chunk = repo.sync_chunks(first.id)[0]
        assert repo.claim_chunk(chunk.id, "c-token", now=NOW)
        assert repo.finalize_chunk(
            chunk.id, "c-token", 1, "unavailable", error_kind="not_available"
        )
        assert repo.finalize_attempt(first.id, "a-token", 1, "succeeded")
        aggregate = repo.aggregate_sync_attempt(first.id)
        assert aggregate.unavailable_chunks == 1
        assert aggregate.completed_count == 1
        assert aggregate.complete

        second = _attempt(repo, "ledger-lifecycle")
        repo.save_sync_stream_state(
            "ledger-lifecycle", "heart_rate", fetch_status="success",
            parse_status="success", write_status="success", fetched_at=NOW,
            parsed_at=NOW, written_at=NOW, raw_records=2, records_written=2,
            attempt_id=second.id,
        )
        repo.save_sync_stream_state(
            "ledger-lifecycle", "heart_rate", fetch_status="failed",
            parse_status="not_run", write_status="not_run", fetched_at=NOW,
            parsed_at=None, written_at=None, raw_records=0, records_written=0,
            attempt_id=first.id,
        )
        state = repo.sync_stream_states("ledger-lifecycle")[0]
        assert state.attempt_id == second.id
        assert repo.request_cancel(second.id, now=NOW)
        assert second.status == "queued"
        assert second.cancel_requested_at == NOW.replace(tzinfo=None)
        assert repo.cancel_sync_attempt(second.id, now=NOW)
        assert second.status == "cancelled"

        repo.delete_for_user("ledger-lifecycle")
        assert repo.sync_attempts("ledger-lifecycle") == []
        assert repo.sync_stream_states("ledger-lifecycle") == []
        assert repo.sync_chunks(first.id) == []
