"""Durable Zepp coordinator tests; all connectors are local fakes."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vitalis.config import settings
from vitalis.connectors.zepp.client import SPORTS, ZeppAuthError
from vitalis.connectors.zepp.fetcher import (
    DAY_MILLISECONDS,
    FetchWindow,
    FetchedRecord,
    RawRecord,
)
from vitalis.models import User
from vitalis.services.zepp_sync_coordinator import (
    SyncControl,
    ZeppSyncCoordinator,
    _ChunkResult,
    stable_chunk_key,
)
from vitalis.storage import HealthRepository, init_db, session_scope
from vitalis.storage.database import SessionLocal
from vitalis.storage import models as orm


NOW = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
WINDOW = FetchWindow.local_dates(date(2026, 8, 1), date(2026, 8, 1))


class HeartConnector:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error
        self.calls = []

    def fetch_heart_rate(self, start, end, limit, hr_type):
        self.calls.append((start, end, limit, hr_type))
        if self.error:
            raise self.error
        return self.pages.pop(0) if self.pages else {"items": []}


class WorkoutConnector:
    def fetch_sport_history(self, sport, start, stop, need_sub_data):
        return {"data": {"items": [{
            "trackid": "w-1", "type": 1,
            "start_time": "2026-08-01T07:00:00Z",
            "end_time": "2026-08-01T08:00:00Z",
            "source": "run", "distance": 1000,
        }], "next": -1}}

    def fetch_sport_detail(self, workout_id, source):
        return {"data": {"trackid": workout_id, "source": source}}


def _clean(user_id):
    init_db()
    with session_scope() as db:
        HealthRepository(db).delete_for_user(user_id)


def _one_chunk_attempt(user_id, connector, *, options=None, now=NOW, deadline=None, trigger="manual"):
    coordinator = ZeppSyncCoordinator(
        connector=connector,
        wall_clock=lambda: now,
        random_fn=lambda: 0.0,
    )
    attempt = coordinator.create_attempt(
        user_id, window=WINDOW, options=options or {}, deadline_at=deadline,
        trigger=trigger,
    )
    with session_scope() as db:
        rows = HealthRepository(db).sync_chunks(attempt.id)
        for row in rows[1:]:
            row.status = "succeeded"
    return coordinator, attempt


def test_manifest_is_stable_and_request_is_reused():
    _clean("coord-manifest")
    coordinator = ZeppSyncCoordinator(wall_clock=lambda: NOW)
    first = coordinator.create_attempt("coord-manifest", window=WINDOW, options={"decode_dense_files": False})
    second = coordinator.create_attempt("coord-manifest", window=WINDOW, options={"decode_dense_files": False})
    assert first.id == second.id
    assert first.chunk_count == 31
    rows = coordinator.status(first.id)["chunks"]
    assert rows[0]["stable_key"] == stable_chunk_key(
        "heart_rate", "minute", WINDOW.start, WINDOW.end, int(WINDOW.start.timestamp())
    )
    assert all("operation" in row["stages"] for row in rows)
    with session_scope() as db:
        chunks = HealthRepository(db).sync_chunks(first.id)
        stress = next(row for row in chunks if row.partition == "all_day_stress")
        params = stress.stages["params"]
        assert params["from_ms"] == int(WINDOW.start.timestamp() * 1000) - DAY_MILLISECONDS
        assert params["to_ms"] == int(WINDOW.end.timestamp() * 1000) + DAY_MILLISECONDS


def test_page_success_creates_atomic_successor():
    _clean("coord-page")
    base = int(WINDOW.start.timestamp())
    page = {"items": [{"timestamp": base + i, "value": 70} for i in range(1000)]}
    connector = HeartConnector([page])
    coordinator, attempt = _one_chunk_attempt("coord-page", connector)
    report = coordinator.run_attempt(attempt.id, max_chunks=1)
    state = coordinator.status(attempt.id)
    assert report.progress["succeeded_chunks"] == 31
    assert len(state["chunks"]) == 32
    successors = [row for row in state["chunks"] if row["status"] == "queued"]
    assert len(successors) == 1
    assert successors[0]["stream"] == "heart_rate"


def test_completed_chunk_is_committed_when_later_chunk_fails():
    _clean("coord-isolation")
    connector = HeartConnector([{"items": [{"timestamp": int(WINDOW.start.timestamp()), "value": 70}]}])
    coordinator = ZeppSyncCoordinator(connector=connector, wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt("coord-isolation", window=WINDOW)
    with session_scope() as db:
        rows = HealthRepository(db).sync_chunks(attempt.id)
        # Keep heart_rate and sleep; mark the rest complete.
        for row in rows[2:]:
            row.status = "succeeded"

    class FailingSleep(HeartConnector):
        def fetch_band_data(self, *args):
            raise ZeppAuthError("bad payload", kind="vendor_response")

    connector.__class__ = FailingSleep
    report = coordinator.run_attempt(attempt.id)
    state = coordinator.status(attempt.id)
    assert state["attempt"]["status"] == "failed"
    assert state["chunks"][0]["status"] == "succeeded"
    assert not report.success


def test_retry_backoff_and_auth_are_terminally_classified():
    _clean("coord-retry")
    connector = HeartConnector(error=ZeppAuthError("offline", kind="network"))
    coordinator, attempt = _one_chunk_attempt("coord-retry", connector)
    coordinator.run_attempt(attempt.id, max_chunks=1)
    row = coordinator.status(attempt.id)["chunks"][0]
    assert row["status"] == "retry_wait"
    assert row["next_retry_at"] is not None
    assert (row["next_retry_at"] - NOW.replace(tzinfo=None)).total_seconds() == 30

    _clean("coord-auth")
    auth_connector = HeartConnector(error=ZeppAuthError("expired", kind="auth", needs_reauth=True))
    auth_coordinator, auth_attempt = _one_chunk_attempt("coord-auth", auth_connector)
    report = auth_coordinator.run_attempt(auth_attempt.id, max_chunks=1)
    assert auth_coordinator.status(auth_attempt.id)["attempt"]["status"] == "needs_reauth"
    assert report.needs_reauth


def test_cancel_and_deadline_are_persisted():
    _clean("coord-cancel")
    connector = HeartConnector()
    coordinator, attempt = _one_chunk_attempt("coord-cancel", connector)
    assert coordinator.request_cancel(attempt.id)
    report = coordinator.run_attempt(attempt.id)
    assert not report.success
    assert coordinator.status(attempt.id)["attempt"]["status"] == "cancelled"

    _clean("coord-deadline")
    deadline = NOW - timedelta(seconds=1)
    deadline_coordinator, deadline_attempt = _one_chunk_attempt(
        "coord-deadline", HeartConnector(), deadline=deadline,
    )
    report = deadline_coordinator.run_attempt(deadline_attempt.id)
    assert deadline_coordinator.status(deadline_attempt.id)["attempt"]["status"] == "partial"
    assert report.success is False


def test_dynamic_workout_detail_is_bounded_and_report_hides_leases():
    _clean("coord-detail")
    coordinator = ZeppSyncCoordinator(connector=WorkoutConnector(), wall_clock=lambda: NOW)
    start, end = WINDOW.start, WINDOW.end
    spec = {
        "stable_key": stable_chunk_key("workouts", "run", start, end, int(end.timestamp())),
        "stream": "workouts", "partition": "run", "ordinal": 0,
        "window_start": start, "window_end": end, "cursor": int(end.timestamp()),
        "allow_unavailable": True,
        "stages": {"operation": "fetch_sport_history", "params": {
            "sport": "run", "start_track_id": int(start.timestamp()),
            "stop_track_id": int(end.timestamp()), "need_sub_data": 1,
        }},
    }
    with session_scope() as db:
        attempt = HealthRepository(db).create_or_reuse_sync_attempt(
            "coord-detail", window_start=start, window_end=end, manifest=[spec],
        )
    report = coordinator.run_attempt(attempt.id)
    state = coordinator.status(attempt.id)
    assert report.success
    assert any(row["stream"] == "workout_detail" for row in state["chunks"])
    assert all("lease_token" not in row and "lease_epoch" not in row for row in state["chunks"])
    assert state["progress"]["complete"] is True


def test_manual_detail_refresh_mixes_cached_strength_and_new_running_workouts():
    captured = {}

    class Repository:
        def sync_chunks(self, _attempt_id):
            return []

        def pending_workout_details(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return [
                SimpleNamespace(workout_id="strength-cached", vendor_source="strength"),
                SimpleNamespace(workout_id="run-new", vendor_source="run"),
            ]

    attempt = {
        "id": "manual-refresh",
        "user_id": "manual-refresh-user",
        "trigger": "manual",
        "created_at": NOW.replace(tzinfo=None),
        "window_start": WINDOW.start,
        "window_end": WINDOW.end,
        "options": {},
    }
    chunk = {
        "stream": "workouts",
        "window_start": WINDOW.start,
        "window_end": WINDOW.end,
    }
    result = _ChunkResult(record=FetchedRecord(RawRecord(
        "workouts", "sport_history:run", WINDOW.start, WINDOW.end, {"data": {}}
    )))
    specs = ZeppSyncCoordinator()._dynamic_specs(
        Repository(), attempt, chunk, result
    )
    assert [spec["stages"]["params"]["workout_id"] for spec in specs] == [
        "strength-cached", "run-new"
    ]
    assert captured["kwargs"]["strength_only"] is False
    assert captured["kwargs"]["refresh_after"] == NOW.replace(tzinfo=None)
    refresh_start = captured["args"][1]
    refresh_end = captured["args"][2]
    assert (refresh_end - refresh_start).days == 28


def test_control_budget_is_monotonic_and_recovery_reclaims_expired_attempt():
    assert SyncControl.budget_for_days(7) <= SyncControl.budget_for_days(8)
    assert SyncControl.budget_for_days(8) <= SyncControl.budget_for_days(30)
    _clean("coord-recover")
    current = [NOW]
    coordinator = ZeppSyncCoordinator(connector=HeartConnector(), wall_clock=lambda: current[0])
    attempt = coordinator.create_attempt("coord-recover", window=WINDOW)
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.claim_attempt(attempt.id, "dead-worker", now=NOW, lease_seconds=1)
    current[0] = NOW + timedelta(seconds=2)
    recovered = coordinator.recover_due("coord-recover")
    assert recovered
    assert coordinator.status(attempt.id)["attempt"]["status"] in {"queued", "succeeded", "failed", "retry_wait"}


def test_large_manifest_keeps_global_statistics_and_sports_singleton():
    window = FetchWindow.local_dates(date(2026, 8, 1), date(2026, 8, 8))
    manifest = ZeppSyncCoordinator()._manifest(window, {})
    operations = [item["stages"]["operation"] for item in manifest]

    assert operations.count("fetch_watch_statistics") == 2
    assert operations.count("fetch_sport_history") == len(SPORTS)
    assert operations.count("fetch_devices") == 1


def test_dense_archive_selection_skips_handled_old_file_and_uses_newest():
    old_start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    new_start = datetime(2026, 8, 2, tzinfo=timezone.utc)
    payload = {"items": [{"value": {
        "startTime": int(old_start.timestamp() * 1000),
        "samples": [
            {"s": 0, "e": 60_000, "fileId": "old", "fileType": "SEC_HR", "dateString": "2026-08-01"},
            {"s": 86_400_000, "e": 86_460_000, "fileId": "new", "fileType": "SEC_HR", "dateString": "2026-08-02"},
        ],
    }}]}

    class Repo:
        def sync_chunks(self, _attempt_id):
            return []

        def dense_data_file_group(self, _user_id, _stream, file_id, source):
            if file_id == "old":
                return [SimpleNamespace(
                    start_utc=old_start.replace(tzinfo=None), device_id="",
                    parse_status="decoded",
                )]
            return []

    attempt = {
        "id": "dense-attempt", "user_id": "dense-user",
        "window_start": WINDOW.start, "window_end": WINDOW.end,
        "options": {"decode_dense_files": True},
    }
    chunk = {
        "stream": "dense_files", "window_start": WINDOW.start,
        "window_end": WINDOW.end,
    }
    result = _ChunkResult(record=FetchedRecord(RawRecord(
        "dense_files", "dense-index", WINDOW.start, WINDOW.end, payload
    )))

    specs = ZeppSyncCoordinator()._archive_spec(Repo(), attempt, chunk, result)
    assert specs[0]["stages"]["params"]["file_id"] == "new"


def test_expired_running_cancel_is_finalized_without_reclaim_loop():
    _clean("coord-expired-cancel")
    current = [NOW]
    coordinator = ZeppSyncCoordinator(wall_clock=lambda: current[0])
    attempt = coordinator.create_attempt("coord-expired-cancel", window=WINDOW)
    with session_scope() as db:
        assert HealthRepository(db).claim_attempt(
            attempt.id, "worker", now=NOW, lease_seconds=1
        )
    current[0] = NOW + timedelta(seconds=2)

    assert coordinator.request_cancel(attempt.id)
    assert coordinator.status(attempt.id)["attempt"]["status"] == "cancelled"


def test_link_refresh_terminal_projection_uses_bare_digest():
    _clean("coord-link-refresh")
    digest = "d" * 64
    with session_scope() as db:
        HealthRepository(db).create_browser_link(digest, "coord-link-refresh")
    coordinator = ZeppSyncCoordinator(wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt(
        "coord-link-refresh", window=WINDOW,
        trigger="link_refresh", trigger_ref=digest,
    )
    with session_scope() as db:
        repo = HealthRepository(db)
        token = "attempt-token"
        assert repo.claim_attempt(attempt.id, token, now=NOW)
        for chunk in repo.sync_chunks(attempt.id):
            chunk_token = f"chunk-{chunk.id}"
            assert repo.claim_chunk(chunk.id, chunk_token, now=NOW)
            assert repo.finalize_chunk(
                chunk.id, chunk_token, chunk.lease_epoch, "succeeded", now=NOW,
                stages={**dict(chunk.stages or {}), "fetch_status": "success", "parse_status": "success", "write_status": "success"},
            )
        claimed_attempt = repo.sync_attempt(attempt.id)
        assert claimed_attempt is not None
        assert repo.finalize_attempt(
            attempt.id, token, claimed_attempt.lease_epoch, "succeeded", now=NOW
        )
    coordinator._apply_terminal_side_effect(attempt.id, "succeeded")

    with session_scope() as db:
        link = HealthRepository(db).browser_link(digest)
        assert link is not None
        assert link.last_sync_at is not None
        assert link.sync_attempt_id == attempt.id


def test_coordinator_marks_full_page_no_progress_partial_without_retrying():
    _clean("coord-incomplete")

    class Connector:
        def __init__(self):
            self.calls = 0

        def fetch_heart_rate(self, start, end, limit, hr_type):
            self.calls += 1
            return {"items": [
                {"timestamp": start - 1, "value": 72}
                for _ in range(limit)
            ]}

    connector = Connector()
    coordinator, attempt = _one_chunk_attempt("coord-incomplete", connector)
    report = coordinator.run_attempt(attempt.id, max_chunks=1)
    state = coordinator.status(attempt.id)

    assert connector.calls == 1
    assert state["attempt"]["status"] == "partial"
    heart_rate = next(
        row for row in state["chunks"] if row["stream"] == "heart_rate"
    )
    assert heart_rate["status"] == "succeeded"
    assert heart_rate["fetch_status"] == "partial"
    assert heart_rate["error_kind"] == "partial_coverage"
    assert report.success is False
    # Terminal partial attempts are not re-queued by the ledger.
    coordinator.run_attempt(attempt.id, max_chunks=1)
    assert connector.calls == 1


def test_manifest_uses_inclusive_three_day_local_odi_windows_across_dst():
    zone = "America/New_York"
    window = FetchWindow.local_dates(date(2026, 3, 7), date(2026, 3, 9), zone)
    manifest = ZeppSyncCoordinator()._manifest(window, {}, zone)
    odi = [
        item for item in manifest
        if item["partition"] == "spo2/odi"
    ]
    osa = [
        item for item in manifest
        if item["partition"] == "spo2/osa_event"
    ]

    assert len(odi) == len(osa) == 1
    assert odi[0]["stages"]["params"] == {
        "event_type": "blood_oxygen",
        "sub_type": "odi",
        "from_date": "2026-03-07",
        "to_date": "2026-03-09",
        "time_zone": zone,
    }
    point = [item for item in manifest if item["partition"] == "spo2"][0]
    assert point["window_start"] == window.start
    assert point["window_end"] == window.end


def test_legacy_odi_iso_params_are_converted_to_local_inclusive_dates():
    coordinator = ZeppSyncCoordinator()
    assert coordinator._legacy_local_date(
        "2026-08-01T16:00:00Z", "Asia/Shanghai"
    ) == "2026-08-02"
    assert coordinator._legacy_local_date(
        "2026-08-02T16:00:00Z", "Asia/Shanghai", exclusive_end=True
    ) == "2026-08-02"


def test_manifest_defaults_to_application_timezone():
    coordinator = ZeppSyncCoordinator()
    window = FetchWindow.local_dates(date(2026, 8, 1), date(2026, 8, 1))
    manifest = coordinator._manifest(window, {})
    odi = next(item for item in manifest if item["partition"] == "spo2/odi")
    assert odi["stages"]["params"]["time_zone"] == settings.timezone


def test_coordinator_wellness_chunk_handles_non_capped_payload():
    class Connector:
        def fetch_events(self, *args):
            return {"items": []}

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    chunk = {
        "cursor": None,
        "window_start": start,
        "window_end": end,
        "stream": "wellness",
        "stages": {
            "operation": "fetch_wellness",
            "params": {
                "label": "respiratory_rate",
                "surface": "v2",
                "event_type": "RespiratoryRate",
                "sub_type": "real_data",
                "from_ms": int(start.timestamp() * 1000),
                "to_ms": int(end.timestamp() * 1000),
            },
        },
    }

    result = ZeppSyncCoordinator()._fetch_chunk(chunk, SyncControl(), Connector())

    assert result.record is not None
    assert result.incomplete is False


@pytest.mark.parametrize("period", ("morning", "evening"))
@pytest.mark.parametrize(
    ("configured_user", "token", "recipient"),
    [
        ("coord-push-owner", "private-token", "coord-push-owner"),
        ("coord-push-unbound", "private-token", None),
        (None, "private-token", None),
        ("coord-push-owner", None, None),
    ],
)
def test_scheduled_push_requires_explicit_recipient_binding(
    monkeypatch, tmp_path, period, configured_user, token, recipient
):
    from vitalis.intelligence.service import IntelligenceCommand
    from vitalis.services import daily_push

    if configured_user is None:
        monkeypatch.delenv("VITALIS_PUSH_USER", raising=False)
    else:
        monkeypatch.setenv("VITALIS_PUSH_USER", configured_user)
    if token is None:
        monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PUSHPLUS_TOKEN", token)

    report_day = date(2026, 8, 29)
    monkeypatch.setattr(daily_push, "local_today", lambda: report_day)
    monkeypatch.setattr(
        daily_push, "local_day_utc_bounds",
        lambda _day: (NOW, datetime(2100, 1, 1, tzinfo=timezone.utc)),
    )
    marker_for = daily_push._delivery_marker
    monkeypatch.setattr(
        daily_push, "_delivery_marker",
        lambda _state_dir, user, day, report_period: marker_for(
            tmp_path, user, day, report_period
        ),
    )
    analyzed = []
    sent = []

    def analyze(_self, user_id):
        analyzed.append(user_id)
        return SimpleNamespace(daily={
            "date": report_day.isoformat(),
            "data_quality": {"status": "SUFFICIENT"},
            "report_context": {"training_history": {
                "status": "COMPLETE", "prior_7d_verified": True,
            }},
            "features": {"sleep": {"status": "AVAILABLE", "wake_time": "08:00:00"}},
        })

    class RecordingPushService:
        def __init__(self, pushplus_token):
            assert pushplus_token == "private-token"

        def push_daily_profile(self, user_id, _daily, period):
            sent.append((user_id, period))
            return {"_pushplus_handler": "ok"}

    monkeypatch.setattr(IntelligenceCommand, "analyze", analyze)
    monkeypatch.setattr(daily_push, "PushService", RecordingPushService)

    for user_id in ("coord-push-owner", "coord-push-other"):
        _clean(user_id)
        coordinator, attempt = _one_chunk_attempt(
            user_id, HeartConnector(), trigger=period
        )
        assert coordinator.run_attempt(attempt.id).success

    assert analyzed == ["coord-push-owner", "coord-push-other"]
    assert sent == ([(recipient, period)] if recipient else [])
    markers = list(tmp_path.glob("*.sent"))
    assert markers == (
        [marker_for(tmp_path, recipient, report_day, period)] if recipient else []
    )
