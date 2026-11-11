"""Durable coordinator integration for account metadata and explicit raw reads."""
from datetime import UTC, date, datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import AnalysisJob, SourceSnapshot
from vitalis.adapters.persistence.source_journal import JournalError, SourceJournalRecord, SourceJournalRepository
from vitalis.adapters.zepp.client import MockZeppClient, ZeppAuthError, ZeppAPIClient
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator

NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
WINDOW = FetchWindow.local_dates(date(2026, 9, 1), date(2026, 9, 1))


class Connector:
    source_mode = "mock"

    def fetch_raw(self, request):
        assert request["path"] == "/v2/users/me/events"
        return {"items": [{"value": {"score": 73, "token": "synthetic-secret"}}]}

    def fetch_profile(self):
        return {"data": {"height": 179, "weight_float": 71, "birthday": "1995-06",
                         "token": "synthetic-secret"}}

    def fetch_devices(self):
        return {"items": [{"deviceSource": "synthetic-watch", "firmwareVersion": "1.0",
                           "macAddress": "02:00:00:00:00:07", "additionalInfo": {"auth_key": "synthetic-secret"}}]}


def test_raw_job_roundtrip_encrypts_payload_and_cannot_read_another_owner():
    owner = "raw-durable-parity"
    service = ZeppSyncCoordinator(connector=Connector(), wall_clock=lambda: NOW)
    request = {"path": "/v2/users/me/events", "params": {"eventType": "Synthetic"}}
    attempt = service.create_attempt(owner, window=WINDOW, options={"raw_request": request})
    assert attempt.chunk_count == 1
    service.drain_once(attempt.id)
    result = service.status(attempt.id)
    assert result["attempt"]["status"] == "succeeded"
    assert result["chunks"][0]["stages"]["parse_status"] == "retained_raw"
    with session_scope() as db:
        journal, = db.scalars(select(SourceJournalRecord).where(SourceJournalRecord.user_id == owner)).all()
        assert journal.stream == "raw_request"
        assert "synthetic-secret" not in journal.encrypted_payload
        assert journal.encrypted_payload.startswith(("fernet:", "mock-fernet:"))
        repository = SourceJournalRepository(db)
        assert repository.read_payload(journal.id, user_id=owner)["items"][0]["value"]["score"] == 73
        with pytest.raises(JournalError):
            repository.read_payload(journal.id, user_id="another-owner")
        assert db.scalars(select(SourceSnapshot).where(SourceSnapshot.user_id == owner)).all() == []


def test_profile_devices_snapshots_keep_journal_fetch_time_and_sanitized_fields():
    owner = "profile-durable-parity"
    service = ZeppSyncCoordinator(connector=Connector(), wall_clock=lambda: NOW)
    attempt = service.create_attempt(owner, window=WINDOW, options={"streams": ["profile", "devices"]})
    for _ in range(2):
        service.drain_once(attempt.id)
    assert service.status(attempt.id)["attempt"]["status"] == "succeeded"
    with session_scope() as db:
        rows = db.scalars(select(SourceSnapshot).where(SourceSnapshot.user_id == owner)).all()
        assert {row.kind for row in rows} == {"profile", "devices"}
        for row in rows:
            assert row.fetched_at == NOW.replace(tzinfo=None)
            journal = db.get(SourceJournalRecord, row.source_record_id)
            assert journal is not None and journal.user_id == owner and journal.stream == row.kind
            assert "synthetic-secret" not in json.dumps(row.data)
            assert "macAddress" not in json.dumps(row.data)
        profile = next(row for row in rows if row.kind == "profile")
        assert profile.data["height_cm"] == 179 and profile.data["weight_kg"] == 71


def test_mock_client_supports_new_profile_and_raw_jobs():
    mock = MockZeppClient()
    assert isinstance(mock.fetch_profile()["data"], dict)
    assert isinstance(mock.fetch_raw({"path": "/v2/users/me/events", "params": {}}), dict)


def test_raw_client_rejects_vendor_error_envelopes_and_binds_current_identity(monkeypatch):
    client = ZeppAPIClient.__new__(ZeppAPIClient)
    client.user_id = "synthetic-owner"
    called = []

    def get(path, params, **kwargs):
        called.append((path, params, kwargs))
        return {"success": False, "message": "rejected"}

    monkeypatch.setattr(client, "_get", get)
    with pytest.raises(ZeppAuthError):
        client.fetch_raw({"path": "/users/me/profile", "params": {}})
    assert called[0][0] == "/users/synthetic-owner/profile"
    assert called[0][1]["userid"] == "synthetic-owner"


