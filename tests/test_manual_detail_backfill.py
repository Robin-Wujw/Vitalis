"""Historical detail refresh must be an explicit manual sync option."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from vitalis.connectors.zepp import ZeppConnector
from vitalis.models import User


def test_manual_detail_backfill_api_passes_explicit_flag(client, monkeypatch):
    from vitalis.api.routes import health

    calls = []

    class Connector:
        mock = False

        def load_token(self, repo, user_id):
            return object()

        def create_attempt(self, user_id, **kwargs):
            calls.append(("queued", kwargs))
            return SimpleNamespace(id="backfill-attempt", status="queued")

        def sync_with_report(self, user, **kwargs):
            calls.append(("sync", kwargs))
            return SimpleNamespace(
                success=True, needs_reauth=False,
                progress={"status": "succeeded", "attempt_id": "backfill-attempt"},
                streams=[], records_written=0, message="",
            )

    monkeypatch.setattr(health, "get_connector", lambda source: Connector())
    headers = {"X-User-Id": "manual-backfill-test"}
    response = client.post(
        "/api/v1/health/sync?days=8&enqueue_only=true&detail_backfill=true",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert calls[-1] == (
        "queued", {"days": 8, "trigger": "manual", "decode_dense_files": False,
                   "detail_backfill": True, "workout_only": False},
    )

    response = client.post(
        "/api/v1/health/sync?days=8&detail_backfill=true", headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "synced"
    assert calls[-1][0] == "sync"
    assert calls[-1][1]["detail_backfill"] is True
    response = client.post("/api/v1/health/sync?days=8&enqueue_only=true", headers=headers)
    assert response.status_code == 200
    assert calls[-1][1]["detail_backfill"] is False
    assert calls[-1][1]["workout_only"] is False
    response = client.post(
        "/api/v1/health/sync?days=730&enqueue_only=true&workout_only=true",
        headers=headers,
    )
    assert response.status_code == 200
    assert calls[-1][1]["workout_only"] is True


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
    from vitalis.services import zepp_sync_coordinator

    captured = []

    def create_attempt(self, user_id, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id="manual-workouts", status="queued")

    monkeypatch.setattr(zepp_sync_coordinator.ZeppSyncCoordinator, "create_attempt", create_attempt)
    connector = ZeppConnector(mock=False)
    connector.create_attempt(
        "owner", days=730, workout_only=True, detail_backfill=True,
    )
    assert captured[0]["trigger"] == "manual"
    assert captured[0]["options"] == {
        "decode_dense_files": False, "detail_backfill": True, "workout_only": True,
    }


def test_sync_cli_only_sets_backfill_param_when_requested(monkeypatch, capsys):
    path = Path(__file__).resolve().parents[1] / "skills" / "vitalis" / "tools" / "sync.py"
    spec = spec_from_file_location("vitalis_sync_cli_test", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "queued"}

    def fake_post(url, **kwargs):
        calls.append(kwargs["params"])
        return Response()

    monkeypatch.setenv("VITALIS_USER", "owner")
    monkeypatch.setattr(module.httpx, "post", fake_post)
    monkeypatch.setattr(sys, "argv", ["sync.py", "--days", "8"])
    assert module.main() == 0
    monkeypatch.setattr(sys, "argv", ["sync.py", "--days", "8", "--detail-backfill"])
    assert module.main() == 0
    monkeypatch.setattr(sys, "argv", ["sync.py", "--days", "730", "--workout-only"])
    assert module.main() == 0
    assert calls == [
        {"days": 8}, {"days": 8, "detail_backfill": "true"},
        {"days": 730, "workout_only": "true"},
    ]
    assert "queued" in capsys.readouterr().out
