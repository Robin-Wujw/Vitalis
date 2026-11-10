"""Business operations remain available through the authenticated API."""

from datetime import date
import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope


@pytest.fixture(autouse=True)
def clean_business_operation_users():
    yield
    with session_scope() as db:
        repo = HealthRepository(db)
        for owner in ("wake-api-owner", "research-owner", "research-other", "research-request"):
            repo.delete_for_user(owner)


def test_wake_review_reads_only_the_requested_owner_and_bounded_window(client):
    response = client.get(
        "/api/health/wake-review?from=2026-10-01&to=2026-10-03",
        headers={"X-User-Id": "wake-api-owner"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "wake-api-owner"
    assert len(body["days"]) == 3
    assert body["passes"] is False
    assert body["sample_count"] == 0
    assert client.get(
        "/api/health/wake-review?from=2025-01-01&to=2026-10-03",
        headers={"X-User-Id": "wake-api-owner"},
    ).status_code == 422


def test_research_read_keeps_shadow_labels_and_never_runs_analysis(client, monkeypatch):
    from vitalis.application.shadow_batch import ShadowBatch

    monkeypatch.setattr(ShadowBatch, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("GET computed")))
    with session_scope() as db:
        repo = HealthRepository(db)
        for owner in ("research-owner", "research-other"):
            repo.upsert_user(owner)
            db.flush()
            repo.save_shadow_result(owner, date(2026, 10, 1), "readiness", "test", {"score": 60})
    response = client.get(
        "/api/intelligence/research/readiness?from=2026-10-01&to=2026-10-03",
        headers={"X-User-Id": "research-owner"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["qualification"] == "shadow_only_unvalidated"
    assert body["sample_count"] == 1
    assert body["coverage"] == 1 / 3
    assert "research-other" not in str(body)


def test_api_can_enqueue_research_without_computing_in_request(client):
    from vitalis.adapters.persistence.models import AnalysisJob

    with session_scope() as db:
        HealthRepository(db).upsert_user("research-request")
    headers = {"X-User-Id": "research-request", "Idempotency-Key": "synthetic-research-key"}
    response = client.post("/api/analysis-runs", json={"day": "2026-10-01", "refresh_research": True}, headers=headers)
    assert response.status_code == 202
    with session_scope() as db:
        row = db.get(AnalysisJob, response.json()["job_id"])
        assert row.status == "queued"
        assert row.execution_context["refresh_research"] is True
        assert row.execution_context["notification_policy"] == "suppress"
    conflict = client.post("/api/analysis-runs", json={"day": "2026-10-01"}, headers=headers)
    assert conflict.status_code == 409


def test_manual_sync_api_carries_notification_suppression(client, monkeypatch):
    from types import SimpleNamespace
    from vitalis.entrypoints.api.routes import current

    commands = []
    service = SimpleNamespace(create=lambda command: commands.append(command) or
        SimpleNamespace(as_dict=lambda: {"job_id": "synthetic-sync", "status": "queued"}))
    monkeypatch.setattr(current, "get_sync_job_service", lambda: service)
    response = client.post("/api/sync-jobs", json={"days": 7}, headers={
        "X-User-Id": "research-request", "Idempotency-Key": "synthetic-sync-api-key"})
    assert response.status_code == 202
    assert commands[0].execution_context["notification_policy"] == "suppress"