@pytest.mark.parametrize("payload", [[], [{"score": 42}], {"items": []}])
def test_raw_client_accepts_object_and_list_roots(monkeypatch, payload):
    client = ZeppAPIClient.__new__(ZeppAPIClient)
    client.user_id = "synthetic-owner"
    monkeypatch.setattr(client, "_get", lambda *_args, **_kwargs: payload)
    assert client.fetch_raw({"path": "/v2/users/me/events"}) == payload


@pytest.mark.parametrize("payload", [None, True, 42, "invalid"])
def test_raw_client_rejects_scalar_roots(monkeypatch, payload):
    client = ZeppAPIClient.__new__(ZeppAPIClient)
    client.user_id = "synthetic-owner"
    monkeypatch.setattr(client, "_get", lambda *_args, **_kwargs: payload)
    with pytest.raises(ZeppAuthError):
        client.fetch_raw({"path": "/v2/users/me/events"})


def test_older_profile_snapshot_cannot_replace_newer_observation():
    from vitalis.adapters.persistence.source_snapshots import save_source_snapshot
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("profile-ordering")
        db.flush()
        save_source_snapshot(repo, "profile-ordering", "profile", {"height_cm": 170}, fetched_at=NOW)
        save_source_snapshot(repo, "profile-ordering", "profile", {"height_cm": 169}, fetched_at=NOW - timedelta(days=1))
        row = db.get(SourceSnapshot, ("profile-ordering", "zepp", "profile"))
        assert row.data["height_cm"] == 170


def test_raw_http_scope_owner_and_sanitized_result(client, issue_token):
    from vitalis.bootstrap import get_vendor_query

    owner = "raw-http-parity"
    read = {"Authorization": "Bearer " + issue_token(owner, scopes=["read"])}
    raw = {"Authorization": "Bearer " + issue_token(owner, scopes=["source_raw"])}
    outsider = {"Authorization": "Bearer " + issue_token("raw-http-other", scopes=["source_raw"])}
    service = ZeppSyncCoordinator(connector=Connector(), wall_clock=lambda: NOW)
    attempt = service.create_attempt(owner, window=WINDOW, options={"raw_request": {"path": "/v2/users/me/events"}})
    service.drain_once(attempt.id)
    path = f"/api/sources/zepp/raw-requests/{attempt.id}"
    assert client.get(path, headers=read).status_code == 403
    assert client.get(path, headers=outsider).status_code == 404
    response = client.get(path, headers=raw)
    assert response.status_code == 200, response.text
    assert "synthetic-secret" not in response.text
    assert response.json()["result"]["payload"]["items"][0]["value"]["score"] == 73
    assert get_vendor_query().job_result("raw-http-other", attempt.id) is None
    body = {"path": "/v2/users/me/events", "params": {}}
    assert client.post("/api/sources/zepp/raw-requests", json=body,
        headers={**read, "Idempotency-Key": "synthetic-raw-key-0001"}).status_code == 403


