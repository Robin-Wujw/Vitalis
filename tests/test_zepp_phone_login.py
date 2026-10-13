"""Synthetic tests for Zepp phone/email login and one-time handoff."""

from __future__ import annotations

import json
from urllib.parse import quote

import httpx
import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.zepp.login import (
    OFFICIAL_RESET_URL,
    ZeppLoginAdapter,
    ZeppLoginError,
    ZeppLoginResult,
)
from vitalis.domain import AuthToken
from vitalis.entrypoints.api.routes import connect

ACCOUNT = "synthetic@example.test"
PASSWORD = "Synthetic-password-123!"


def _success_payload() -> dict:
    return {
        "result": "ok",
        "token_info": {
            "app_token": "synthetic-app-token",
            "login_token": "synthetic-login-token",
            "user_id": "vendor-synthetic-1",
        },
        "domains": [{"cnames": ["api-mifitcn.zepp.com"]}],
        "regist_info": {"country_code": "CN"},
    }


def _adapter(handler):
    return ZeppLoginAdapter(transport=httpx.MockTransport(handler))


def test_official_two_step_login_maps_login_token_to_encrypted_refresh_field():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api-user.huami.com":
            assert str(request.url).split("?", 1)[0].endswith(
                "/registrations/" + quote(ACCOUNT, safe="") + "/tokens"
            )
            body = request.content.decode("utf-8")
            assert "password=" + quote(PASSWORD, safe="") in body
            assert request.headers.get("app_name") == "com.huami.webapp"
            assert PASSWORD not in str(request.url)
            return httpx.Response(
                200,
                json={"access": "synthetic-access", "country_code": "CN"},
                request=request,
            )
        assert request.url.host == "account.huami.com"
        assert request.url.path == "/v2/client/login"
        fields = request.content.decode("utf-8")
        assert "grant_type=access_token" in fields
        assert "code=synthetic-access" in fields
        return httpx.Response(200, json=_success_payload(), request=request)

    with _adapter(handler) as adapter:
        result = adapter.login(ACCOUNT, PASSWORD)

    assert len(requests) == 2
    assert result.source_user_id == "vendor-synthetic-1"
    assert result.region_host == "api-mifitcn.zepp.com"
    auth = result.to_auth_token("local-synthetic")
    assert auth.access_token == "synthetic-app-token"
    assert auth.refresh_token == "synthetic-login-token"
    assert PASSWORD not in repr(result)
    assert PASSWORD not in repr(auth)


def test_login_rejects_bad_password_without_echoing_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": 401, "message": PASSWORD}, request=request)

    with _adapter(handler) as adapter, pytest.raises(ZeppLoginError) as caught:
        adapter.login(ACCOUNT, PASSWORD)

    assert caught.value.failure_code == "invalid_credentials"
    assert caught.value.needs_reauth is True
    assert PASSWORD not in str(caught.value)
    assert PASSWORD not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_login_maps_429_and_retry_after_to_stable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"message": "rate limited"},
            request=request,
        )

    with _adapter(handler) as adapter, pytest.raises(ZeppLoginError) as caught:
        adapter.login(ACCOUNT, PASSWORD)

    assert caught.value.kind == "rate_limited"
    assert caught.value.failure_code == "rate_limited"
    assert caught.value.retry_after == 17


def test_login_refuses_captcha_and_points_to_official_reset():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": "captcha_required", "message": "captcha"},
            request=request,
        )

    with _adapter(handler) as adapter, pytest.raises(ZeppLoginError) as caught:
        adapter.login(ACCOUNT, PASSWORD)

    assert caught.value.failure_code == "captcha_required"
    assert caught.value.reset_url == OFFICIAL_RESET_URL
    assert caught.value.needs_reauth is True


def test_legacy_login_token_refresh_is_explicitly_unverified():
    adapter = ZeppLoginAdapter(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    try:
        with pytest.raises(ZeppLoginError) as caught:
            adapter.refresh(
                AuthToken(
                    user_id="local-synthetic",
                    access_token="app-token",
                    refresh_token="login-token",
                )
            )
    finally:
        adapter.close()

    assert caught.value.failure_code == "reauth_required"
    assert caught.value.needs_reauth is True
    assert caught.value.reset_url == OFFICIAL_RESET_URL


def test_oversized_upstream_response_is_rejected_before_json_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (64 * 1024 + 1), request=request)

    with _adapter(handler) as adapter, pytest.raises(ZeppLoginError) as caught:
        adapter.login(ACCOUNT, PASSWORD)

    assert caught.value.failure_code == "response_too_large"


