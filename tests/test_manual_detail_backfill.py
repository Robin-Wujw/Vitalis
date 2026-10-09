"""Historical detail refresh must be an explicit manual sync option."""

from datetime import datetime, timedelta, timezone
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

from vitalis.adapters.zepp import ZeppConnector
from vitalis.domain import AuthToken, User, Workout
from vitalis.adapters.persistence import HealthRepository, session_scope


def _api_ref(key: str) -> str:
    return "api:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def test_manual_detail_backfill_api_passes_explicit_flags_to_queued_attempt(client, monkeypatch):
    from vitalis.entrypoints.api.routes import current
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncJobAdapter
    from vitalis.application.sync_jobs import SyncJobService

    calls = []

    class Connector:
        mock = True

        def create_attempt(self, user_id, **kwargs):
            calls.append((user_id, kwargs))
            return SimpleNamespace(id=f"backfill-attempt-{len(calls)}", status="queued")

    monkeypatch.setattr(
        current,
        "get_sync_job_service",
        lambda: SyncJobService(
            ZeppSyncJobAdapter(lambda _source: Connector())
        ),
    )
    headers = {"X-User-Id": "manual-backfill-test"}

    def submit(body):
        key = f"manual-backfill-request-{len(calls) + 1}"
        response = client.post(
            "/api/sync-jobs", json=body,
            headers={**headers, "Idempotency-Key": key},
        )
        assert response.status_code == 202, response.text
        assert response.json() == {
            "job_id": f"backfill-attempt-{len(calls)}", "status": "queued",
            "status_url": f"/api/jobs/backfill-attempt-{len(calls)}",
        }
        assert calls[-1][0] == "manual-backfill-test"
        assert calls[-1][1]["trigger_ref"] == _api_ref(key)
        return calls[-1][1]

    assert submit({"days": 8, "detail_backfill": True}) == {
        "days": 8, "trigger": "manual", "trigger_ref": _api_ref("manual-backfill-request-1"),
        "decode_dense_files": False, "detail_backfill": True,
        "workout_only": False, "detail_only": False,
        "detail_refresh_before": None, "detail_limit": None,
    }
    assert submit({"days": 8})["detail_backfill"] is False
    assert submit({"days": 730, "workout_only": True})["workout_only"] is True
    assert submit({"days": 730, "detail_only": True})["detail_only"] is True
    options = submit({"days": 730, "detail_only": True, "detail_limit": 1,
                      "detail_refresh_before": "2026-09-24T14:00:00Z"})
    assert options["detail_limit"] == 1
    assert options["detail_refresh_before"] == "2026-09-24T14:00:00Z"
    assert len(calls) == 5


@pytest.mark.parametrize("body,expected", [
    ({"detail_limit": 1}, 400),
    ({"detail_refresh_before": "2026-09-24T14:00:00Z"}, 400),
    ({"detail_only": True, "detail_limit": 0}, 422),
    ({"detail_only": True, "detail_limit": 5}, 422),
    ({"detail_only": True, "workout_only": True}, 400),
    ({"detail_only": True, "detail_backfill": True}, 400),
    ({"detail_only": True, "decode_dense_files": True}, 400),
])
def test_sync_job_rejects_incompatible_manual_options(client, body, expected):
    headers = {"X-User-Id": "manual-detail-invalid", "Idempotency-Key": "manual-detail-invalid-request"}
    response = client.post("/api/sync-jobs", json=body, headers=headers)
    assert response.status_code == expected, response.text


def test_detail_only_api_reports_empty_backlog_without_creating_job(client):
    user_id = "manual-detail-empty"
    headers = {"X-User-Id": user_id, "Idempotency-Key": "manual-detail-empty-request"}
    response = client.post("/api/sync-jobs", json={"days": 730, "detail_only": True}, headers=headers)
    assert response.status_code == 409
    assert response.json()["failure_code"] == "conflict"
    assert response.json()["retryable"] is False
    with session_scope() as db:
        assert HealthRepository(db).sync_attempts(user_id) == []


