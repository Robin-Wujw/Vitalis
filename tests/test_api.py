"""API 端到端测试：mock Zepp 下完整走通 连接 -> 同步 -> 查询 -> 分析。"""

import hashlib
from datetime import datetime, timezone
import importlib

from starlette.requests import Request

from vitalis.config import settings
from vitalis.storage import HealthRepository, session_scope


def test_connect_and_sync(client):
    resp = client.post("/api/v1/connect/zepp", json={"source": "zepp", "sync_history": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected"
    assert body["auth_mode"] == "mock"
    assert body["sync"]["days_synced"] > 0
    assert "profile" in body


def test_health_today(client):
    client.post("/api/v1/connect/zepp", json={"sync_history": True})
    resp = client.get("/api/v1/health/today", headers={"X-User-Id": "001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] is not None
    assert body["training"] in {"full", "moderate", "easy", "not_ready"}
    assert body["stress"] in {"low", "medium", "high"}


def test_health_today_second_user(client):
    """多用户隔离：新用户无数据时返回 no_data。"""
    resp = client.get("/api/v1/health/today", headers={"X-User-Id": "999"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] is None
    assert body["training"] == "no_data"


def test_analyze(client):
    client.post("/api/v1/connect/zepp", json={"sync_history": True})
    resp = client.post(
        "/api/v1/analyze",
        json={"agent_query": "我今天适合跑步吗？"},
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "explanation" in body
    assert body["engine"].startswith("rule+statistical")
    # LLM 未配置时应为模板回退，不允许出现空解释
    assert body["explanation"]


def test_unknown_source(client):
    resp = client.post("/api/v1/connect/nonexistent", json={"sync_history": False})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_root_lists_sources(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "zepp" in resp.json()["available_sources"]


def test_browser_get_connect_zepp_opens_pairing_page(client):
    response = client.get("/api/v1/connect/zepp?user=browser-user")
    assert response.status_code == 200
    assert "连接 Zepp 健康数据" in response.text


async def test_app_lifespan_initializes_database(monkeypatch):
    app_module = importlib.import_module("vitalis.api.app")
    calls = []
    monkeypatch.setattr(app_module, "init_db", lambda: calls.append("initialized"))

    candidate = app_module.create_app()
    async with candidate.router.lifespan_context(candidate):
        assert calls == ["initialized"]


def test_public_base_url_prefers_https_configuration(monkeypatch):
    from vitalis.api.routes.connect import _public_base_url

    request = Request(
        {
            "type": "http",
            "scheme": "http",
            "method": "GET",
            "path": "/api/v1/connect/zepp/scan",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8000")],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 12345),
        }
    )
    monkeypatch.setattr(settings, "public_url", "https://health.example.com")

    assert _public_base_url(request) == "https://health.example.com"


# ---- 新 API：健康查询 / 同步 / 聚合 ----

def test_health_sync(client):
    """POST /health/sync 手动触发增量同步。"""
    # 先扫码授权保存 mock token
    au = client.get("/api/v1/connect/zepp/authorize", headers={"X-User-Id": "sync-user"}).json()
    client.get("/api/v1/connect/zepp/callback", params={"code": "mock-sync", "state": au["state"]})
    resp = client.post("/api/v1/health/sync?days=3", headers={"X-User-Id": "sync-user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "synced"
    assert "streams" in body
    assert body["success"] is True


def test_health_token_status_authorized(client):
    """GET /health/token-status 已授权用户。"""
    # 先扫码授权保存 mock token
    au = client.get("/api/v1/connect/zepp/authorize", headers={"X-User-Id": "tok-user"}).json()
    client.get("/api/v1/connect/zepp/callback", params={"code": "mock-tok", "state": au["state"]})
    resp = client.get("/api/v1/health/token-status", headers={"X-User-Id": "tok-user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is True
    assert body["valid"] is True  # mock 模式下始终有效


def test_health_token_status_unauthorized(client):
    """GET /health/token-status 未授权用户。"""
    resp = client.get("/api/v1/health/token-status", headers={"X-User-Id": "nope"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is False


def test_health_range(client):
    """GET /health/range 多级聚合查询。"""
    client.post("/api/v1/connect/zepp", json={"sync_history": True})
    resp = client.get(
        "/api/v1/health/range?from=2024-01-01&to=2024-01-07&granularity=1d",
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "1d"
    assert isinstance(body["blocks"], list)


def test_health_range_rejects_over_2years(client):
    """GET /health/range 跨度超过 730 天应拒绝。"""
    resp = client.get(
        "/api/v1/health/range?from=2020-01-01&to=2024-01-01&granularity=30d",
        headers={"X-User-Id": "001"},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---- 扫码授权流程（mock 模式） ----

def test_authorize_returns_scan_url(client):
    resp = client.get("/api/v1/connect/zepp/authorize", headers={"X-User-Id": "001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scan_required"
    assert body["authorize_url"].startswith("http")
    assert body["state"]  # 一次性 state


def test_callback_saves_token_and_syncs(client):
    # 1. 发起扫码拿到 state
    au = client.get("/api/v1/connect/zepp/authorize", headers={"X-User-Id": "001"}).json()
    state = au["state"]
    # 2. 模拟 Zepp 授权回调（mock 模式下任意 code 换 token）
    cb = client.get(
        "/api/v1/connect/zepp/callback",
        params={"code": "mock-code-abc12345", "state": state},
        headers={"X-User-Id": "001"},
    )
    assert cb.status_code == 200
    body = cb.json()
    assert body["status"] == "authorized"
    assert body["token_saved"] is True
    assert body["source_user_id"] == "mock-user-001"
    assert body["sync"]["days_synced"] > 0

    # 3. token 状态已持久化
    tok = client.get("/api/v1/connect/zepp/token", headers={"X-User-Id": "001"})
    assert tok.json()["authorized"] is True
    assert tok.json()["source_user_id"] == "mock-user-001"


def test_callback_rejects_reused_state(client):
    au = client.get("/api/v1/connect/zepp/authorize", headers={"X-User-Id": "002"}).json()
    state = au["state"]
    ok = client.get("/api/v1/connect/zepp/callback", params={"code": "c1", "state": state}).json()
    assert ok["status"] == "authorized"
    # 同一 state 再次使用应失败
    again = client.get("/api/v1/connect/zepp/callback", params={"code": "c2", "state": state})
    assert again.status_code == 400


def test_token_status_when_not_authorized(client):
    resp = client.get("/api/v1/connect/zepp/token", headers={"X-User-Id": "099"})
    assert resp.json()["authorized"] is False


# ---- 网页扫码页 + 二维码 ----

def test_scan_page_renders_html(client):
    resp = client.get("/api/v1/connect/zepp/scan?user=007")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "连接 Zepp 健康数据" in body
    assert "/api/v1/connect/zepp/qrcode.png?state=" in body
    assert "mockBtn" in body  # mock 模式显示模拟授权按钮


def test_qrcode_png_generated(client):
    # 先创建 state（用 scan 页拿到 state，从 img src 提取）
    page = client.get("/api/v1/connect/zepp/scan?user=007").text
    import re

    state = re.search(r"qrcode\.png\?state=([a-zA-Z0-9_-]+)", page).group(1)
    resp = client.get(f"/api/v1/connect/zepp/qrcode.png?state={state}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG 魔数


def test_qrcode_png_rejects_unknown_state(client):
    resp = client.get("/api/v1/connect/zepp/qrcode.png?state=not-exist-state")
    assert resp.status_code == 404


def test_scan_page_full_flow(client):
    """网页扫码完整链路：页面 state -> 回调授权 -> token 生效 -> 同步完成。"""
    import re

    page = client.get("/api/v1/connect/zepp/scan?user=008").text
    state = re.search(r"qrcode\.png\?state=([a-zA-Z0-9_-]+)", page).group(1)
    qr = client.get(f"/api/v1/connect/zepp/qrcode.png?state={state}")
    assert qr.status_code == 200

    cb = client.get(
        "/api/v1/connect/zepp/callback",
        params={"code": "mock-scan-001", "state": state},
        headers={"X-User-Id": "008"},
    )
    assert cb.status_code == 200
    assert cb.json()["status"] == "authorized"

    tok = client.get("/api/v1/connect/zepp/token", headers={"X-User-Id": "008"})
    assert tok.json()["authorized"] is True
    assert tok.json()["expired"] is False


# ---- 云端配对（浏览器书签 / 扩展共用） ----

def test_cloud_pairing_one_time_flow(client):
    created = client.post(
        "/api/v1/connect/zepp/pair?sync_days=3",
        headers={"X-User-Id": "pair-user"},
    )
    assert created.status_code == 200
    code = created.json()["pairing_code"]
    cookie = '{"token_info":{"userid":"vendor-42","apptoken":"secret-token"}}'

    submitted = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials",
        json={"cookie": cookie},
    )
    assert submitted.status_code == 200
    submitted_body = submitted.json()
    assert submitted_body["status"] == "connected"
    link_token = submitted_body["browser_link_token"]
    assert len(link_token) >= 32
    with session_scope() as db:
        link = HealthRepository(db).latest_browser_link("pair-user")
        assert link is not None
        assert link.token_digest == hashlib.sha256(link_token.encode()).hexdigest()
        assert link.token_digest != link_token

    status = client.get(
        f"/api/v1/connect/zepp/pair/{code}",
        headers={"X-User-Id": "pair-user"},
    ).json()
    assert status["status"] == "connected"
    assert "同步" in status["message"]

    reused = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials",
        json={"cookie": cookie},
    )
    assert reused.status_code == 409


def test_browser_link_renews_and_reports_disconnect(client):
    code = client.post(
        "/api/v1/connect/zepp/pair?sync_days=3",
        headers={"X-User-Id": "renew-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-renew","apptoken":"first-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}

    renewed = client.post(
        "/api/v1/connect/zepp/link/credentials",
        headers=auth,
        json={"cookie": '{"userid":"vendor-renew","apptoken":"second-token"}'},
    )
    assert renewed.status_code == 200
    assert renewed.json()["status"] == "connected"

    disconnected = client.post(
        "/api/v1/connect/zepp/link/disconnected",
        headers=auth,
        json={"reason": "browser session ended"},
    )
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "needs_login"

    status = client.get(
        "/api/v1/connect/zepp/token",
        headers={"X-User-Id": "renew-user"},
    ).json()
    assert status["authorized"] is True
    assert status["connection_status"] == "needs_login"
    assert status["needs_login"] is True
    assert status["connection_message"] == "browser session ended"


def test_browser_link_server_validation_recovers_false_disconnect(client):
    code = client.post(
        "/api/v1/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": "validate-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-validate","apptoken":"saved-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}
    client.post(
        "/api/v1/connect/zepp/link/disconnected",
        headers=auth,
        json={"reason": "cookie temporarily invisible"},
    )

    validated = client.post("/api/v1/connect/zepp/link/validate", headers=auth)
    assert validated.status_code == 200
    assert validated.json()["status"] == "connected"
    status = client.get(
        "/api/v1/connect/zepp/token", headers={"X-User-Id": "validate-user"}
    ).json()
    assert status["connection_status"] == "connected"


def test_browser_link_validation_network_failure_keeps_connection(client, monkeypatch):
    from vitalis.api.routes import zepp_pairing
    from vitalis.connectors.zepp import ZeppAuthError

    code = client.post(
        "/api/v1/connect/zepp/pair?sync_days=1",
        headers={"X-User-Id": "validate-network-user"},
    ).json()["pairing_code"]
    paired = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials",
        json={"cookie": '{"userid":"vendor-network","apptoken":"saved-token"}'},
    ).json()
    auth = {"Authorization": f"Bearer {paired['browser_link_token']}"}

    class UnavailableClient:
        def verify(self):
            raise ZeppAuthError("网络错误: temporary failure")

    class UnavailableConnector:
        def _client_for(self, *_args, **_kwargs):
            return UnavailableClient()

    monkeypatch.setattr(
        zepp_pairing, "get_connector", lambda _source: UnavailableConnector()
    )
    response = client.post("/api/v1/connect/zepp/link/validate", headers=auth)

    assert response.status_code == 503
    status = client.get(
        "/api/v1/connect/zepp/token",
        headers={"X-User-Id": "validate-network-user"},
    ).json()
    assert status["connection_status"] == "connected"
    assert status["needs_login"] is False


def test_browser_link_rejects_invalid_token(client):
    response = client.post(
        "/api/v1/connect/zepp/link/credentials",
        headers={"Authorization": f"Bearer {'x' * 48}"},
        json={"cookie": '{"userid":"vendor","apptoken":"token"}'},
    )
    assert response.status_code == 401
    assert "browser_link_token" not in response.text


def test_balance2_device_link_ingests_idempotent_callback_samples(client):
    created = client.post(
        "/api/v1/connect/zepp/device-link",
        headers={"X-User-Id": "balance2-device-user"},
    )
    assert created.status_code == 200
    token = created.json()["device_link_token"]
    assert len(token) >= 32
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    auth = {"Authorization": f"Bearer {token}"}
    batch = {"samples": [
        {"timestamp": now_ms - 2000, "heart_rate": 72},
        {"timestamp": now_ms - 1000, "heart_rate": 73},
    ]}

    first = client.post(
        "/api/v1/connect/zepp/device-link/heart-rate", headers=auth, json=batch
    )
    second = client.post(
        "/api/v1/connect/zepp/device-link/heart-rate", headers=auth, json=batch
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["accepted"] == 2
    with session_scope() as db:
        repo = HealthRepository(db)
        rows = repo.metric_samples(
            "balance2-device-user",
            "heart_rate",
            datetime.fromtimestamp((now_ms - 3000) / 1000, tz=timezone.utc),
            datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
        )
        link = repo.device_link(hashlib.sha256(token.encode()).hexdigest())
    assert len(rows) == 2
    assert {row.source for row in rows} == {"zepp_os"}
    assert {row.source_scope for row in rows} == {"device_callback"}
    assert {row.device_id for row in rows} == {"balance2_zepp_os"}
    assert link is not None and link.last_seen_at is not None


def test_balance2_device_upload_rejects_invalid_link(client):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    response = client.post(
        "/api/v1/connect/zepp/device-link/heart-rate",
        headers={"Authorization": f"Bearer {'x' * 48}"},
        json={"samples": [{"timestamp": now_ms, "heart_rate": 72}]},
    )
    assert response.status_code == 401


def test_non_auth_link_sync_failure_keeps_connection_valid(monkeypatch):
    from vitalis.api.routes import zepp_pairing

    digest = hashlib.sha256(b"sync-failure-link").hexdigest()
    with session_scope() as db:
        HealthRepository(db).create_browser_link(digest, "sync-failure-user")

    class FailingConnector:
        def sync_with_report(self, *_args, **_kwargs):
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(zepp_pairing, "get_connector", lambda _source: FailingConnector())
    zepp_pairing._linked_incremental_sync("sync-failure-user", digest)

    with session_scope() as db:
        link = HealthRepository(db).browser_link(digest)
        assert link is not None
        assert link.status == "connected"
        assert link.last_sync_at is None
        assert link.message == "登录状态有效，但数据同步失败，将稍后重试"


def test_browser_link_cannot_be_rebound_by_user_header(client):
    def pair(user_id: str) -> str:
        code = client.post(
            "/api/v1/connect/zepp/pair",
            headers={"X-User-Id": user_id},
        ).json()["pairing_code"]
        return client.post(
            f"/api/v1/connect/zepp/pair/{code}/credentials",
            json={"cookie": f'{{"userid":"{user_id}","apptoken":"token-{user_id}"}}'},
        ).json()["browser_link_token"]

    first_token = pair("link-owner")
    pair("other-user")
    response = client.post(
        "/api/v1/connect/zepp/link/disconnected",
        headers={
            "Authorization": f"Bearer {first_token}",
            "X-User-Id": "other-user",
        },
        json={"reason": "owner browser signed out"},
    )
    assert response.status_code == 200

    owner = client.get(
        "/api/v1/connect/zepp/token", headers={"X-User-Id": "link-owner"}
    ).json()
    other = client.get(
        "/api/v1/connect/zepp/token", headers={"X-User-Id": "other-user"}
    ).json()
    assert owner["connection_status"] == "needs_login"
    assert other["connection_status"] == "connected"


def test_cloud_pairing_raw_bookmarklet_flow(client):
    code = client.post(
        "/api/v1/connect/zepp/pair",
        headers={"X-User-Id": "bookmark-user"},
    ).json()["pairing_code"]
    cookie = '{"userid":"vendor-43","apptoken":"bookmark-token"}'
    submitted = client.post(
        f"/api/v1/connect/zepp/pair/{code}/credentials/raw",
        content=cookie,
        headers={"Content-Type": "text/plain"},
    )
    assert submitted.status_code == 200


def test_extension_zip_download(client):
    response = client.get("/api/v1/connect/zepp/extension.zip")
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
