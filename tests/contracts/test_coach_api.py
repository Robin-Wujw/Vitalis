"""Dedicated coach authorization cannot imply any other write scope."""
from datetime import datetime, timedelta, timezone

import pytest

from vitalis.application.coach_operations import CoachError, CoachPolicy, CoachPolicyFile
from vitalis.entrypoints.api.routes import coach
from vitalis.time import local_today


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "coach-policy.json"
    policy = CoachPolicy(enabled=True, revision="api-test", allow_refresh=True,
                         allow_analyze=True, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    path.write_text(CoachPolicyFile(users={"coach-api": policy}).model_dump_json(), encoding="utf-8")
    # File trust is tested separately; this fixture isolates endpoint semantics on non-root CI.
    from vitalis.application.coach_operations import CoachOperationService
    from vitalis.adapters.persistence.coach_requests import CoachRequestRepository
    from vitalis.adapters.persistence import session_scope
    from vitalis.bootstrap import get_sync_job_service
    service = CoachOperationService(CoachRequestRepository(session_scope, get_sync_job_service()),
        today=local_today, policy_loader=lambda user: policy if user == "coach-api" else CoachPolicy())
    monkeypatch.setattr(coach, "get_coach_operation_service", lambda: service)
    monkeypatch.setenv("VITALIS_COACH_POLICY_FILE", str(path))
    return policy


def headers(issue_token, scopes):
    return {"Authorization": "Bearer " + issue_token("coach-api", scopes),
            "Idempotency-Key": "coach-api-stable-key"}


def test_read_policy_does_not_require_coach_or_manage(client, issue_token, monkeypatch):
    monkeypatch.delenv("VITALIS_COACH_POLICY_FILE", raising=False)
    response = client.get("/api/coach/policy", headers=headers(issue_token, ["read"]))
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["budget_unit"] == "root_requests"


@pytest.mark.parametrize("scopes", [["read"], ["manage"], ["analyze", "sync"]])
def test_only_dedicated_coach_scope_can_post(client, issue_token, scopes):
    response = client.post("/api/coach/operations", headers=headers(issue_token, scopes),
                           json={"operation": "analyze", "day": local_today().isoformat()})
    assert response.status_code == 403
    assert response.json()["state"] == "failed"


def test_coach_does_not_imply_manage_or_other_writes(client, issue_token):
    auth = headers(issue_token, ["coach"])
    for path, body in [("/api/analysis-runs", {}), ("/api/sync-jobs", {"days": 1}),
                       ("/api/jobs/not-owned/cancel", {}), ("/api/product/goals", {})]:
        assert client.post(path, headers=auth, json=body).status_code == 403
    # Coach authorization itself does not grant read either.
    assert client.get("/api/coach/policy", headers=auth).status_code == 403


@pytest.mark.parametrize("extra", [{"user_id": "other"}, {"notification_policy": "allow"},
                                    {"days": 28}, {"force": True}, {"execution_context": {}}])
def test_operation_forbids_client_policy_overrides(client, issue_token, extra):
    response = client.post("/api/coach/operations", headers=headers(issue_token, ["coach"]),
        json={"operation": "analyze", "day": local_today().isoformat(), **extra})
    assert response.status_code == 422


def test_policy_denial_is_http_failure(client, issue_token, monkeypatch):
    monkeypatch.delenv("VITALIS_COACH_POLICY_FILE", raising=False)
    response = client.post("/api/coach/operations", headers=headers(issue_token, ["coach"]),
        json={"operation": "analyze", "day": local_today().isoformat()})
    assert response.status_code == 403
    assert response.json()["state"] == "failed"


def test_dispatch_uncertainty_is_503_with_same_key_instruction(client, issue_token, monkeypatch):
    class Uncertain:
        def create(self, *args):
            raise CoachError("Dispatch uncertain; retry only the same Idempotency-Key", 503,
                             failure_code="dispatch_pending", retryable=True,
                             next_action="refresh_and_retry")
    monkeypatch.setattr(coach, "get_coach_operation_service", lambda: Uncertain())
    response = client.post("/api/coach/operations", headers=headers(issue_token, ["coach"]),
        json={"operation": "analyze", "day": local_today().isoformat()})
    assert response.status_code == 503
    assert response.json()["failure_code"] == "dispatch_pending"
    assert response.json()["retryable"] is True
    assert "same Idempotency-Key" in response.json()["message"]
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_cross_user_header_is_rejected(client, issue_token):
    auth = headers(issue_token, ["read", "coach"])
    auth["X-User-Id"] = "other"
    assert client.get("/api/coach/policy", headers=auth).status_code == 403


def test_accepted_operation_replays_without_implicit_manage(client, issue_token, policy_file):
    auth = headers(issue_token, ['coach'])
    body = {'operation': 'analyze', 'day': local_today().isoformat()}
    first = client.post('/api/coach/operations', headers=auth, json=body)
    assert first.status_code == 202, first.text
    second = client.post('/api/coach/operations', headers=auth, json=body)
    assert second.status_code == 202
    assert first.json()['request_id'] == second.json()['request_id']
    assert first.json()['job_id'] == second.json()['job_id']
    assert first.json()['policy_revision'] == 'api-test'
