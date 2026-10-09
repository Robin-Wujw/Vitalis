"""HTTP contract tests for the explicit product-validation API."""

from datetime import date



DAY = date(2026, 9, 20)


def _goal_body(**overrides):
    body = {
        "confirmed": True,
        "goal_type": "weekly_running_sessions",
        "target_value": 3,
        "target_unit": "sessions",
        "target_date": "2026-10-20",
        "window_days": 7,
    }
    body.update(overrides)
    return body


def _feedback_body(**overrides):
    body = {
        "confirmed": True,
        "kind": "report_usefulness",
        "report_run_id": "synthetic-run-contract",
        "usefulness": "useful",
        "occurred_on": DAY.isoformat(),
    }
    body.update(overrides)
    return body


def _headers(user_id, token, key=None):
    result = {"Authorization": f"Bearer {token}", "X-User-Id": user_id}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def test_product_routes_are_mounted_with_stable_operation_contract(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/product/goals" in paths
    assert "/api/product/feedback" in paths
    assert "/api/product/summary" in paths
    assert "/api/product/metrics" in paths
    assert "/api/product/context" in paths
    assert paths["/api/product/goals"]["post"]["operationId"] == "create_product_goal"
    assert paths["/api/product/feedback"]["post"]["operationId"] == "record_product_feedback"


def test_product_reads_require_bearer_and_do_not_accept_x_user_id_alone(client):
    unauthenticated = client.get("/api/product/goals", headers={"X-User-Id": "product-api-no-auth"}, authenticate=False)
    assert unauthenticated.status_code == 401
    body = unauthenticated.json()
    assert body["state"] == "failed"
    assert body["failure_code"] == "unauthorized"
    assert body["retryable"] is False
    assert body["next_action"] == "authenticate"


def test_product_goal_requires_manage_scope_and_idempotency_key(client, issue_token):
    read_token = issue_token("product-api-manage", {"read"})
    forbidden = client.post(
        "/api/product/goals",
        json=_goal_body(),
        headers=_headers("product-api-manage", read_token, "product-api-missing-scope-001"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["failure_code"] == "forbidden"

    manage_token = issue_token("product-api-manage", {"manage"})
    missing_key = client.post(
        "/api/product/goals",
        json=_goal_body(),
        headers=_headers("product-api-manage", manage_token),
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["failure_code"] == "validation_error"

    invalid = client.post(
        "/api/product/goals",
        json={**_goal_body(), "confirmed": False},
        headers=_headers("product-api-manage", manage_token, "product-api-invalid-body-001"),
    )
    assert invalid.status_code == 422
    assert invalid.json()["failure_code"] == "validation_error"


def test_product_goal_create_replay_patch_and_user_scope(client, issue_token):
    owner = "product-api-owner"
    other = "product-api-other"
    owner_token = issue_token(owner, {"read", "manage", "feedback"})
    other_token = issue_token(other, {"read", "manage", "feedback"})
    headers = _headers(owner, owner_token, "product-api-goal-create-001")
    first = client.post("/api/product/goals", json=_goal_body(), headers=headers)
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["goal"]["source"] == "user_confirmed"
    assert first_body["goal"]["target_date"] == "2026-10-20"
    assert first_body["input_event"]["source"] == "user"
    replay = client.post("/api/product/goals", json=_goal_body(), headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first_body

    conflict = client.post(
        "/api/product/goals",
        json=_goal_body(target_value=4),
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["failure_code"] == "conflict"

    goal_id = first_body["goal"]["id"]
    cross_user = client.request(
        "PATCH",
        f"/api/product/goals/{goal_id}",
        json={"confirmed": True, "expected_revision": 1, "target_value": 4},
        headers=_headers(other, other_token, "product-api-cross-user-001"),
    )
    assert cross_user.status_code == 404
    assert cross_user.json()["failure_code"] == "not_found"

    patched = client.request(
        "PATCH",
        f"/api/product/goals/{goal_id}",
        json={"confirmed": True, "expected_revision": 1, "target_value": 4},
        headers=_headers(owner, owner_token, "product-api-goal-patch-001"),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["goal"]["revision"] == 2
    assert patched.json()["goal"]["target_value"] == 4

    stale = client.request(
        "PATCH",
        f"/api/product/goals/{goal_id}",
        json={"confirmed": True, "expected_revision": 1, "target_value": 5},
        headers=_headers(owner, owner_token, "product-api-goal-stale-001"),
    )
    assert stale.status_code == 409
    assert stale.json()["failure_code"] == "conflict"


def test_product_feedback_categories_are_explicit_and_not_channel_feedback(client, issue_token):
    user = "product-api-feedback"
    token = issue_token(user, {"read", "feedback"})
    missing_scope = client.post(
        "/api/product/feedback",
        json=_feedback_body(),
        headers=_headers(user, issue_token(user, {"read"}), "product-api-feedback-scope-001"),
    )
    assert missing_scope.status_code == 403

    no_key = client.post(
        "/api/product/feedback",
        json=_feedback_body(),
        headers=_headers(user, token),
    )
    assert no_key.status_code == 422

    unsupported = client.post(
        "/api/product/feedback",
        json={**_feedback_body(), "kind": "delivery_opened"},
        headers=_headers(user, token, "product-api-feedback-invalid-001"),
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["failure_code"] == "validation_error"

    assert client.get(
        "/api/product/feedback",
        headers=_headers(user, token),
    ).status_code == 200
    assert client.get(
        "/api/product/input-events",
        headers=_headers(user, token),
    ).status_code == 200


def test_product_context_summary_metrics_are_user_scoped_and_read_only(client, issue_token):
    owner = "product-api-read-owner"
    other = "product-api-read-other"
    owner_token = issue_token(owner, {"read", "manage", "feedback"})
    other_token = issue_token(other, {"read", "manage", "feedback"})
    owner_headers = _headers(owner, owner_token)
    other_headers = _headers(other, other_token)

    for path in ("/api/product/goals", "/api/product/feedback", "/api/product/input-events"):
        response = client.get(path, headers=owner_headers)
        assert response.status_code == 200, response.text

    for path in ("/api/product/summary", "/api/product/metrics", "/api/product/context"):
        response = client.get(path, headers=owner_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["user_id"] == owner
        assert owner not in str(payload.get("feedback", [])) if path.endswith("context") else True

    assert client.get("/api/product/goals", headers=other_headers).json()["goals"] == []
    assert client.get("/api/product/feedback", headers=other_headers).json() == []
    context = client.get("/api/product/context", headers=other_headers).json()
    assert context["user_id"] == other
    assert context["feedback"] == []

    future = client.get(
        "/api/product/metrics",
        params={"start": "2030-01-01", "end": "2030-01-01"},
        headers=owner_headers,
    )
    assert future.status_code == 422


def test_product_date_and_period_validation_use_real_http_errors(client, issue_token):
    user = "product-api-period"
    token = issue_token(user, {"read", "manage"})
    headers = _headers(user, token)
    reversed_period = client.get(
        "/api/product/summary",
        params={"start": "2026-09-20", "end": "2026-09-19"},
        headers=headers,
    )
    assert reversed_period.status_code == 422
    assert reversed_period.json()["failure_code"] == "validation_error"

    too_long = client.get(
        "/api/product/metrics",
        params={"start": "2025-01-01", "end": "2026-09-19"},
        headers=headers,
    )
    assert too_long.status_code == 422
    assert too_long.json()["failure_code"] == "validation_error"
