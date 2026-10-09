"""Synthetic fairness and ownership tests for workout-detail backfill selection."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.domain import WORKOUT_DETAIL_SCHEMA_VERSION, Workout


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
WINDOW_START = NOW - timedelta(days=30)
WINDOW_END = NOW + timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'detail-fairness.db').as_posix()}"
    )
    init_db(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.rollback()
    finally:
        engine.dispose()


def _workout(repo, user_id, workout_id, started_at, *, source="zepp"):
    repo.save_workout(Workout(
        user_id=user_id,
        source=source,
        workout_id=workout_id,
        started_at=started_at,
        duration=30,
        training_family="strength",
        vendor_source=f"{source}-detail",
    ))


def _record_detail_attempt(
    repo,
    user_id,
    workout_id,
    finished_at,
    *,
    source="zepp",
    partition=None,
    status="failed",
    chunk_status=None,
):
    partition = partition or f"{source}:{workout_id}"
    attempt = repo.create_or_reuse_sync_attempt(
        user_id,
        source=source,
        trigger="manual",
        trigger_ref=f"synthetic:{user_id}:{source}:{workout_id}:{finished_at.isoformat()}",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        options={"detail_only": True, "source_mode": "mock"},
        manifest=[{
            "stable_key": f"detail:{partition}:{finished_at.isoformat()}",
            "stream": "workout_detail",
            "partition": partition,
        }],
    )
    chunk = repo.sync_chunks(attempt.id)[0]
    started_at = finished_at - timedelta(minutes=1)
    attempt.created_at = finished_at - timedelta(minutes=2)
    attempt.started_at = started_at
    attempt.finished_at = finished_at if status != "queued" else None
    attempt.status = status
    attempt.updated_at = finished_at
    chunk.started_at = started_at
    chunk.finished_at = finished_at if chunk_status != "queued" else None
    chunk.status = chunk_status or status
    chunk.updated_at = finished_at
    return attempt, chunk


def test_bounded_backfill_drains_never_attempted_details_before_newest_failures(db):
    repo = HealthRepository(db)
    user_id = "detail-fairness-owner"
    repo.upsert_user(user_id)

    # The newest four have already failed permanently. Older rows remain eligible
    # and must be reached before retrying those newer rows.
    for index in range(4):
        workout_id = f"failed-new-{index}"
        _workout(repo, user_id, workout_id, NOW - timedelta(hours=index + 1))
        _record_detail_attempt(
            repo,
            user_id,
            workout_id,
            NOW - timedelta(hours=4 - index),
        )
    for index in range(8):
        workout_id = f"older-pending-{index}"
        _workout(repo, user_id, workout_id, NOW - timedelta(hours=index + 10))

    first = repo.pending_workout_details(
        user_id, WINDOW_START, WINDOW_END, limit=4,
    )
    assert [row.workout_id for row in first] == [
        "older-pending-0",
        "older-pending-1",
        "older-pending-2",
        "older-pending-3",
    ]

    # A bounded attempt can fail again; the next bounded attempt still advances
    # through never-attempted rows rather than being trapped on the newest four.
    for index, row in enumerate(first):
        _record_detail_attempt(
            repo,
            user_id,
            row.workout_id,
            NOW - timedelta(minutes=20 + index),
        )
    second = repo.pending_workout_details(
        user_id, WINDOW_START, WINDOW_END, limit=4,
    )
    assert [row.workout_id for row in second] == [
        "older-pending-4",
        "older-pending-5",
        "older-pending-6",
        "older-pending-7",
    ]

    # Once every row has history, the least recently attempted group rotates in.
    for index, row in enumerate(second):
        _record_detail_attempt(
            repo,
            user_id,
            row.workout_id,
            NOW - timedelta(minutes=30 + index),
        )
    rotated = repo.pending_workout_details(
        user_id, WINDOW_START, WINDOW_END, limit=4,
    )
    assert [row.workout_id for row in rotated] == [
        "failed-new-0",
        "failed-new-1",
        "failed-new-2",
        "failed-new-3",
    ]


def test_active_detail_chunks_are_not_reselected(db):
    repo = HealthRepository(db)
    user_id = "detail-active-owner"
    repo.upsert_user(user_id)
    _workout(repo, user_id, "queued-detail", NOW - timedelta(hours=1))
    _workout(repo, user_id, "running-detail", NOW - timedelta(hours=2))
    _workout(repo, user_id, "available-detail", NOW - timedelta(hours=3))
    _record_detail_attempt(
        repo, user_id, "queued-detail", NOW - timedelta(minutes=2),
        status="queued", chunk_status="queued",
    )
    _record_detail_attempt(
        repo, user_id, "running-detail", NOW - timedelta(minutes=1),
        status="running", chunk_status="running",
    )

    rows = repo.pending_workout_details(user_id, WINDOW_START, WINDOW_END, limit=10)
    assert [row.workout_id for row in rows] == ["available-detail"]


def test_detail_history_isolated_by_user_source_and_account_epoch(db):
    repo = HealthRepository(db)
    user_id = "detail-isolation-owner"
    other_user = "detail-isolation-other-user"
    repo.upsert_user(user_id)
    repo.upsert_user(other_user)
    _workout(repo, user_id, "isolated", NOW - timedelta(hours=1))
    _workout(repo, user_id, "locally-attempted", NOW - timedelta(hours=2))

    # Same partition, but a different user and source, must not count as this
    # user's detail history.
    _record_detail_attempt(repo, other_user, "isolated", NOW - timedelta(hours=3))
    _record_detail_attempt(
        repo, user_id, "isolated", NOW - timedelta(hours=4),
        source="other", partition="zepp:isolated",
    )
    _record_detail_attempt(repo, user_id, "locally-attempted", NOW - timedelta(hours=5))
    rows = repo.pending_workout_details(user_id, WINDOW_START, WINDOW_END, limit=1)
    assert [row.workout_id for row in rows] == ["isolated"]

    # Re-pairing advances the source-account fence. History from the old epoch
    # must not bias selection for the newly active account.
    account_user = "detail-isolation-account"
    repo.upsert_user(account_user)
    repo.ensure_source_account(account_user, "zepp", "synthetic-account")
    _workout(repo, account_user, "repaired", NOW - timedelta(hours=1))
    _workout(repo, account_user, "current-history", NOW - timedelta(hours=2))
    _record_detail_attempt(repo, account_user, "repaired", NOW - timedelta(hours=4))
    repo.revoke_source_account(account_user, now=NOW - timedelta(hours=3))
    repo.ensure_source_account(account_user, "zepp", "synthetic-account")
    _record_detail_attempt(repo, account_user, "current-history", NOW - timedelta(hours=1))
    rows = repo.pending_workout_details(account_user, WINDOW_START, WINDOW_END, limit=1)
    assert [row.workout_id for row in rows] == ["repaired"]


def test_current_detail_refresh_cutoff_converges_after_each_fetch(db):
    repo = HealthRepository(db)
    user_id = "detail-refresh-convergence"
    repo.upsert_user(user_id)
    for index in range(3):
        workout_id = f"refresh-{index}"
        _workout(repo, user_id, workout_id, NOW - timedelta(days=index + 1))
        repo.save_workout_detail(
            user_id,
            workout_id,
            {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION},
            fetched_at=NOW - timedelta(days=index + 2),
        )

    for expected in ("refresh-2", "refresh-1", "refresh-0"):
        rows = repo.pending_workout_details(
            user_id,
            WINDOW_START,
            NOW,
            limit=1,
            refresh_after=NOW - timedelta(days=1),
        )
        assert [row.workout_id for row in rows] == [expected]
        assert repo.save_workout_detail(
            user_id,
            expected,
            {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION},
            fetched_at=NOW,
        )

    assert repo.pending_workout_details(
        user_id,
        WINDOW_START,
        NOW,
        limit=1,
        refresh_after=NOW - timedelta(days=1),
    ) == []