@pytest.mark.parametrize("code,status", [
    (sqlite3.SQLITE_BUSY, 503), (sqlite3.SQLITE_LOCKED, 503),
    (sqlite3.SQLITE_BUSY_SNAPSHOT, 503), (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 503),
    (sqlite3.SQLITE_IOERR, 500), (None, 500),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_raw_post_storage_failures_use_error_codes_without_exposing_sql(
    client, issue_token, monkeypatch, caplog, code, status, wrapped,
):
    from vitalis.entrypoints.api.routes import vendor

    secret = "synthetic-private-sql-parameters"
    error = sqlite3.OperationalError("database is locked: " + secret)
    if code is not None:
        error.sqlite_errorcode = code
    failure = OperationalError("INSERT " + secret, {"private": secret}, error) if wrapped else error

    def create(_command):
        raise failure

    monkeypatch.setattr(vendor, "get_sync_job_service", lambda: SimpleNamespace(create=create))
    headers = {"Authorization": "Bearer " + issue_token("raw-storage-failure", ["source_raw"]),
               "Idempotency-Key": "synthetic-raw-failure-key"}
    response = client.post("/api/sources/zepp/raw-requests", headers=headers,
                           json={"path": "/v2/users/me/events", "params": {}})
    assert response.status_code == status
    body = response.json()
    assert body["state"] == "failed"
    assert body["failure_code"] == ("service_unavailable" if status == 503 else "internal_error")
    assert body["retryable"] is (status == 503)
    assert body["next_action"] == ("retry_later" if status == 503 else "check_service")
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert response.headers.get("Retry-After") == ("5" if status == 503 else None)
    assert secret not in response.text + caplog.text


def test_raw_http_post_dispatch_and_result_use_one_durable_chunk(client, issue_token, monkeypatch):
    from vitalis.adapters.zepp import ZeppConnector

    owner = "raw-http-dispatch"
    calls = []

    def fetch_raw(_client, request):
        calls.append(request)
        return Connector().fetch_raw(request)

    monkeypatch.setattr(MockZeppClient, "fetch_raw", fetch_raw)
    headers = {"Authorization": "Bearer " + issue_token(owner, ["source_raw"]),
               "Idempotency-Key": "synthetic-raw-dispatch-key"}
    body = {"path": "/v2/users/me/events", "params": {"eventType": "Synthetic"}}
    created = client.post("/api/sources/zepp/raw-requests", headers=headers, json=body)
    assert created.status_code == 202, created.text
    assert calls == []
    accepted = created.json()
    repeated = client.post("/api/sources/zepp/raw-requests", headers=headers, json=body)
    assert repeated.status_code == 202 and repeated.json()["job_id"] == accepted["job_id"]
    pending = client.get(accepted["result_url"], headers=headers)
    assert pending.status_code == 202 and pending.json()["state"] == "pending"
    with session_scope() as db:
        repo = HealthRepository(db)
        chunks = repo.sync_chunks(accepted["job_id"])
        assert len(chunks) == 1
        assert chunks[0].stream == "raw_request"
        assert chunks[0].stages["operation"] == "fetch_raw"
        from vitalis.adapters.persistence.models import SyncAttempt
        attempt = db.get(SyncAttempt, accepted["job_id"])
        assert attempt.options["execution_context"]["notification_policy"] == "suppress"
    worker = ZeppSyncCoordinator(connector=ZeppConnector(mock=True))
    worker.drain_once(accepted["job_id"])
    assert calls == [body]
    result = client.get(accepted["result_url"], headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "ready"
    assert result.json()["status"] == "succeeded"
    assert result.json()["result"]["payload"]["items"][0]["value"]["score"] == 73
    assert result.json()["result"]["qualification"] == "raw_unverified"
    assert "synthetic-secret" not in result.text
    with session_scope() as db:
        assert db.scalars(select(AnalysisJob).where(AnalysisJob.user_id == owner)).all() == []


def test_raw_job_result_has_stable_413(client, issue_token, monkeypatch):
    from vitalis.entrypoints.api.routes import vendor
    from types import SimpleNamespace

    def large(*_):
        raise ValueError("synthetic response exceeds budget")

    monkeypatch.setattr(vendor, "get_vendor_query", lambda: SimpleNamespace(job_result=large))
    headers = {"Authorization": "Bearer " + issue_token("large-raw", scopes=["source_raw"])}
    result = client.get("/api/sources/zepp/raw-requests/synthetic", headers=headers)
    assert result.status_code == 413
    assert result.json()["state"] == "failed"
    assert set(result.json()) >= {"failure_code", "retryable", "next_action"}


def test_raw_metadata_list_includes_instant_records_without_loading_ciphertext():
    from sqlalchemy import event
    from vitalis.adapters.persistence.health_reader import SqlHealthReader
    from vitalis.adapters.persistence.source_journal import SourceRecordInput
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("instant-raw")
        db.flush()
        repo.bind_source_mode("instant-raw", "mock")
        SourceJournalRepository(db).append(SourceRecordInput(user_id="instant-raw", source="zepp",
            source_mode="mock", stream="raw_request", record_id="instant", observed_start=NOW,
            observed_end=None, fetched_at=NOW, payload={"synthetic": 1}))
        engine = db.get_bind()
    statements = []

    def capture(_, __, statement, ___, ____, _____):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = SqlHealthReader().raw_source_records("instant-raw", "raw_request", NOW.date(), NOW.date(), 10)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(rows) == 1
    assert not any("encrypted_payload" in statement for statement in statements)