def test_detail_only_api_freezes_requested_workout_backlog(client, monkeypatch):
    from vitalis.entrypoints.api.routes import current

    user_id = "manual-detail-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        db.flush()
        repo.save_token(AuthToken(
            user_id=user_id, source="zepp", source_user_id="vendor-manual-detail-workout",
            access_token="test-token",
        ))
        for index in range(2):
            repo.save_workout(Workout(
                user_id=user_id, workout_id=f"detail-{index}",
                started_at=datetime.now(timezone.utc) - timedelta(days=2),
                duration=30, training_family="strength", vendor_source="cloud",
            ))
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncJobAdapter
    from vitalis.application.sync_jobs import SyncJobService

    monkeypatch.setattr(
        current,
        "get_sync_job_service",
        lambda: SyncJobService(
            ZeppSyncJobAdapter(lambda _source: ZeppConnector(mock=False))
        ),
    )
    headers = {"X-User-Id": user_id, "Idempotency-Key": "manual-detail-one-workout"}
    response = client.post(
        "/api/sync-jobs", json={"days": 7, "detail_only": True, "detail_limit": 1},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    own = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert own.status_code == 200
    assert own.json()["attempt"]["status"] == "queued"
    assert len(own.json()["chunks"]) == 1
    assert own.json()["chunks"][0]["stream"] == "workout_detail"
    with session_scope() as db:
        repo = HealthRepository(db)
        attempt = repo.sync_attempt(job_id, user_id=user_id)
        assert attempt.options == {
            "decode_dense_files": False, "detail_only": True, "detail_limit": 1,
            "source_mode": "real",
        }
        assert len(repo.pending_workout_details(
            user_id, attempt.window_start, attempt.window_end, limit=10,
        )) == 1


def test_nonmanual_detail_backfill_is_rejected_before_scheduling():
    connector = ZeppConnector(mock=False)
    with pytest.raises(ValueError, match="manual sync"):
        connector.create_attempt("owner", trigger="nightly", detail_backfill=True)
    with pytest.raises(ValueError, match="manual sync"):
        connector.sync_with_report(
            User(id="owner"), trigger="nightly", detail_backfill=True,
        )
    with pytest.raises(ValueError, match="manual sync"):
        connector.create_attempt("owner", trigger="morning", workout_only=True)
    with pytest.raises(ValueError, match="manual sync"):
        connector.sync_with_report(
            User(id="owner"), trigger="evening", workout_only=True,
        )


def test_connector_persists_workout_only_as_manual_attempt_option(monkeypatch):
    from vitalis.adapters.zepp import sync_coordinator as zepp_sync_coordinator

    captured = []

    def create_attempt(self, user_id, **kwargs):
        captured.append(kwargs)
        return None if kwargs["options"].get("detail_only") else SimpleNamespace(
            id="manual-workouts", status="queued"
        )

    monkeypatch.setattr(zepp_sync_coordinator.ZeppSyncCoordinator, "create_attempt", create_attempt)
    connector = ZeppConnector(mock=False)
    connector.create_attempt(
        "owner", days=730, workout_only=True, detail_backfill=True,
    )
    assert captured[0]["trigger"] == "manual"
    assert captured[0]["options"] == {
        "decode_dense_files": False, "detail_backfill": True, "workout_only": True,
        "source_mode": "real",
    }
    assert connector.create_attempt(
        "owner", days=730, detail_only=True, detail_limit=1,
        detail_refresh_before="2026-09-24T14:00:00Z",
    ) is None
    assert captured[1]["options"] == {
        "decode_dense_files": False,
        "detail_only": True,
        "detail_refresh_before": "2026-09-24T14:00:00Z",
        "detail_limit": 1,
        "source_mode": "real",
    }
    with pytest.raises(ValueError, match="detail_limit requires detail_only"):
        connector.create_attempt("owner", detail_limit=1)


def test_sync_client_passes_manual_options_as_idempotent_job_body(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "skills" / "vitalis" / "scripts" / "vitalis_api.py"
    spec = spec_from_file_location("vitalis_sync_cli_test", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    calls = []

    def fake_request(method, endpoint, token, **kwargs):
        calls.append((method, endpoint, token, kwargs))
        return {"job_id": "queued-job", "status": "queued"}

    monkeypatch.setenv("VITALIS_API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(module, "request", fake_request)
    variants = [
        ("--days", "8"),
        ("--days", "8", "--detail-backfill"),
        ("--days", "730", "--workout-only"),
        ("--days", "730", "--detail-only"),
        ("--days", "730", "--detail-only", "--detail-limit", "1"),
        ("--days", "730", "--detail-only", "--detail-limit", "1",
         "--detail-refresh-before", "2026-09-24T14:00:00Z"),
    ]
    for index, flags in enumerate(variants):
        args = module.parse_args([
            "sync", "--key-file", str(tmp_path / f"sync-{index}.json"), *flags,
        ])
        assert module.run(args) == {"job_id": "queued-job", "status": "queued"}
    assert len(calls) == len(variants)
    assert all(method == "POST" and endpoint == "sync-jobs" and token == "synthetic-test-token"
               and len(kwargs["key"]) >= 16 for method, endpoint, token, kwargs in calls)
    bodies = [item[3]["body"] for item in calls]
    assert [body["days"] for body in bodies] == [8, 8, 730, 730, 730, 730]
    assert [body["detail_backfill"] for body in bodies] == [False, True, False, False, False, False]
    assert bodies[2]["workout_only"] is True
    assert all(body["detail_only"] is True for body in bodies[3:])
    assert bodies[4]["detail_limit"] == 1
    assert bodies[5]["detail_refresh_before"] == "2026-09-24T14:00:00Z"
    first_key = calls[0][3]["key"]
    assert module.run(module.parse_args([
        "sync", "--key-file", str(tmp_path / "sync-0.json"), "--days", "8",
    ]))["status"] == "queued"
    assert calls[-1][3]["key"] == first_key
    with pytest.raises(module.ClientError):
        module.parse_args([
            "sync", "--key-file", str(tmp_path / "invalid.json"),
            "--detail-limit", "5",
        ])
