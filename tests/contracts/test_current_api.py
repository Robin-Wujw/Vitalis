"""Current top-level API operations share persisted results and user ownership."""

from datetime import date
import inspect
import re

from fastapi import HTTPException

from vitalis.entrypoints.api.app import app
from vitalis.entrypoints.api.routes import current
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.time import local_day_utc_bounds


def assert_error(response, status, code, message, retryable=False):
    assert response.status_code == status
    assert response.json() == {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": response.headers["x-request-id"],
    }
    assert re.fullmatch(r"[0-9a-f]{32}", response.json()["request_id"])


OPERATIONS = {
    ("get", "/api/data-status"): "get_data_status",
    ("get", "/api/deliveries"): "list_notification_deliveries",
    ("get", "/api/reports/{kind}"): "get_report",
    ("post", "/api/analysis-runs"): "create_analysis_run",
    ("get", "/api/jobs/{job_id}"): "get_job",
    ("post", "/api/jobs/{job_id}/cancel"): "cancel_sync_job",
    ("post", "/api/sync-jobs"): "create_sync_job",
    ("get", "/api/workouts"): "list_workouts",
    ("get", "/api/workouts/{workout_id}"): "get_workout",
    ("post", "/api/feedback"): "create_feedback",
    ("post", "/api/bridge/batches"): "ingest_bridge_batch",
}


def test_current_operation_ids_are_unique_and_match_contract():
    paths = app.openapi()["paths"]
    seen = [operation["operationId"] for methods in paths.values() for operation in methods.values()]
    assert len(seen) == len(set(seen))
    for (method, path), operation_id in OPERATIONS.items():
        assert paths[path][method]["operationId"] == operation_id


def test_feedback_post_delegates_transaction_to_application_action():
    source = inspect.getsource(current.create_feedback)
    assert "session_scope" not in source
    assert "HealthRepository" not in source
    assert "create_or_reuse_feedback_request" not in source
    assert "get_intelligence_action().log_feedback" in source


def test_current_health_reads_delegate_to_health_query_not_health_routes():
    for function in (
        current.get_data_status,
        current.list_workouts,
        current.get_workout,
    ):
        source = inspect.getsource(function)
        assert "get_health_query()" in source
        assert "health_data_health" not in source
        assert "health_workouts" not in source
        assert "health_workout_detail" not in source


def test_superseded_report_aliases_are_absent_from_current_api():
    paths = app.openapi()["paths"]
    for alias in (
        "daily", "weekly", "monthly", "morning-briefing",
        "evening-briefing", "weekly-briefing", "monthly-briefing",
    ):
        assert f"/api/intelligence/{alias}" not in paths


def test_openapi_uses_one_error_schema_for_current_operations():
    schema = app.openapi()
    error_schema = schema["components"]["schemas"]["APIError"]
    assert set(error_schema["required"]) == {"code", "message", "retryable", "request_id"}
    assert set(error_schema["properties"]) == set(error_schema["required"])
    for (method, path) in OPERATIONS:
        responses = schema["paths"][path][method]["responses"]
        for status in ("401", "403", "404", "409", "422", "500"):
            assert responses[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/APIError"
            }
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_read_status_does_not_generate_report_or_sync(client):
    headers = {"X-User-Id": "current-status-user"}
    status = client.get("/api/data-status", headers=headers)
    assert status.status_code == 200
    assert status.json()["latest_attempt"] is None
    deliveries = client.get("/api/deliveries", headers=headers)
    assert deliveries.status_code == 200 and deliveries.json() == []
    missing = client.get("/api/reports/daily", headers=headers)
    assert_error(missing, 404, "not_found", "Resource not found")
    invalid_kind = client.get("/api/reports/not-a-kind", headers=headers)
    assert_error(invalid_kind, 422, "validation_error", "Invalid request data")
    assert client.get("/api/workouts", headers=headers).status_code == 200


