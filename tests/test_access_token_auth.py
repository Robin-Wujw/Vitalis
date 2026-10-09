"""API bearer identity, scopes, and purpose-bound pairing access."""

from datetime import datetime, timedelta, timezone

import pytest

from vitalis.domain import Workout, WorkoutType
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.access_tokens import (
    provision_access_token, revoke_access_token, token_digest,
)
from vitalis.adapters.persistence.models import AccessToken, OAuthState, ZeppPairingSession


def test_missing_or_invalid_bearer_never_trusts_user_selector(client):
    path = "/api/intelligence/profile"
    missing = client.get(path)
    assert missing.status_code == 401
    assert missing.json() == {
        "state": "failed", "failure_code": "unauthorized", "message": "Authentication required",
        "retryable": False, "next_action": "authenticate", "request_id": missing.headers["x-request-id"],
    }
    without_token = client.get(
        path, headers={"X-User-Id": "auth-without-token"}, authenticate=False
    )
    assert without_token.status_code == 401
    assert without_token.json()["failure_code"] == "unauthorized"
    for value in ("Basic xyz", "Bearer not-a-real-token", "Bearer "):
        response = client.get(path, headers={"Authorization": value})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["failure_code"] == "unauthorized"
        assert response.json()["message"] == "Authentication required"
        assert value not in response.text


