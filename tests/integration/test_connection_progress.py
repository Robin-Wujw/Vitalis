"""Offline connection windows, truthful progress, and safe latency queries."""

from datetime import UTC, date, datetime, timedelta
import secrets
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, database, init_db
from vitalis.adapters.persistence.models import AnalysisJob, AnalysisRun, AnalysisSnapshot, SyncAttempt, User
from vitalis.application.connection import ConnectionOperationError, ConnectionService
from vitalis.config import settings
from vitalis.domain import ActivityRecord, AuthToken, NormalizedDaily, SleepRecord
from vitalis.entrypoints.api.routes import connect, zepp_pairing


DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


@pytest.fixture
def connection_store(tmp_path, monkeypatch):
    from vitalis.adapters.zepp import ZeppConnector

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'connection.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr("vitalis.time.local_today", lambda: DAY)
    creator = ZeppConnector(mock=True)

    class Provider:
        source = "zepp"
        mock = False

        def authorize_url(self):
            state = secrets.token_urlsafe(24)
            return self.authorize_url_for(state), state

        def authorize_url_for(self, state):
            return f"https://oauth.example.test/authorize?state={state}"

        def parse_cookie(self, raw):
            return creator.parse_cookie(raw)

        def verify_credentials(self, user_id, credentials, **_kwargs):
            return AuthToken(
                user_id=user_id, source="zepp", access_token="synthetic-secret",
                source_user_id=credentials.vendor_user_id, scope="apptoken",
            )

        def exchange_code(self, user_id, code, state):
            return AuthToken(
                user_id=user_id, source="zepp", access_token="synthetic-secret",
                source_user_id=f"vendor-{user_id}", scope="oauth",
            )

        def verify_saved(self, _token):
            return None

    provider = Provider()
    service = ConnectionService(
        database.UnitOfWork, provider, sync_creator=creator.create_attempt,
        now_factory=lambda: NOW,
    )
    monkeypatch.setattr(connect, "_connector", lambda: provider)
    monkeypatch.setattr(connect, "get_connection_service", lambda **_kwargs: service)
    monkeypatch.setattr(zepp_pairing, "get_connector", lambda _source: provider)
    monkeypatch.setattr(zepp_pairing, "get_connection_service", lambda **_kwargs: service)
    from vitalis.entrypoints.api.app import app
    app.dependency_overrides[connect._connector] = lambda: provider
    try:
        yield SimpleNamespace(factory=factory, service=service, provider=provider)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_large_backfill_manifest_is_persisted_without_parameter_overflow(connection_store):
    from vitalis.adapters.zepp import ZeppConnector

    connector = ZeppConnector(mock=True)
    with connection_store.factory.begin() as db:
        repo = HealthRepository(db)
        attempt = connector.create_attempt(
            "large-backfill", days=730, trigger="pairing_initial", repository=repo,
        )
        repeated = connector.create_attempt(
            "large-backfill", days=730, trigger="pairing_initial", repository=repo,
        )
        assert attempt.id == repeated.id
        assert len(repo.sync_chunks(attempt.id, user_id="large-backfill")) == attempt.chunk_count
        assert attempt.chunk_count > 730
        assert len({chunk.stable_key for chunk in repo.sync_chunks(attempt.id, user_id="large-backfill")}) == attempt.chunk_count


@pytest.mark.parametrize("entry", ["oauth", "pairing", "token"])
@pytest.mark.parametrize("days", [None, 1, 90, 730])
def test_all_first_connections_share_bounded_window(client, connection_store, entry, days):
    user = f"window-{entry}-{days}"
    headers = {"X-User-Id": user}
    requested = 180 if days is None else days
    if entry == "oauth":
        auth = client.post(
            "/api/connect/zepp/authorize", headers=headers,
            params={} if days is None else {"sync_days": days},
        )
        assert auth.status_code == 200
        response = client.get(
            "/api/connect/zepp/callback",
            params={"state": auth.json()["state"], "code": "synthetic-code"},
        )
        assert response.status_code == 200, response.text
        attempt_id = response.json()["sync"]["attempt_id"]
    elif entry == "pairing":
        pairing = client.post(
            "/api/connect/zepp/pair", headers=headers,
            params={} if days is None else {"sync_days": days},
        ).json()
        response = client.post(
            f"/api/connect/zepp/pair/{pairing['pairing_code']}/credentials",
            json={"cookie": f'{{"userid":"vendor-{user}","apptoken":"synthetic"}}'},
        )
        assert response.status_code == 200, response.text
        attempt_id = response.json()["sync_attempt_id"]
    else:
        response = client.post(
            "/api/connect/zepp/token", headers=headers,
            json={"user_id": f"vendor-{user}", "app_token": "synthetic", **(
                {} if days is None else {"sync_days": days}
            )},
        )
        assert response.status_code == 200, response.text
        attempt_id = response.json()["sync"]["attempt_id"]
        assert response.json()["entrypoint"] == "advanced_token_import"
    with connection_store.factory() as db:
        row = db.get(SyncAttempt, attempt_id)
        assert (row.window_end.date() - row.window_start.date()).days == requested
    progress = client.get("/api/connect/zepp/progress", headers=headers)
    assert progress.status_code == 200, progress.text
    assert progress.json()["state"] == "backfill_requested"
    assert progress.json()["backfill"]["requested_days"] == requested


