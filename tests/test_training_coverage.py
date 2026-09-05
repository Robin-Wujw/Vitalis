from datetime import date, datetime, timedelta, timezone

from vitalis.connectors.zepp.client import SPORTS
from vitalis.models import Workout
from vitalis.connectors.zepp.fetcher import FetchWindow
from vitalis.services.zepp_sync_coordinator import stable_chunk_key
from vitalis.storage import HealthRepository, session_scope


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
WINDOW = FetchWindow.local_dates(date(2026, 8, 1), date(2026, 8, 2))


def _attempt(user_id: str, *, missing: str | None = None, future: bool = False):
    with session_scope() as db:
        repo = HealthRepository(db)
        specs = []
        for ordinal, sport in enumerate(SPORTS):
            if sport == missing:
                continue
            specs.append({
                "stable_key": stable_chunk_key(
                    "workouts", sport, WINDOW.start, WINDOW.end, int(WINDOW.end.timestamp())
                ),
                "stream": "workouts",
                "partition": sport,
                "ordinal": ordinal,
                "window_start": WINDOW.start,
                "window_end": WINDOW.end,
                "cursor": int(WINDOW.end.timestamp()),
                "allow_unavailable": True,
                "stages": {
                    "fetch_status": "success",
                    "parse_status": "success",
                    "write_status": "success",
                },
            })
        attempt = repo.create_or_reuse_sync_attempt(
            user_id,
            window_start=WINDOW.start,
            window_end=WINDOW.end,
            manifest=specs,
        )
        attempt.status = "succeeded"
        attempt.finished_at = (
            datetime(2026, 8, 31, tzinfo=timezone.utc).replace(tzinfo=None)
            if future else NOW.replace(tzinfo=None)
        )
        for row in repo.sync_chunks(attempt.id):
            row.status = "succeeded"
            row.fetch_status = "success"
            row.parse_status = "success"
            row.write_status = "success"
            row.finished_at = attempt.finished_at
        return attempt.id


def test_training_history_coverage_requires_all_sports_in_one_attempt():
    _attempt("coverage-complete")
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-complete", date(2026, 8, 1), date(2026, 8, 2), NOW
        )
    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]
    assert result["last_synced_at"] == "2026-08-30T12:00:00Z"


def test_training_history_coverage_is_partial_without_required_partition():
    _attempt("coverage-partial", missing=SPORTS[-1])
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-partial", date(2026, 8, 1), date(2026, 8, 2), NOW
        )
    assert result["status"] == "PARTIAL"
    assert result["verified_days"] == []
    assert result["limitations"]


def test_training_history_coverage_excludes_future_attempts_and_other_users():
    _attempt("coverage-future", future=True)
    _attempt("coverage-other")
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-future", date(2026, 8, 1), date(2026, 8, 2), NOW
        )
    assert result["status"] == "UNKNOWN"
    assert result["verified_days"] == []
    assert result["last_synced_at"] is None


def test_workout_detail_refresh_prefers_backlog_and_records_fetched_at():
    user_id = "detail-refresh-contract"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="old-schema",
            started_at=NOW - timedelta(days=2),
            duration=30,
            training_family="strength",
            vendor_source="strength",
        ))
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="stale-cache",
            started_at=NOW - timedelta(days=1),
            duration=30,
            training_family="strength",
            vendor_source="strength",
        ))
        repo.save_workout_detail(
            user_id, "stale-cache", {"schema_version": "4.0"},
            fetched_at=NOW - timedelta(days=2),
        )
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id="run-new",
            started_at=NOW,
            duration=20,
            training_family="aerobic",
            vendor_source="run",
        ))
        for index in range(40):
            fresh_id = f"fresh-{index}"
            repo.save_workout(Workout(
                user_id=user_id,
                workout_id=fresh_id,
                started_at=NOW + timedelta(minutes=index + 1),
                duration=10,
                training_family="aerobic",
                vendor_source="run",
            ))
            repo.save_workout_detail(
                user_id, fresh_id, {"schema_version": "4.0"}, fetched_at=NOW
            )
        backlog_only = repo.pending_workout_details(
            user_id,
            NOW - timedelta(days=28),
            NOW + timedelta(days=2),
            limit=2,
        )
        assert [row.workout_id for row in backlog_only] == ["run-new", "old-schema"]
        rows = repo.pending_workout_details(
            user_id,
            NOW - timedelta(days=28),
            NOW + timedelta(days=1),
            limit=2,
            refresh_after=NOW - timedelta(days=1),
            strength_only=True,
        )
        assert [row.workout_id for row in rows] == ["old-schema", "stale-cache"]
        mixed = repo.pending_workout_details(
            user_id,
            NOW - timedelta(days=28),
            NOW + timedelta(days=1),
            limit=3,
            refresh_after=NOW - timedelta(days=1),
            exclude_workout_ids={"old-schema"},
        )
        assert [row.workout_id for row in mixed] == ["run-new", "stale-cache"]
        assert repo.save_workout_detail(
            user_id, "old-schema", {"schema_version": "4.0"}, fetched_at=NOW
        )
        saved = repo.workout(user_id, "old-schema")
        assert saved is not None
        assert saved.detail["fetched_at"] == "2026-08-30T12:00:00Z"


def test_detail_refresh_rotates_to_least_recently_fetched_history():
    user_id = "refresh-rotation"
    with session_scope() as db:
        repo = HealthRepository(db)
        for offset in (1, 2, 3):
            workout_id = f"session-{offset}"
            repo.save_workout(Workout(
                user_id=user_id, workout_id=workout_id,
                started_at=NOW - timedelta(days=offset), duration=30,
                training_family="strength", vendor_source="strength",
            ))
            repo.save_workout_detail(
                user_id, workout_id, {"schema_version": "4.0"},
                fetched_at=NOW - timedelta(hours=offset),
            )
        rows = repo.pending_workout_details(
            user_id, NOW - timedelta(days=28), NOW, limit=1, refresh_after=NOW,
        )
        assert rows[0].workout_id == "session-3"
        repo.save_workout_detail(user_id, "session-3", {"schema_version": "4.0"}, fetched_at=NOW)
        rows = repo.pending_workout_details(
            user_id, NOW - timedelta(days=28), NOW, limit=1,
            refresh_after=NOW + timedelta(minutes=1),
        )
        assert rows[0].workout_id == "session-2"


def test_future_successor_cannot_be_omitted_to_prove_coverage():
    user_id = "coverage-future-successor"
    attempt_id = _attempt(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.sync_chunks(attempt_id)[-1].finished_at = (NOW + timedelta(days=1)).replace(tzinfo=None)
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] != "COMPLETE"
    assert result["verified_days"] == []
