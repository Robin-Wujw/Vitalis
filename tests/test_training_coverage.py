from datetime import date, datetime, timedelta, timezone

from vitalis.connectors.zepp.client import LEGACY_SPORTS, SPORTS
from vitalis.models import MetricSample, Workout, WORKOUT_DETAIL_SCHEMA_VERSION
from vitalis.connectors.zepp.fetcher import FetchWindow
from vitalis.services.zepp_sync_coordinator import PLAN_VERSION, stable_chunk_key
from vitalis.storage import HealthRepository, session_scope


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
WINDOW = FetchWindow.local_dates(date(2026, 8, 1), date(2026, 8, 2))


def _attempt(
    user_id: str, *, missing: str | None = None, future: bool = False,
    plan_version: str = PLAN_VERSION,
):
    with session_scope() as db:
        repo = HealthRepository(db)
        specs = []
        required = SPORTS if plan_version == PLAN_VERSION else LEGACY_SPORTS
        for ordinal, sport in enumerate(required):
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
            plan_version=plan_version,
            manifest=specs,
        )
        attempt.created_at = (NOW - timedelta(minutes=2)).replace(tzinfo=None)
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


def test_training_history_coverage_accepts_complete_aggregate_feed():
    _attempt("coverage-complete")
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-complete", date(2026, 8, 1), date(2026, 8, 2), NOW
        )
    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]
    assert result["last_synced_at"] == "2026-08-30T12:00:00Z"


def test_legacy_training_history_still_requires_all_thirteen_partitions():
    _attempt("coverage-partial", missing=LEGACY_SPORTS[-1], plan_version="zepp-sync-v5")
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-partial", date(2026, 8, 1), date(2026, 8, 2), NOW
        )
    assert result["status"] == "PARTIAL"
    assert result["verified_days"] == []
    assert result["limitations"]


def test_legacy_complete_history_keeps_its_original_proof():
    _attempt("coverage-legacy-complete", plan_version="zepp-sync-v5")
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            "coverage-legacy-complete", date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "COMPLETE"


def test_old_unavailable_sport_paths_are_not_reinterpreted_as_empty():
    user_id = "coverage-old-unavailable"
    attempt_id = _attempt(user_id, plan_version="zepp-sync-v5")
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(attempt_id):
            if chunk.partition != "run":
                chunk.status = "unavailable"
                chunk.fetch_status = "unavailable"
                chunk.parse_status = "not_run"
                chunk.write_status = "not_run"

    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "PARTIAL"
    assert result["verified_days"] == []


def test_new_complete_feed_proves_history_despite_old_unavailable_paths():
    user_id = "coverage-new-after-old"
    old_id = _attempt(user_id, plan_version="zepp-sync-v5")
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(old_id):
            if chunk.partition != "run":
                chunk.status = "unavailable"
                chunk.fetch_status = "unavailable"
                chunk.parse_status = "not_run"
                chunk.write_status = "not_run"
    _attempt(user_id)

    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]


def test_aggregate_fetch_before_a_day_ends_cannot_later_prove_that_day():
    user_id = "coverage-intraday"
    attempt_id = _attempt(user_id)
    day_one_end = WINDOW.start + timedelta(days=1)
    with session_scope() as db:
        repo = HealthRepository(db)
        attempt = repo.sync_attempt(attempt_id)
        assert attempt is not None
        attempt.created_at = (day_one_end + timedelta(hours=1)).replace(tzinfo=None)
        chunk, = repo.sync_chunks(attempt_id)
        chunk.started_at = attempt.created_at

    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "PARTIAL"
    assert result["verified_days"] == ["2026-08-01"]
    assert any("当天结束前" in note for note in result["limitations"])


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


