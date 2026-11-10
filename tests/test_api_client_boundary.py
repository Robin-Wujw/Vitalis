"""All business clients use the same authenticated HTTP boundary."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from vitalis import api_client


@pytest.fixture(autouse=True)
def clean_api_client_users():
    yield
    from vitalis.adapters.persistence import HealthRepository, session_scope

    with session_scope() as db:
        repo = HealthRepository(db)
        for owner in ("api-sleep-owner", "api-sleep-other", "api-catalog"):
            repo.delete_for_user(owner)


def test_api_help_does_not_load_private_server_configuration(monkeypatch):
    import os

    result = subprocess.run([sys.executable, "-m", "vitalis", "api", "--help"],
        env={**os.environ, "VITALIS_ENV": "production", "DATABASE_URL": "invalid://", "ZEPP_MOCK": "true"},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "request" in result.stdout and "metrics" in result.stdout


def test_packaged_and_standalone_client_share_one_generated_source():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/vitalis/api_client.py").read_bytes() == (root / "skills/vitalis/scripts/vitalis_api.py").read_bytes()


@pytest.mark.parametrize("path", ["https://other.invalid/api/profile", "//other.invalid", "../profile", "health/../profile", "health%2f..%2fprofile", "health?token=bad", "health#fragment"])
def test_generic_api_client_rejects_paths_that_escape_the_api_origin(path):
    with pytest.raises(api_client.ClientError):
        api_client.api_path(path)


def test_generic_api_write_uses_json_stdin_and_a_persistent_idempotency_key(monkeypatch, tmp_path):
    import io

    calls = []
    monkeypatch.setattr(api_client, "api_origin", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(api_client, "access_token", lambda: "synthetic-token")
    monkeypatch.setattr(api_client, "request", lambda *args, **kwargs: calls.append((args, kwargs)) or {"status": "queued"})
    monkeypatch.setattr(api_client.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"days":7}'), encoding="utf-8"))
    key_file = tmp_path / "sync.key"
    api_client.run(api_client.parse_args(["request", "POST", "sync-jobs", "--key-file", str(key_file)]))
    assert calls[0][0] == ("POST", "sync-jobs", "synthetic-token")
    assert calls[0][1]["body"] == {"days": 7}
    assert calls[0][1]["key"] and key_file.exists()


def test_saved_sleep_api_keeps_sources_separate_and_scopes_the_owner(client):
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from datetime import date

    with session_scope() as db:
        repo = HealthRepository(db)
        for user in ("api-sleep-owner", "api-sleep-other"):
            repo.upsert_user(user)
            db.flush()
            repo.save_band_source(user, date(2026, 10, 2), "sleep", {
                "source": "zepp", "source_scope": "user_fused", "sleep_duration": 450,
                "sleep_latency_minutes": 12, "time_in_bed_minutes": 500})
    response = client.get("/api/health/sleep-records?from=2026-10-02&to=2026-10-02",
        headers={"X-User-Id": "api-sleep-owner"})
    assert response.status_code == 200
    rows = response.json()["records"]
    assert len(rows) == 1 and rows[0]["data"]["sleep_latency_minutes"] == 12
    assert "api-sleep-other" not in json.dumps(rows)


def test_catalog_api_explains_raw_only_and_vendor_estimate_data(client):
    response = client.get("/api/health/catalog", headers={"X-User-Id": "api-catalog"})
    assert response.status_code == 200
    body = response.json()
    assert body["streams"]["charge_stress"]["normalization"] == "raw_only"
    assert body["signals"]["vendor_tsb"]["unit"] == "vendor_load"
    assert body["signals"]["vendor_tsb"]["decision_role"] == "no"