@pytest.mark.parametrize("days", [0, 731, -5])
def test_application_window_validation_precedes_side_effects(connection_store, days):
    service = connection_store.service
    with pytest.raises(ConnectionOperationError) as failed:
        service.create_pairing("invalid-window", days)
    assert failed.value.kind == "invalid_request"
    with pytest.raises(ConnectionOperationError):
        service.authorize("invalid-window", sync_days=days)
    with pytest.raises(ConnectionOperationError):
        service.import_token("invalid-window", sync_days=days, sync_history=False)
    with connection_store.factory() as db:
        assert db.query(SyncAttempt).count() == 0


def test_oauth_window_cannot_be_changed_after_state_creation(client, connection_store):
    auth = client.post(
        "/api/connect/zepp/authorize?sync_days=90", headers={"X-User-Id": "oauth-window-bound"},
    ).json()
    forged = auth["state"] + "-730"
    rejected = client.get(
        "/api/connect/zepp/callback", params={"state": forged, "code": "synthetic"},
    )
    assert rejected.status_code == 400
    accepted = client.get(
        "/api/connect/zepp/callback",
        params={"state": auth["state"], "code": "synthetic", "sync_days": 730},
    )
    assert accepted.status_code == 200
    with connection_store.factory() as db:
        attempt = db.get(SyncAttempt, accepted.json()["sync"]["attempt_id"])
        assert (attempt.window_end.date() - attempt.window_start.date()).days == 90


@pytest.mark.parametrize(("kind", "expected"), [
    ("auth", 401), ("network", 503), ("service", 503), ("timeout", 504),
    ("identity_conflict", 409), ("invalid_request", 400), ("vendor_response", 502),
])
@pytest.mark.parametrize("entry", ["oauth", "pairing", "token"])
def test_connection_errors_use_one_http_contract(client, connection_store, monkeypatch, kind, expected, entry):
    def fail(*_args, **_kwargs):
        raise ConnectionOperationError("synthetic-secret must never leak", kind=kind)

    user = f"error-{entry}-{kind}"
    monkeypatch.setattr(connection_store.provider, "verify_credentials", fail)
    monkeypatch.setattr(connection_store.provider, "exchange_code", fail)
    headers = {"X-User-Id": user}
    if entry == "oauth":
        state = client.post("/api/connect/zepp/authorize", headers=headers).json()["state"]
        response = client.get("/api/connect/zepp/callback", params={"state": state, "code": "synthetic"})
    elif entry == "pairing":
        code = client.post("/api/connect/zepp/pair", headers=headers).json()["pairing_code"]
        response = client.post(
            f"/api/connect/zepp/pair/{code}/credentials",
            json={"cookie": f'{{"userid":"vendor-{user}","apptoken":"synthetic"}}'},
        )
    else:
        response = client.post(
            "/api/connect/zepp/token", headers=headers,
            json={"user_id": f"vendor-{user}", "app_token": "synthetic"},
        )
    assert response.status_code == expected
    assert response.json()["state"] == "failed"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "synthetic-secret" not in response.text
    assert response.json()["retryable"] is (expected >= 500)


def test_token_import_in_mock_is_http_failure(client, monkeypatch):
    monkeypatch.setattr(connect, "_connector", lambda: SimpleNamespace(mock=True))
    response = client.post(
        "/api/connect/zepp/token", headers={"X-User-Id": "mock-advanced-import"},
        json={"cookie": "synthetic"},
    )
    assert response.status_code == 409
    assert response.json()["state"] == "failed"
    assert response.json()["failure_code"] == "conflict"