def test_phone_page_has_no_external_scripts_and_no_password_echo(client):
    user_id = "phone-page-synthetic"
    created = client.post("/api/connect/zepp/pair", headers={"X-User-Id": user_id})
    assert created.status_code == 200
    code = created.json()["pairing_code"]

    response = client.get(f"/api/connect/zepp/phone?code={code}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "https://cdn" not in response.text
    assert "<script src=" not in response.text
    assert "current-password" in response.text
    assert PASSWORD not in response.text
    assert "script-src 'nonce-" in response.headers["content-security-policy"]


def test_phone_login_success_uses_pairing_claim_and_never_persists_password(
    client, monkeypatch
):
    user_id = "phone-login-success-synthetic"
    created = client.post("/api/connect/zepp/pair", headers={"X-User-Id": user_id})
    assert created.status_code == 200
    code = created.json()["pairing_code"]

    class FakeLogin:
        def login(self, account, password, *, country_code):
            assert account == ACCOUNT
            assert password == PASSWORD
            assert country_code == "CN"
            return ZeppLoginResult(
                source_user_id="vendor-phone-success",
                region_host="api-mifitcn.zepp.com",
                app_token="synthetic-app-token",
                login_token="synthetic-login-token",
            )

        def close(self):
            return None

    from vitalis.entrypoints.api.app import app

    fake = FakeLogin()
    app.dependency_overrides[connect._login_adapter] = lambda: fake
    try:
        response = client.post(
            f"/api/connect/zepp/pair/{code}/login",
            json={"account": ACCOUNT, "password": PASSWORD, "country_code": "CN"},
        )
    finally:
        app.dependency_overrides.pop(connect._login_adapter, None)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    assert PASSWORD not in response.text
    with session_scope() as db:
        token = HealthRepository(db).get_token(user_id)
        assert token is not None
        assert token.access_token == "synthetic-app-token"
        assert token.refresh_token == "synthetic-login-token"
        row = HealthRepository(db).pairing_session(code)
        assert row.status == "connected"
        assert PASSWORD not in json.dumps(row.message)


def test_phone_login_validation_does_not_echo_password(client):
    user_id = "phone-login-validation-synthetic"
    created = client.post("/api/connect/zepp/pair", headers={"X-User-Id": user_id})
    code = created.json()["pairing_code"]
    secret = "x" * 257
    response = client.post(
        f"/api/connect/zepp/pair/{code}/login",
        json={"account": ACCOUNT, "password": secret, "country_code": "CN"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.text


def _saved_token(**changes):
    values = dict(
        user_id="local-synthetic", source="zepp", access_token="old-app-token",
        refresh_token="synthetic-login-token", source_user_id="vendor-synthetic-1",
        region_host="api-mifitcn.zepp.com",
    )
    values.update(changes)
    return AuthToken(**values)


def test_login_token_renews_the_app_token_for_the_same_vendor_user():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v1/client/app_tokens"
        assert request.url.params["login_token"] == "synthetic-login-token"
        return httpx.Response(200, json={"token_info": {
            "app_token": "new-app-token", "user_id": "vendor-synthetic-1",
        }}, request=request)

    with _adapter(handler) as adapter:
        result = adapter.refresh(_saved_token())
    auth = result.to_auth_token("local-synthetic")
    assert auth.access_token == "new-app-token"
    assert auth.refresh_token == "synthetic-login-token"
    assert auth.region_host == "api-mifitcn.zepp.com"


def test_renewal_for_another_vendor_user_asks_for_a_new_login():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_info": {
            "app_token": "new-app-token", "user_id": "someone-else",
        }}, request=request)

    with _adapter(handler) as adapter, pytest.raises(ZeppLoginError) as caught:
        adapter.refresh(_saved_token())
    assert caught.value.failure_code == "reauth_required"


def test_plain_http_is_allowed_only_for_a_private_network_address():
    assert connect._private_origin("10.217.48.211") is True
    assert connect._private_origin("192.168.1.5") is True
    assert connect._private_origin("45.62.172.81") is False
    assert connect._private_origin("127.0.0.1") is False
    assert connect._private_origin("vitalis.example.com") is False


def test_expired_credential_without_renewal_sends_one_relogin_link_per_day(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from vitalis.adapters import notifications
    from vitalis.config import settings
    from vitalis.scheduler import jobs

    user_id = "relogin-synthetic"
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)
    sent = []

    class Repo:
        def __init__(self, _db):
            self._real = HealthRepository(_db)

        def sync_attempts(self, *_args, **_kwargs):
            return [SimpleNamespace(status="needs_reauth")]

        def get_token(self, *_args):
            return _saved_token(refresh_token="")

        def __getattr__(self, name):
            return getattr(self._real, name)

    class Service:
        def create_pairing(self, user, ttl_minutes):
            assert ttl_minutes == 24 * 60
            return {"phone_url": "/api/connect/zepp/phone?code=synthetic"}

    monkeypatch.setattr("vitalis.adapters.persistence.HealthRepository", Repo)
    monkeypatch.setattr(jobs, "_get_authorized_users", lambda: {user_id})
    monkeypatch.setattr("vitalis.bootstrap.get_connection_service", lambda: Service())
    monkeypatch.setattr(settings, "public_url", "http://10.217.48.211")
    monkeypatch.setattr(notifications.PushService, "push", lambda self, message: sent.append(message) or {})
    now = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
    assert jobs.reauth_job(now) == 1
    assert jobs.reauth_job(now + timedelta(hours=1)) == 0
    assert len(sent) == 1 and "http://10.217.48.211/api/connect/zepp/phone?code=synthetic" in sent[0].body