def test_token_owner_not_overridden_by_user_header(client, issue_token):
    token = issue_token("auth-owner", {"read"})
    own = client.get(
        "/api/intelligence/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert own.status_code == 200
    assert own.json()["user_id"] == "auth-owner"

    forged = client.get(
        "/api/intelligence/profile",
        headers={"Authorization": f"Bearer {token}", "X-User-Id": "auth-other"},
    )
    assert forged.status_code == 403
    assert forged.json() == {
        "state": "failed", "failure_code": "forbidden", "message": "Access denied",
        "retryable": False, "next_action": "request_scope", "request_id": forged.headers["x-request-id"],
    }
    assert "auth-other" not in forged.text


def test_cross_user_resource_and_pairing_ids_are_denied(client, issue_token):
    owner_token = issue_token("auth-resource-owner", {"read", "manage"})
    other_token = issue_token("auth-resource-other", {"read"})
    with session_scope() as db:
        HealthRepository(db).save_workout(Workout(
            user_id="auth-resource-owner", workout_id="auth-private-workout",
            type=WorkoutType.RUNNING, duration=30,
        ))
    path = "/api/workouts/auth-private-workout?source=zepp"
    owner = client.get(path, headers={"Authorization": f"Bearer {owner_token}"})
    other = client.get(path, headers={"Authorization": f"Bearer {other_token}"})
    assert owner.status_code == 200
    assert other.status_code == 404

    created = client.post(
        "/api/connect/zepp/pair",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    code = created.json()["pairing_code"]
    denied = client.get(
        f"/api/connect/zepp/pair/{code}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert denied.status_code == 404


@pytest.mark.parametrize("path,method,json_body,scopes", [
    ("/api/intelligence/profile", "GET", None, {"sync"}),
    ("/api/analysis-runs", "POST", {}, {"read"}),
    ("/api/sync-jobs", "POST", {"days": 1}, {"read"}),
    ("/api/feedback", "POST", {"date": "2026-08-28", "notes": "ok"}, {"read"}),
    ("/api/connect/zepp/pair", "POST", None, {"read"}),
])
def test_missing_scope_denies_route(client, issue_token, path, method, json_body, scopes):
    token = issue_token(f"scope-{method}-{path.replace('/', '-')}", scopes)
    response = client.request(
        method, path, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "scope-test-request-one"},
        **({"json": json_body} if json_body is not None else {}),
    )
    assert response.status_code == 403
    assert response.json()["failure_code"] == "forbidden"
    assert response.json()["retryable"] is False
    assert token not in response.text


def test_revoked_and_expired_tokens_are_rejected(client, issue_token):
    revoked = issue_token("auth-revoked", {"read"})
    expiring = issue_token("auth-expired", {"read"})
    with session_scope() as db:
        row = db.get(AccessToken, token_digest(revoked))
        assert row is not None
        assert row.token_digest != revoked
        assert row.scopes == ["read"]
        assert revoke_access_token(db, row.token_digest)
        assert not revoke_access_token(db, row.token_digest)
        db.get(AccessToken, token_digest(expiring)).expires_at = datetime.utcnow() - timedelta(seconds=1)
    for token in (revoked, expiring):
        response = client.get(
            "/api/intelligence/profile",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401


def test_removed_user_cannot_keep_using_an_orphaned_access_token(client, issue_token):
    from vitalis.adapters.persistence.models import User

    token = issue_token("auth-removed-owner", {"read"})
    with session_scope() as db:
        db.delete(db.get(User, "auth-removed-owner"))
    response = client.get(
        "/api/intelligence/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_provisioning_validates_user_scope_and_expiry():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    with session_scope() as db:
        for user, scopes, expiry in (
            ("missing-auth-user", {"read"}, future),
            ("", {"read"}, future),
            ("auth-existing", {"admin"}, future),
            ("auth-existing", set(), future),
            ("auth-existing", {"read"}, datetime.utcnow()),
            ("auth-existing", {"read"}, datetime.now(timezone.utc) - timedelta(seconds=1)),
        ):
            if user == "auth-existing":
                HealthRepository(db).upsert_user(user)
                db.flush()
            with pytest.raises(ValueError):
                provision_access_token(db, user, scopes, expires_at=expiry)


def test_scan_read_cannot_create_state_and_post_is_owner_scoped(client, issue_token):
    user = "auth-scan-target"
    token = issue_token("auth-scan-attacker", {"manage"})
    scan_path = f"/api/connect/zepp/scan?user={user}"
    assert client.get(scan_path, authenticate=False).status_code == 422
    assert client.post("/api/connect/zepp/scan", authenticate=False).status_code == 401
    assert client.post(
        "/api/connect/zepp/scan", headers={
            "Authorization": f"Bearer {token}", "X-User-Id": user,
        },
    ).status_code == 403
    assert client.get(
        f"/api/connect/zepp?user={user}", authenticate=False
    ).status_code == 404
    with session_scope() as db:
        assert db.query(OAuthState).filter_by(user_id=user).count() == 0
        assert db.query(ZeppPairingSession).filter_by(user_id=user).count() == 0


def test_pairing_scan_code_is_read_only_short_lived_capability(client, issue_token):
    token = issue_token("auth-code-owner", {"manage"})
    created = client.post(
        "/api/connect/zepp/pair", headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code == 200
    code = created.json()["pairing_code"]
    scan_url = created.json()["scan_url"]
    assert scan_url == f"/api/connect/zepp/scan?code={code}"
    with session_scope() as db:
        assert db.query(ZeppPairingSession).filter_by(user_id="auth-code-owner").count() == 1
    scan = client.get(scan_url)
    assert scan.status_code == 200
    assert code in scan.text
    assert "'X-User-Id'" not in scan.text
    assert client.get(f"/api/connect/zepp/pair/{code}").json()["status"] == "waiting"
    assert client.get(
        f"/api/connect/zepp/pair/{code}",
        headers={"X-User-Id": "auth-code-owner"}, authenticate=False,
    ).status_code == 401
    with session_scope() as db:
        assert db.query(ZeppPairingSession).filter_by(user_id="auth-code-owner").count() == 1
        db.get(ZeppPairingSession, code).expires_at = datetime.utcnow() - timedelta(seconds=1)
    assert client.get(scan_url).status_code == 410
    assert client.get(f"/api/connect/zepp/pair/{code}").status_code == 410


def test_pairing_status_get_does_not_persist_expiry_or_replace_revocation(client):
    user_id = "pairing-read-only-owner"
    code = client.post(
        "/api/connect/zepp/pair", headers={"X-User-Id": user_id},
    ).json()["pairing_code"]
    with session_scope() as db:
        db.get(ZeppPairingSession, code).expires_at = datetime.utcnow() - timedelta(seconds=1)
    expired = client.get(f"/api/connect/zepp/pair/{code}", headers={"X-User-Id": user_id})
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    with session_scope() as db:
        row = db.get(ZeppPairingSession, code)
        assert row.status == "waiting"
        row.status = "revoked"
        row.message = "配对已撤销"
    revoked = client.get(f"/api/connect/zepp/pair/{code}", headers={"X-User-Id": user_id})
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    with session_scope() as db:
        assert db.get(ZeppPairingSession, code).status == "revoked"


def test_source_revoke_has_one_canonical_route(client):
    response = client.post(
        "/api/connect/zepp/revoke",
        headers={"X-User-Id": "canonical-revoke-user"},
    )
    assert response.status_code == 404