def test_queued_sync_and_job_are_read_only_and_scoped(client):
    headers = {"X-User-Id": "current-sync-user", "Idempotency-Key": "current-sync-request-one"}
    queued = client.post("/api/sync-jobs", json={"days": 1}, headers=headers)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    own = client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": "current-sync-user"})
    assert own.status_code == 200
    assert own.json()["kind"] == "sync"
    assert client.get(
        f"/api/jobs/{job_id}", headers={"X-User-Id": "current-sync-other"}
    ).status_code == 404
    repeated = client.post("/api/sync-jobs", json={"days": 1}, headers=headers)
    assert repeated.status_code == 202
    assert repeated.json()["job_id"] == job_id
    conflicting = client.post("/api/sync-jobs", json={"days": 2}, headers=headers)
    assert_error(conflicting, 409, "conflict", "Request conflicts with existing state")
    assert client.get(f"/api/jobs/{job_id}", headers=headers).status_code == 200


def test_sync_idempotency_header_at_maximum_length_does_not_overflow_storage(client):
    headers = {"X-User-Id": "sync-long-key-user", "Idempotency-Key": "k" * 128}
    response = client.post("/api/sync-jobs", json={"days": 1}, headers=headers)
    assert response.status_code == 202, response.text
    again = client.post("/api/sync-jobs", json={"days": 1}, headers=headers)
    assert again.json()["job_id"] == response.json()["job_id"]
    invalid = client.post("/api/sync-jobs", json={"days": 1}, headers={
        **headers, "Idempotency-Key": "k" * 129,
    })
    assert_error(invalid, 422, "validation_error", "Invalid request data")


def test_removed_legacy_connect_aliases_are_not_current_contract(client):
    headers = {
        "X-User-Id": "removed-connect-user",
        "Idempotency-Key": "removed-connect-request",
    }
    assert client.post(
        "/api/connect/zepp", json={"sync_history": True}, headers=headers
    ).status_code == 404
    assert client.get(
        "/api/connect/zepp?user=removed-connect-user", headers=headers
    ).status_code == 404


