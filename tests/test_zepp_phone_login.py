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