def test_metric_window_summaries_aggregate_minutes_and_provenance_in_sql():
    user_id = "metric-window-summary"
    samples = []
    for index in range(120):
        samples.append(MetricSample(
            user_id=user_id,
            source="zepp",
            metric="heart_rate",
            timestamp=NOW - timedelta(minutes=119 - index, seconds=30),
            value=60 + index % 10,
            unit="bpm",
            source_scope="device",
            device_id="watch-a",
        ))
    samples.append(MetricSample(
        user_id=user_id,
        source="zepp",
        metric="stress",
        timestamp=NOW - timedelta(minutes=5),
        value=25,
        unit="score",
        source_scope="user_fused",
        device_id=None,
    ))
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.save_metric_samples(samples)
        rows = repo.metric_window_summaries(
            user_id,
            ("heart_rate", "stress"),
            NOW - timedelta(hours=2),
            NOW + timedelta(minutes=1),
        )

    heart_rate = next(item for item in rows if item["metric"] == "heart_rate")
    stress = next(item for item in rows if item["metric"] == "stress")
    assert heart_rate["sample_count"] == 120
    assert heart_rate["observed_minutes"] == 120
    assert heart_rate["minimum"] == 60
    assert heart_rate["maximum"] == 69
    assert heart_rate["truncated"] is False
    assert stress["source_scope"] == "user_fused"
    assert stress["device_id"] is None


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
            user_id, "stale-cache", {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION},
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
                user_id, fresh_id, {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION}, fetched_at=NOW
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
            user_id, "old-schema", {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION}, fetched_at=NOW
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
                user_id, workout_id, {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION},
                fetched_at=NOW - timedelta(hours=offset),
            )
        rows = repo.pending_workout_details(
            user_id, NOW - timedelta(days=28), NOW, limit=1, refresh_after=NOW,
        )
        assert rows[0].workout_id == "session-3"
        repo.save_workout_detail(user_id, "session-3", {"schema_version": WORKOUT_DETAIL_SCHEMA_VERSION}, fetched_at=NOW)
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


def test_training_history_coverage_reaches_old_proof_after_more_than_64_attempts():
    user_id = "coverage-old-proof"
    proof_id = _attempt(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        proof = repo.sync_attempt(proof_id)
        assert proof is not None
        proof.finished_at = (NOW - timedelta(days=2)).replace(tzinfo=None)
        for chunk in repo.sync_chunks(proof_id):
            chunk.finished_at = proof.finished_at
    for _ in range(64):
        _attempt(user_id)

    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW
        )

    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]
    assert result["budget_exhausted"] is False


def test_many_detail_only_attempts_do_not_hide_older_workout_proof():
    user_id = "coverage-detail-only-budget"
    proof_id = _attempt(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        proof = repo.sync_attempt(proof_id)
        assert proof is not None
        proof.finished_at = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        for chunk in repo.sync_chunks(proof_id):
            chunk.finished_at = proof.finished_at
        for index in range(260):
            attempt = repo.create_or_reuse_sync_attempt(
                user_id, plan_version=PLAN_VERSION,
                options={"detail_only": True, "batch": index},
                window_start=WINDOW.start, window_end=WINDOW.end,
                manifest=[{
                    "stable_key": f"detail-only-{index}",
                    "stream": "workout_detail", "partition": f"zepp:{index}",
                    "ordinal": 0, "window_start": WINDOW.start,
                    "window_end": WINDOW.end,
                }],
            )
            attempt.status = "succeeded"
            attempt.finished_at = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
            detail_chunk, = repo.sync_chunks(attempt.id)
            detail_chunk.status = "succeeded"
            detail_chunk.finished_at = attempt.finished_at
            db.flush()

    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]
    assert result["budget_exhausted"] is False


def test_successful_empty_workout_queries_prove_coverage_but_unavailable_does_not():
    user_id = "coverage-confirmed-empty"
    attempt_id = _attempt(user_id)
    with session_scope() as db:
        repo = HealthRepository(db)
        for chunk in repo.sync_chunks(attempt_id):
            chunk.parse_status = "empty"
            chunk.write_status = "not_run"
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] == "COMPLETE"
    assert result["verified_days"] == ["2026-08-01", "2026-08-02"]
    with session_scope() as db:
        chunk = HealthRepository(db).sync_chunks(attempt_id)[0]
        chunk.status = "unavailable"
        chunk.fetch_status = "unavailable"
        chunk.parse_status = "not_run"
    with session_scope() as db:
        result = HealthRepository(db).training_history_coverage(
            user_id, date(2026, 8, 1), date(2026, 8, 2), NOW,
        )
    assert result["status"] != "COMPLETE"
    assert result["verified_days"] == []