@pytest.mark.parametrize(("existing", "expired", "expected"), [
    (False, False, 404), (True, True, 410),
])
def test_pairing_submission_distinguishes_missing_and_expired(
    client, connection_store, existing, expired, expected,
):
    code = "unknown-pairing"
    if existing:
        code = client.post(
            "/api/connect/zepp/pair", headers={"X-User-Id": "expired-pairing"},
        ).json()["pairing_code"]
        with connection_store.factory.begin() as db:
            HealthRepository(db).pairing_session(code).expires_at = NOW.replace(tzinfo=None) - timedelta(seconds=1)
    response = client.post(
        f"/api/connect/zepp/pair/{code}/credentials", json={"cookie": "synthetic"},
    )
    assert response.status_code == expected


def test_progress_is_read_only_truthful_and_completes_after_bootstrap(client, connection_store):
    user = "truthful-progress"
    headers = {"X-User-Id": user}
    pairing = client.post("/api/connect/zepp/pair?sync_days=3", headers=headers).json()
    connected = client.post(
        f"/api/connect/zepp/pair/{pairing['pairing_code']}/credentials",
        json={"cookie": '{"userid":"vendor-truthful","apptoken":"synthetic"}'},
    ).json()
    attempt_id = connected["sync_attempt_id"]
    with connection_store.factory.begin() as db:
        repo = HealthRepository(db)
        for offset in (0, 1):
            day = DAY - timedelta(days=offset)
            repo.save_daily(NormalizedDaily(
                user_id=user, date=day,
                sleep=SleepRecord(user_id=user, date=day, sleep_duration=420),
                activity=ActivityRecord(user_id=user, date=day, steps=1234) if offset == 0 else None,
            ))
        row = db.get(SyncAttempt, attempt_id)
        row.status = "partial"
        row.started_at = NOW.replace(tzinfo=None) - timedelta(minutes=2)
        row.finished_at = NOW.replace(tzinfo=None) - timedelta(minutes=1)
        job = repo.enqueue_analysis_job(
            user, DAY, event_type="pairing_initial_sync", source="zepp",
            idempotency_key=f"sync-analysis:{attempt_id}:{DAY}:-",
        )
        job_id = job.id
    first = client.get("/api/connect/zepp/progress", headers=headers).json()
    with connection_store.factory() as db:
        diagnostic = [(row.id, str(row.created_at), row.delivery_period) for row in HealthRepository(db).analysis_jobs_for_target(user, DAY)]
    assert first["state"] == "bootstrap_analysis_queued", (first["bootstrap_analysis"], diagnostic, first["backfill"]["latency"])
    assert first["bootstrap_analysis"]["job_id"] == job_id
    assert first["coverage"]["sleep"]["available_days"] == 2
    assert first["coverage"]["activity"]["available_days"] == 1
    assert first["coverage"]["sleep"]["expected_days"] == 3
    assert first["coverage"]["sleep"]["coverage"] == pytest.approx(2 / 3)
    assert first["warmup"]["sleep_28d"]["ready"] is False
    assert first["first_report"]["ready"] is False
    with connection_store.factory() as db:
        before = (db.query(AnalysisJob).count(), db.query(AnalysisRun).count(), db.query(AnalysisSnapshot).count())
    for _ in range(2):
        assert client.get("/api/connect/zepp/progress", headers=headers).status_code == 200
        assert client.get("/api/connect/zepp/token", headers=headers).status_code == 200
    with connection_store.factory() as db:
        assert before == (db.query(AnalysisJob).count(), db.query(AnalysisRun).count(), db.query(AnalysisSnapshot).count())
    from vitalis.bootstrap import get_intelligence_command
    get_intelligence_command(now_factory=lambda: NOW, today_factory=lambda: DAY).analyze(user, DAY)
    ready = client.get("/api/connect/zepp/progress", headers=headers).json()
    assert ready["state"] == "first_report_ready"
    assert ready["first_report"]["ready"] is True
    assert ready["first_report"]["report_url"] == f"/api/reports/daily?day={DAY}"
    assert "synthetic-secret" not in str(ready)


def test_latency_metadata_keeps_unknown_values_and_utc_elapsed_time():
    from vitalis.application.connection_progress import latency_metadata
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    queued = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    started = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    finished = started.astimezone(UTC) + timedelta(minutes=5)
    measured = latency_metadata(created_at=queued, started_at=started, finished_at=finished, as_of=finished)
    assert measured["queue_latency_seconds"] == 3600
    assert measured["compute_latency_seconds"] == 300
    unknown = latency_metadata(created_at=queued, as_of=started)
    assert unknown["queue_latency_seconds"] is None
    assert unknown["compute_latency_seconds"] is None
    assert unknown["queue_age_seconds"] == 3600