def test_sync_job_explicit_window_overrides_days_and_is_bounded(client):
    user_id = "current-window-user"
    key = "current-window-request"
    response = client.post(
        "/api/sync-jobs",
        json={"days": 730, "from": "2026-08-01", "to": "2026-08-03"},
        headers={"X-User-Id": user_id, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    start, _ = local_day_utc_bounds(date.fromisoformat("2026-08-01"))
    _, end = local_day_utc_bounds(date.fromisoformat("2026-08-03"))
    with session_scope() as db:
        attempt = HealthRepository(db).sync_attempt(job_id, user_id=user_id)
        assert attempt is not None
        assert attempt.window_start == start.replace(tzinfo=None)
        assert attempt.window_end == end.replace(tzinfo=None)


def test_sync_cancel_is_atomic_and_user_scoped(client):
    owner = "current-cancel-owner"
    other = "current-cancel-other"
    created = client.post(
        "/api/sync-jobs", json={"days": 1},
        headers={"X-User-Id": owner, "Idempotency-Key": "current-cancel-request"},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["job_id"]
    assert client.get(
        f"/api/jobs/{job_id}", headers={"X-User-Id": other}
    ).status_code == 404
    denied = client.post(
        f"/api/jobs/{job_id}/cancel", headers={"X-User-Id": other}
    )
    assert denied.status_code == 404
    still_queued = client.get(
        f"/api/jobs/{job_id}", headers={"X-User-Id": owner}
    )
    assert still_queued.status_code == 200
    assert still_queued.json()["attempt"]["status"] == "queued"
    cancelled = client.post(
        f"/api/jobs/{job_id}/cancel", headers={"X-User-Id": owner}
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"job_id": job_id, "cancel_requested": True}
    status = client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": owner})
    assert status.json()["attempt"]["status"] == "cancelled"


def test_analysis_job_worker_persists_report_and_rejects_other_user(client):
    from vitalis.application.jobs import drain_analysis_jobs

    headers = {"X-User-Id": "current-analysis-user", "Idempotency-Key": "current-analysis-request-one"}
    queued = client.post("/api/analysis-runs", json={"day": "2026-08-29"}, headers=headers)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    assert client.get("/api/reports/daily?day=2026-08-29", headers=headers).status_code == 404
    assert client.get(f"/api/jobs/{job_id}", headers={"X-User-Id": "other-analysis-user"}).status_code == 404
    assert drain_analysis_jobs(max_jobs=1) == 1
    result = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"
    report = client.get("/api/reports/daily?day=2026-08-29", headers=headers)
    assert report.status_code == 200
    assert report.json()["analysis_run_id"] == result.json()["analysis_run_id"]
    repeated = client.post("/api/analysis-runs", json={"day": "2026-08-29"}, headers=headers)
    assert repeated.json()["job_id"] == job_id


def test_feedback_write_requires_scope_and_bridge_needs_device_token(client, issue_token):
    limited = issue_token("current-feedback-user", {"read"})
    response = client.post(
        "/api/feedback", json={"date": "2026-08-29", "notes": "synthetic test"},
        headers={"Authorization": f"Bearer {limited}"},
    )
    assert_error(response, 403, "forbidden", "Access denied")
    permitted = issue_token("current-feedback-user", {"feedback"})
    response = client.post(
        "/api/feedback", json={"date": "2026-08-29", "notes": "synthetic test"},
        headers={"Authorization": f"Bearer {permitted}"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["notes"] == "synthetic test"
    bridge = client.post("/api/bridge/batches", json={"protocol_version": 2, "samples": [{}]})
    assert_error(bridge, 401, "unauthorized", "Authentication required")


def test_bad_pairing_and_malformed_requests_never_echo_supplied_values(client):
    secret = "private-token-health-and-vendor-payload"
    headers = {"X-User-Id": "current-errors-user", "Idempotency-Key": "current-errors-key-01"}
    invalid = client.post("/api/analysis-runs", json={"day": secret}, headers=headers)
    assert_error(invalid, 422, "validation_error", "Invalid request data")
    malformed = client.post(
        "/api/analysis-runs", content=f'{{"day": "{secret}"',
        headers={**headers, "Content-Type": "application/json"},
    )
    assert_error(malformed, 422, "validation_error", "Invalid request data")
    pairing = client.post("/api/connect/zepp/pair", headers=headers)
    assert pairing.status_code == 200
    bad_cookie = client.post(
        f'/api/connect/zepp/pair/{pairing.json()["pairing_code"]}/credentials',
        json={"cookie": secret},
    )
    assert_error(bad_cookie, 400, "bad_request", "Invalid request")
    for response in (invalid, malformed, bad_cookie):
        assert secret not in response.text
        assert "detail" not in response.json()


def test_unknown_routes_have_real_status_and_unique_server_request_ids(client):
    first = client.get("/api/no-such-resource", headers={"X-Request-ID": "untrusted-id"})
    second = client.get("/api/no-such-resource")
    assert_error(first, 404, "not_found", "Resource not found")
    assert_error(second, 404, "not_found", "Resource not found")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert first.headers["x-request-id"] != "untrusted-id"
    method = client.request("DELETE", "/api/data-status")
    assert_error(method, 405, "method_not_allowed", "Method not allowed")


def test_raised_exceptions_cannot_expose_vendor_or_sql_contents(client, monkeypatch):
    from vitalis.entrypoints.api.routes import current

    secret = "Bearer token-cookie-health-payload-SQL-select"
    headers = {"X-User-Id": "current-error-owner", "Origin": "http://localhost:8000"}

    def conflict(_user_id):
        raise HTTPException(status_code=409, detail={"vendor_response": secret})

    class Query:
        def __init__(self, callback):
            self._callback = callback

        def data_status(self, user_id):
            return self._callback(user_id)

    monkeypatch.setattr(current, "get_health_query", lambda: Query(conflict))
    response = client.get("/api/data-status", headers=headers)
    assert_error(response, 409, "conflict", "Request conflicts with existing state")
    assert secret not in response.text
    assert response.headers["access-control-allow-origin"] == headers["Origin"]
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]

    def crash(_user_id):
        raise RuntimeError(secret)

    monkeypatch.setattr(current, "get_health_query", lambda: Query(crash))
    failed = client.get("/api/data-status", headers=headers)
    assert_error(failed, 500, "internal_error", "Internal server error")
    assert secret not in failed.text
    assert failed.headers["access-control-allow-origin"] == headers["Origin"]
