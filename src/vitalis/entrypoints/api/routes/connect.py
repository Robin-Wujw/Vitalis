"""Zepp connection, pairing, and credential routes."""
from __future__ import annotations

import html as html_mod
from importlib import resources
import ipaddress
import json
import secrets
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from vitalis.config import settings
from vitalis.bootstrap import (
    get_connector,
    get_connection_service,
    get_zepp_login_adapter,
)
from vitalis.application.connection import ConnectionOperationError
from vitalis.application.connection_progress import INITIAL_SYNC_DAYS, iso_utc
from vitalis.application.ports import CredentialProvider
from vitalis.entrypoints.api.deps import require_scope, require_user_id

router = APIRouter(prefix="/connect", tags=["connect"])
# Zepp's open platform routes to the official login for the account's region,
# where "forgot password" sets a password with an SMS code.
ZEPP_ACCOUNT_URL = "https://console.zepp.com/"


def _connector() -> CredentialProvider:
    return get_connector("zepp")  # type: ignore[return-value]


def _login_adapter():
    adapter = get_zepp_login_adapter()
    try:
        yield adapter
    finally:
        adapter.close()


class PasswordLoginRequest(BaseModel):
    """Password input accepted once by the login page."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    account: str = Field(min_length=1, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=256)
    country_code: str = Field(default="CN", min_length=2, max_length=24)


def _connection_status(error: ConnectionOperationError) -> int:
    return error.http_status


# Stable login classifications the login page may show; never vendor text.
LOGIN_FAILURE_CODES = frozenset({
    "captcha_required", "invalid_credentials", "invalid_password", "invalid_account",
    "invalid_country_code", "rate_limited", "network_error", "timeout",
})


def connection_http_error(error: ConnectionOperationError) -> HTTPException:
    headers = {"Retry-After": str(error.retry_after)} if error.retry_after else {}
    failure = getattr(error, "failure_code", None)
    if failure in LOGIN_FAILURE_CODES:
        headers["X-Login-Failure"] = failure
    return HTTPException(
        status_code=error.http_status, detail="连接操作失败", headers=headers or None,
    )


@router.post("/zepp/authorize", summary="扫码授权：创建 state 并返回二维码 URL")
def zepp_authorize(
    sync_days: int = Query(INITIAL_SYNC_DAYS, ge=1, le=730),
    user_id: str = Depends(require_scope("manage")),
) -> dict:
    """生成扫码授权地址（返回给前端/Agent 渲染二维码）。

    用户用 Zepp App 扫这个二维码并确认授权后，
    Zepp 会回调 /connect/zepp/callback?code=...&state=...
    """
    try:
        return get_connection_service().authorize(user_id, sync_days)
    except ConnectionOperationError as exc:
        raise connection_http_error(exc) from exc


@router.get("/zepp/scan", response_class=HTMLResponse, summary="显示现有 Zepp 配对会话")
def zepp_scan_page(request: Request, code: str = Query(..., min_length=1)) -> str:
    """Render a previously created pairing code without creating state."""
    try:
        status = get_connection_service().pairing_status(code)
    except ConnectionOperationError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail="配对会话不存在或已过期",
        ) from exc
    pairing = {"pairing_code": code, "expires_at": iso_utc(status.expires_at), "sync_days": status.sync_days}
    return _cloud_pairing_html(status.user_id, _public_base_url(request), pairing)


@router.post("/zepp/scan", response_class=HTMLResponse, summary="创建模拟扫码页")
def zepp_mock_scan_page(
    sync_days: int = Query(INITIAL_SYNC_DAYS, ge=1, le=730),
    user_id: str = Depends(require_scope("manage")),
) -> str:
    """Start the explicit mock OAuth QR flow; real pairing uses POST /pair."""
    if not settings.zepp_mock:
        raise HTTPException(status_code=404, detail="真实 Zepp 配对请创建一次性配对码")
    try:
        authorization = get_connection_service().authorize(user_id, sync_days)
    except ConnectionOperationError as exc:
        raise HTTPException(status_code=_connection_status(exc), detail="授权服务暂时不可用") from exc
    return _scan_page_html(user_id, authorization["state"], real_qr=False)


@router.get("/zepp/mock-authorize", response_class=HTMLResponse, summary="模拟 Zepp 授权页（仅 mock 模式，手机扫码可达）", include_in_schema=False)
def zepp_mock_authorize(request: Request, state: str) -> str:
    """本地演示用的模拟授权确认页：手机扫 mock 二维码后打开此页。

    展示授权申请（scope），点「同意」即跳转 callback，完成 OAuth 流程。
    真实模式（ZEPP_MOCK=false）此页不可用，二维码指向真实 Zepp。
    """
    if not settings.zepp_mock:
        raise HTTPException(status_code=404, detail="真实模式请使用 Zepp 官方授权页")
    if not get_connection_service().oauth_state_exists(state):
        raise HTTPException(status_code=404, detail="state 不存在或已过期，请重新扫码")
    base = str(request.url).split("/api")[0]
    agree_url = f"{base}/api/connect/zepp/callback?code=mock-scan-001&state={state}"
    return _mock_authorize_html(agree_url)


def _mock_authorize_html(agree_url: str) -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Zepp 授权（模拟）</title>
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #f4f7fa; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #fff; border-radius: 16px; box-shadow: 0 8px 30px rgba(0,0,0,.08);
          padding: 36px 32px; max-width: 380px; width: 92%; text-align: center; }}
  .logo {{ width: 56px; height: 56px; border-radius: 14px; background: linear-gradient(135deg,#00c6ff,#0072ff);
          color: #fff; font-size: 26px; font-weight: 800; display: flex; align-items: center; justify-content: center;
          margin: 0 auto 14px; }}
  h1 {{ font-size: 18px; margin-bottom: 4px; color: #222; }}
  .app {{ color: #0072ff; font-weight: 600; }}
  .desc {{ color: #888; font-size: 13px; margin: 8px 0 18px; }}
  ul {{ list-style: none; text-align: left; background: #f7f9fc; border-radius: 10px; padding: 12px 16px; margin-bottom: 20px; }}
  ul li {{ padding: 5px 0; color: #444; font-size: 14px; }}
  a.btn {{ display: block; padding: 12px; border-radius: 10px; background: #0072ff; color: #fff;
          text-decoration: none; font-size: 15px; font-weight: 600; }}
  .tip {{ margin-top: 12px; color: #c0a35a; font-size: 12px; }}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Z</div>
  <h1>Zepp 授权申请（<span class="app">模拟演示</span>）</h1>
  <div class="desc">Vitalis Health Agent 请求访问你的健康数据</div>
  <ul>
    <li>✓ 睡眠数据（时长 / 深睡 / 快速眼动睡眠 / 评分）</li>
    <li>✓ 日常活动（步数 / 活动时长 / 静息心率）</li>
    <li>✓ 训练记录（类型 / 时长 / 负荷）</li>
    <li>✓ 心率数据（HRV 趋势分析）</li>
  </ul>
  <a class="btn" href=""" + agree_url + """>同意并授权</a>
  <div class="tip">※ 本地 mock 演示页，仅用于体验扫码授权流程</div>
</div>
</body>
</html>"""


def _cloud_pairing_html(user: str, cloud_base: str, pairing: dict) -> str:
    """Real mode: official-page login completed by the browser extension."""
    user_esc = html_mod.escape(user)
    cloud_esc = html_mod.escape(cloud_base)
    pairing_code = html_mod.escape(pairing["pairing_code"])
    expires_at = html_mod.escape(pairing["expires_at"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vitalis · 连接 Zepp 云端</title>
<meta name="referrer" content="no-referrer"/>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f7f8; min-height: 100vh; color: #172026; }}
  main {{ width: min(680px, calc(100% - 32px)); margin: 0 auto; padding: 56px 0; }}
  header {{ border-bottom: 1px solid #d9e0e3; padding-bottom: 24px; }}
  .brand {{ color: #16745b; font-weight: 700; font-size: 14px; }}
  h1 {{ font-size: 28px; margin: 8px 0 8px; letter-spacing: 0; }}
  .lead {{ margin: 0; color: #5d6a70; line-height: 1.6; }}
  section {{ padding: 26px 0; border-bottom: 1px solid #d9e0e3; }}
  h2 {{ font-size: 16px; margin: 0 0 14px; }}
  ol {{ margin: 0; padding-left: 22px; color: #344248; line-height: 1.9; font-size: 14px; }}
  .pair {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: stretch; }}
  code {{ min-width: 0; overflow-wrap: anywhere; padding: 13px; background: #fff; border: 1px solid #cbd5d9;
          border-radius: 6px; color: #172026; font-size: 14px; }}
  button, .download {{ border: 0; border-radius: 6px; background: #16745b; color: #fff; padding: 0 16px;
                       font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }}
  .download {{ min-height: 42px; margin-bottom: 14px; }}
  .status {{ margin-top: 14px; min-height: 24px; color: #8a5a12; font-size: 14px; }}
  .status.ok {{ color: #16745b; }}
  .meta {{ color: #718087; font-size: 12px; margin-top: 10px; }}
  a {{ color: #12634e; }}
  @media (max-width: 520px) {{ main {{ padding: 28px 0; }} .pair {{ grid-template-columns: 1fr; }} button {{ min-height: 42px; }} }}
</style>
</head>
<body>
<main>
  <header><div class="brand">VITALIS CLOUD</div><h1>连接 Zepp 健康数据</h1>
    <p class="lead">在 Zepp 官方页面完成登录，Vitalis 云端随后自动同步。无需打开开发者工具，也无需手工复制 Cookie。</p></header>
  <section><h2>安装登录扩展</h2>
    <a class="download" href="/api/connect/zepp/extension.zip">下载 Vitalis Zepp 登录扩展</a>
    <ol><li>解压后在 Chrome/Edge 扩展管理页选择“加载已解压的扩展程序”。</li>
      <li>打开扩展，填入下方 Vitalis 地址和一次性配对码。</li>
      <li>点击“登录并自动连接”，在打开的 Zepp 官方页面完成手机号或账号登录。</li>
      <li>登录成功后无需返回操作；扩展会自动连接、续期并在断联时提示。</li></ol>
    <div class="meta">账号密码和验证码只提交给 Zepp 官方页面，Vitalis 不接收这些字段。</div>
  </section>
  <section><h2>本次配对</h2>
    <div class="meta">Vitalis 地址</div><div class="pair"><code id="base">{cloud_esc}</code><button onclick="copyText('base')">复制</button></div>
    <div class="meta">一次性配对码</div><div class="pair"><code id="code">{pairing_code}</code><button onclick="copyText('code')">复制</button></div>
    <div class="meta">用户 {user_esc} · {expires_at} 前有效</div><div id="status" class="status">等待浏览器扩展连接…</div>
  </section>
</main>
<script>
function copyText(id){{navigator.clipboard.writeText(document.getElementById(id).textContent)}}
async function poll(){{
  try {{
    const r=await fetch('/api/connect/zepp/pair/{pairing_code}');
    const d=await r.json(); const el=document.getElementById('status');
    el.textContent=d.message||'等待浏览器扩展连接…';
    if(d.status==='connected'){{el.className='status ok';return}}
    if(d.status==='expired'){{return}}
  }} catch(e) {{}}
  setTimeout(poll,2000);
}}
setTimeout(poll,1000);
</script>
</body>
</html>"""


def _public_base_url(request: Request) -> str:
    """Prefer the operator-configured HTTPS origin behind a reverse proxy."""
    if settings.public_url:
        return settings.public_url
    return str(request.base_url).rstrip("/")


def _private_origin(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return address.is_private and not address.is_loopback


def _require_login_origin(request: Request, *, submission: bool = True) -> None:
    """Apply the login page's transport and origin boundary.

    Opening the page is a top-level navigation (often from a link in another
    app), which carries no Origin and may carry a foreign Referer; only the
    password submission must come from the page itself.
    """
    expected = _public_base_url(request).rstrip("/")
    expected_parts = urlsplit(expected)
    production = settings.env in {"prod", "production"}
    # Plain HTTP is accepted only on a private address, i.e. inside an
    # encrypted overlay network such as ZeroTier; anything else needs HTTPS.
    scheme = "http" if _private_origin(expected_parts.hostname) else "https"
    if production and (
        not settings.public_url
        or expected_parts.scheme != scheme
        or request.headers.get("X-Forwarded-Proto", request.url.scheme).split(",", 1)[0].strip().lower() != scheme
    ):
        raise HTTPException(status_code=503, detail="登录页要求 HTTPS 或私有网络地址")
    if not submission:
        return

    origin = request.headers.get("Origin")
    if production and origin is None:
        raise HTTPException(status_code=403, detail="登录需要同源请求")
    if origin is not None and origin.rstrip("/") != expected:
        raise HTTPException(status_code=403, detail="不允许此登录来源")
    referrer = request.headers.get("Referer")
    if referrer:
        parts = urlsplit(referrer)
        ref_origin = f"{parts.scheme}://{parts.netloc}".rstrip("/")
        if ref_origin != expected:
            raise HTTPException(status_code=403, detail="登录来源页面无效")


def _login_response_headers(nonce: str | None = None) -> dict[str, str]:
    script_policy = f"'nonce-{nonce}'" if nonce else "'none'"
    style_policy = f"'nonce-{nonce}'" if nonce else "'none'"
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src {script_policy}; style-src {style_policy}; "
            "img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        ),
    }


def _login_page_html(code: str, expires_at: str, *, nonce: str) -> str:
    """Self-contained Zepp login page; no third-party assets or scripts."""
    code_json = json.dumps(code, ensure_ascii=True)
    expiry_json = json.dumps(expires_at, ensure_ascii=True)
    reset_url = ZEPP_ACCOUNT_URL
    reset_esc = html_mod.escape(reset_url, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="referrer" content="no-referrer"/>
<title>Vitalis · 连接 Zepp</title>
<style nonce="{nonce}">
:root {{ color-scheme: light; --ink: #16231d; --muted: #5d6e65; --line: #d7e2db; --brand: #136b54;
  --brand-dark: #0d3d31; --soft: #eef5f1; --warn: #8a5a12; --bad: #a33a2b;
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; background: #f3f6f4; color: var(--ink); display: flex; align-items: center; justify-content: center; padding: 32px 16px; }}
.shell {{ width: min(980px, 100%); display: grid; grid-template-columns: 1fr 1.05fr; background: #fff;
  border: 1px solid var(--line); border-radius: 14px; overflow: hidden; box-shadow: 0 18px 50px rgba(15,46,36,.08); }}
.shell > section {{ min-width: 0; }}
.intro {{ background: var(--brand-dark); color: #e8f2ed; padding: 44px 40px; display: flex; flex-direction: column; gap: 28px; }}
.brand {{ font-size: 13px; font-weight: 750; letter-spacing: .14em; color: #9fd3bd; }}
.intro h1 {{ margin: 0; font-size: 30px; line-height: 1.25; color: #fff; }}
.intro p {{ margin: 12px 0 0; color: #c3d8ce; line-height: 1.7; font-size: 15px; }}
.trust {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }}
.trust li {{ display: grid; grid-template-columns: 22px 1fr; gap: 10px; font-size: 14px; line-height: 1.55; color: #d6e6de; }}
.trust svg {{ margin-top: 2px; }}
.steps {{ list-style: none; margin: auto 0 0; padding: 0; display: grid; gap: 12px; }}
.steps li {{ display: grid; grid-template-columns: 26px 1fr; gap: 10px; align-items: center; font-size: 14px; color: #a9c4b8; }}
.dot {{ width: 26px; height: 26px; border-radius: 50%; border: 2px solid #4c7a68; display: grid; place-items: center; font-size: 12px; font-weight: 700; }}
.steps li.active {{ color: #fff; }} .steps li.active .dot {{ border-color: #9fd3bd; color: #9fd3bd; }}
.steps li.done {{ color: #d6e6de; }} .steps li.done .dot {{ background: #9fd3bd; border-color: #9fd3bd; color: var(--brand-dark); }}
.panel {{ padding: 44px 44px 36px; display: flex; flex-direction: column; }}
.panel h2 {{ margin: 0 0 6px; font-size: 22px; }}
.sub {{ margin: 0 0 22px; color: var(--muted); font-size: 14px; line-height: 1.6; }}
label {{ display: block; font-size: 14px; font-weight: 650; margin: 16px 0 7px; }}
.field {{ position: relative; }}
input, select {{ width: 100%; min-height: 46px; border: 1px solid #bfcfc5; border-radius: 8px; background: #fff; color: var(--ink);
  font: inherit; padding: 10px 12px; }}
input:focus, select:focus {{ outline: 3px solid #bfe2d3; border-color: var(--brand); }}
.toggle {{ position: absolute; right: 8px; top: 8px; min-height: 30px; padding: 0 10px; border: 0; border-radius: 6px;
  background: var(--soft); color: var(--brand); font: inherit; font-size: 13px; cursor: pointer; }}
button.primary {{ width: 100%; min-height: 48px; border: 0; border-radius: 8px; margin-top: 24px; background: var(--brand); color: #fff;
  font: inherit; font-size: 16px; font-weight: 700; cursor: pointer; }}
button.primary:hover {{ background: #0f5c48; }}
button.primary:disabled {{ cursor: wait; opacity: .6; }}
.status {{ margin-top: 18px; padding: 12px 14px; border-radius: 8px; background: var(--soft); font-size: 14px; line-height: 1.6; min-height: 46px; }}
.status.warn {{ background: #fbf3e4; color: var(--warn); }} .status.bad {{ background: #fbeceb; color: var(--bad); }}
.status.ok {{ background: #e3f3ec; color: var(--brand); }}
.meta {{ margin-top: auto; padding-top: 22px; color: #7b8a82; font-size: 12.5px; line-height: 1.7; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
a {{ color: var(--brand); }}
@media (max-width: 760px) {{ body {{ padding: 0; align-items: stretch; }} .shell {{ grid-template-columns: 1fr; border-radius: 0; border: 0; }} .intro, .panel {{ padding: 30px 24px; }} .steps {{ margin-top: 0; }} }}
</style>
</head>
<body>
<main class="shell">
<section class="intro">
<div class="brand">VITALIS</div>
<div><h1>连接你的 Zepp 账号</h1>
<p>连接后 Vitalis 每天从 Zepp 云端读取睡眠、心率、HRV 和训练记录，用来生成晨报和晚报。</p></div>
<ul class="trust">
<li><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#9fd3bd" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
<span>密码只用来向 Zepp 官方登录一次，不保存、不写日志、不返回；保存的是加密的登录令牌。</span></li>
<li><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#9fd3bd" stroke-width="2"><path d="M12 3 4 6v6c0 5 3.4 8.3 8 9 4.6-.7 8-4 8-9V6z"/></svg>
<span>本页只在你的 ZeroTier 私有网络里打开，传输由 ZeroTier 端到端加密。</span></li>
<li><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#9fd3bd" stroke-width="2"><path d="M3 12h4l3 8 4-16 3 8h4"/></svg>
<span>只读取健康数据，不会改动你在 Zepp 里的任何设置。</span></li>
</ul>
<ol class="steps" id="steps">
<li class="active"><span class="dot">1</span><span>登录 Zepp</span></li>
<li><span class="dot">2</span><span>加密保存登录令牌</span></li>
<li><span class="dot">3</span><span>回填近 180 天数据</span></li>
<li><span class="dot">4</span><span>之后每天自动同步</span></li>
</ol>
</section>
<section class="panel">
<h2>Zepp 账号登录</h2>
<p class="sub">用你在 Zepp App 里的手机号（或邮箱）和密码。</p>
<form id="login-form" autocomplete="on">
<label for="account">手机号或邮箱</label>
<input id="account" name="username" autocomplete="username" maxlength="320" placeholder="例如 13800000000" required/>
<label for="password">密码</label>
<div class="field"><input id="password" name="password" type="password" autocomplete="current-password" maxlength="256" required/>
<button type="button" class="toggle" id="toggle">显示</button></div>
<label for="country">账号注册地区</label>
<select id="country" name="country_code">
<option value="CN" selected>中国大陆</option><option value="HK">中国香港</option><option value="TW">中国台湾</option>
<option value="US">美国</option><option value="GB">英国</option><option value="DE">德国</option><option value="JP">日本</option>
</select>
<button class="primary" id="submit" type="submit">登录并连接</button>
</form>
<div id="status" class="status" role="status" aria-live="polite">填写后点“登录并连接”。</div>
<div class="meta"><span>没有密码或忘了？打开 <a href="{reset_esc}" target="_blank" rel="noopener noreferrer">Zepp 官方开放平台</a>，点登录后选“忘记密码”，用手机验证码设置密码，再回来登录。</span>
<span id="expiry"></span></div>
</section>
</main>
<script nonce="{nonce}">
(() => {{
  const code = {code_json};
  const expiresAt = new Date({expiry_json});
  const form = document.getElementById("login-form");
  const password = document.getElementById("password");
  const button = document.getElementById("submit");
  const status = document.getElementById("status");
  const expiry = document.getElementById("expiry");
  const loginPath = "/api/connect/zepp/pair/" + encodeURIComponent(code) + "/login";
  const statusPath = "/api/connect/zepp/pair/" + encodeURIComponent(code);
  const show = (text, kind = "") => {{ status.textContent = text; status.className = "status " + kind; }};
  const step = (index) => {{
    document.querySelectorAll("#steps li").forEach((item, i) => {{
      item.className = i < index ? "done" : i === index ? "active" : "";
    }});
  }};
  document.getElementById("toggle").addEventListener("click", (event) => {{
    const hidden = password.type === "password";
    password.type = hidden ? "text" : "password";
    event.target.textContent = hidden ? "隐藏" : "显示";
  }});
  let finished = false;
  const tick = () => {{
    const left = Math.round((expiresAt - new Date()) / 60000);
    if (finished || isNaN(left)) {{ expiry.textContent = ""; return; }}
    expiry.textContent = left > 0 ? "本页 " + left + " 分钟内有效" : "本页已过期，请重新生成链接";
    if (left <= 0) button.disabled = true;
  }};
  tick(); window.setInterval(tick, 30000);
  async function poll() {{
    try {{
      const response = await fetch(statusPath, {{ cache: "no-store", headers: {{ "Accept": "application/json" }} }});
      if (response.ok) {{
        const data = await response.json();
        const attempt = data.attempt_status;
        if (data.status === "connected") {{
          if (attempt === "succeeded" || attempt === "partial") {{
            step(4); show("全部完成：历史数据已回填，之后每天自动同步，晨报和晚报照常推送。可以关闭本页。", "ok"); return;
          }}
          if (attempt === "failed" || attempt === "needs_reauth" || attempt === "cancelled") {{
            show("已连接，但首次回填没有完成；worker 会按计划重试，不需要重新登录。", "warn"); return;
          }}
          step(2); show("已连接，正在回填近 180 天数据，通常几分钟到几十分钟；可以关闭本页，回填会继续。", "ok");
        }}
        if (data.status === "expired") {{ show("本页已过期；登录已完成的话，回填仍在后台继续。", "warn"); return; }}
      }}
    }} catch (_) {{}}
    window.setTimeout(poll, 5000);
  }}
  form.addEventListener("submit", async (event) => {{
    event.preventDefault();
    button.disabled = true;
    show("正在向 Zepp 官方登录…");
    let response;
    try {{
      response = await fetch(loginPath, {{
        method: "POST", cache: "no-store",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify({{
          account: document.getElementById("account").value.trim(),
          password: password.value,
          country_code: document.getElementById("country").value
        }})
      }});
    }} catch (_) {{
      password.value = ""; button.disabled = false;
      show("连不上 Vitalis 服务，确认电脑已连上 ZeroTier 后重试。", "bad"); return;
    }}
    password.value = "";
    if (response.ok) {{
      finished = true; tick(); form.querySelectorAll("input,select,button").forEach((item) => item.disabled = true);
      step(1); show("登录成功，令牌已加密保存。", "ok"); window.setTimeout(poll, 1000); return;
    }}
    const failure = response.headers.get("X-Login-Failure") || "";
    button.disabled = false;
    if (response.status === 429 || failure === "rate_limited") show("尝试次数过多，请过几分钟再试。", "warn");
    else if (failure === "captcha_required") show("Zepp 要求图形验证，网页登录过不去。请先在 Zepp 官方开放平台（console.zepp.com）的登录页用“忘记密码”重设，再回来登录。", "warn");
    else if (response.status === 409) show("这个 Zepp 账号已连接到另一个 Vitalis 用户。", "bad");
    else if (response.status === 404 || response.status === 410) show("本页已过期或已经用过，请重新生成链接。", "warn");
    else if (failure === "invalid_country_code") show("注册地区不对，换一个地区再试。", "bad");
    else if (response.status === 401 || response.status === 403 || failure.startsWith("invalid_")) show("账号或密码不对，或者注册地区选错了。", "bad");
    else if (response.status >= 502) show("Zepp 登录服务暂时连不上，请稍后重试。", "warn");
    else show("登录没有完成，请稍后重试。", "bad");
  }});
}})();
</script>
</body>
</html>"""


@router.get("/zepp/login", response_class=HTMLResponse, summary="Zepp 账号密码登录页")
def zepp_login_page(
    request: Request,
    code: str = Query(..., min_length=1, max_length=128),
) -> Response:
    _require_login_origin(request, submission=False)
    try:
        status = get_connection_service().pairing_status(code)
    except ConnectionOperationError as exc:
        raise HTTPException(status_code=exc.http_status, detail="配对会话不存在或已过期") from exc
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(
        _login_page_html(code, iso_utc(status.expires_at) or "", nonce=nonce),
        headers=_login_response_headers(nonce),
    )


@router.get("/zepp/extension.zip", summary="下载 Vitalis Zepp 登录桥", include_in_schema=False)
def zepp_extension_zip() -> Response:
    source_dir = resources.files("vitalis").joinpath("static", "browser_extension")
    if not source_dir.is_dir():
        # Editable installs reference the repository's one asset source; wheels
        # carry the same directory through Hatch's force-include configuration.
        source_dir = Path(__file__).resolve().parents[5] / "clients" / "browser_extension"
    if not source_dir.is_dir():
        raise HTTPException(status_code=404, detail="浏览器扩展未随部署发布")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.iterdir(), key=lambda item: item.name):
            if path.is_file():
                archive.writestr(f"vitalis-zepp-login/{path.name}", path.read_bytes())
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vitalis-zepp-login.zip"'},
    )


@router.get("/zepp/qrcode.png", summary="扫码二维码图片", include_in_schema=False)
def zepp_qrcode_png(request: Request, state: str) -> Response:
    """二维码 PNG：内容为该 state 对应的授权 URL（mock 时指向本服务模拟授权页）。"""
    service = get_connection_service()
    if not service.oauth_state_exists(state):
        raise HTTPException(status_code=404, detail="state 不存在或已过期，请刷新扫码页")
    if settings.zepp_mock:
        base = str(request.url).split("/api")[0]
        content = f"{base}/api/connect/zepp/mock-authorize?state={state}"
    else:
        content = service.authorize_url_for(state)
    return Response(content=_qr_png(content), media_type="image/png")


def _qr_png(content: str) -> bytes:
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scan_page_html(user: str, state: str, real_qr: bool = False) -> str:
    """扫码页模板。"""
    redirect_uri = html_mod.escape(settings.zepp_redirect_uri)
    user_esc = html_mod.escape(user)
    mock_flag = "true" if settings.zepp_mock else "false"
    qr_desc = "Zepp 官方授权页" if real_qr else "模拟授权页（演示环境，可扫码打通）"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vitalis · 连接 Zepp</title>
<style>
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: linear-gradient(160deg, #0f2027, #203a43, #2c5364); min-height: 100vh;
         display: flex; align-items: center; justify-content: center; color: #e8f1f5; }}
  .card {{ background: rgba(255,255,255,.06); backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,.12);
          border-radius: 18px; padding: 40px 44px; max-width: 420px; width: 92%; text-align: center; }}
  h1 {{ font-size: 22px; margin-bottom: 6px; }}
  .sub {{ color: #9fb8c4; font-size: 13px; margin-bottom: 26px; }}
  .qr-wrap {{ background: #fff; border-radius: 14px; padding: 14px; display: inline-block; margin-bottom: 18px; }}
  img.qr {{ width: 230px; height: 230px; display: block; }}
  .status {{ font-size: 15px; min-height: 24px; margin-top: 8px; }}
  .ok {{ color: #6ee7a0; }}
  .warn {{ color: #f6c177; }}
  button {{ margin-top: 14px; padding: 10px 18px; border: 0; border-radius: 8px; cursor: pointer;
           background: #4facfe; color: #fff; font-size: 14px; }}
  button:hover {{ opacity: .9; }}
  .meta {{ margin-top: 20px; color: #7d97a4; font-size: 12px; word-break: break-all; }}
</style>
</head>
<body>
<div class="card">
  <h1>连接 Zepp 健康数据</h1>
  <div class="sub">用户 {user_esc} · Vitalis Health Agent</div>
  <div class="qr-wrap"><img class="qr" src="/api/connect/zepp/qrcode.png?state={state}" alt="二维码"/></div>
  <div class="status warn" id="status">请用 Zepp App 扫描上方二维码授权…</div>
  <div class="meta" id="scanHint">二维码内容：{qr_desc}</div>
  <button id="mockBtn" style="display:none" onclick="mockAuth()">模拟扫码授权（本地演示）</button>
  <div class="meta">授权回调：{redirect_uri}</div>
</div>
<script>
const mock = {mock_flag};
if (mock) document.getElementById('mockBtn').style.display = 'inline-block';

async function mockAuth() {{
  const status = document.getElementById('status');
  status.textContent = '模拟授权中…';
  try {{
    const response = await fetch('/api/connect/zepp/callback?code=mock-scan-001&state={state}');
    if (!response.ok) throw new Error('授权失败');
    status.className = 'status ok';
    status.textContent = '授权成功，数据同步中…';
  }} catch (e) {{
    status.className = 'status warn';
    status.textContent = '授权失败，请刷新扫码页重试';
  }}
}}
</script>
</body>
</html>"""


@router.get("/zepp/callback", summary="Zepp 扫码回调：收 code，存 token，同步数据")
def zepp_callback(
    request: Request,
    code: str,
    state: str,
    connector: CredentialProvider = Depends(_connector),
) -> Response:
    """Zepp 授权回调入口（redirect_uri）。

    流程：校验 state -> code 换 token -> 保存 -> 自动同步最近数据。
    浏览器直接访问时返回 HTML 成功页，API 调用返回 JSON。
    """
    try:
        service = get_connection_service(provider=connector)
        user_id, auth, attempt = service.complete_oauth(code, state)
    except ConnectionOperationError as exc:
        raise connection_http_error(exc) from exc
    if "text/html" in request.headers.get("accept", "*/*"):
        body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>授权成功</title><style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#f4f7fa;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:16px;padding:40px 36px;text-align:center;width:92%;max-width:360px;box-shadow:0 8px 30px rgba(0,0,0,.08)}}
.em{{font-size:52px}}.ok{{color:#16a34a;font-size:18px;font-weight:700;margin:10px 0 6px}}
.sub{{color:#888;font-size:13px}}
a{{display:inline-block;margin-top:18px;color:#0072ff;text-decoration:none;font-size:14px}}
</style></head>
<body><div class="card">
<div class="em">✅</div><div class="ok">授权成功</div>
<div class="sub">已保存 Zepp 访问令牌，同步任务已入队；请在 Vitalis 客户端查询任务状态。此页可关闭。</div>
</div></body></html>"""
        return HTMLResponse(body)
    return JSONResponse({
        "status": "authorized",
        "connection_state": "backfill_requested" if attempt else "credential_verified",
        "progress_url": "/api/connect/zepp/progress",
        "user_id": user_id,
        "source": connector.source,
        "token_saved": True,
        "source_user_id": auth.source_user_id,
        "sync": {
            "attempt_id": attempt.id,
            "attempt_status": attempt.status,
        } if attempt is not None else None,
    })


@router.get("/zepp/token", summary="查询 token 状态")
def zepp_token_status(user_id: str = Depends(require_user_id)) -> dict:
    return get_connection_service().token_status(user_id).as_dict()


class ImportTokenRequest(BaseModel):
    """导入 Zepp apptoken 凭据（来自网页登录 cookie hm-user-login-info）。

    这是高级手工入口；常规连接先使用浏览器扩展配对。
    可粘贴完整 cookie 或分别填写 user_id + app_token。
    """

    cookie: str = Field(default="", description="完整的 hm-user-login-info cookie 值（URL 编码或纯 JSON）")
    user_id: str = Field(default="", description="Zepp 用户 id（cookie 中的 userid）")
    app_token: str = Field(default="", description="Zepp apptoken（cookie 中的 apptoken）")
    region_host: str = Field(default="", description="区域主机，如 api-mifitcn.zepp.com（缺省自动探测）")
    sync_history: bool = Field(default=True, description="导入后自动同步")
    sync_days: int = Field(default=INITIAL_SYNC_DAYS, ge=1, le=730)


@router.post("/zepp/token", summary="高级入口：手工导入 Zepp 凭据")
def import_zepp_token(
    req: ImportTokenRequest,
    vitalis_user: str = Depends(require_scope("manage")),
) -> dict:
    """导入并验证 Zepp 凭据，随后同步历史数据。

    凭据来源：浏览器登录 watchface.zepp.com 后，F12 -> Application -> Cookies
    -> 复制 `hm-user-login-info` 的值，粘贴到 cookie 字段即可。
    """
    connector = _connector()
    if getattr(connector, "mock", False):
        raise HTTPException(status_code=409, detail="模拟模式不接受手工厂商凭据")
    try:
        auth, attempt = get_connection_service(provider=connector).import_token(
            vitalis_user,
            cookie=req.cookie,
            vendor_user_id=req.user_id,
            app_token=req.app_token,
            saved_host=req.region_host or None,
            sync_history=req.sync_history,
            sync_days=req.sync_days,
        )
    except ConnectionOperationError as exc:
        raise connection_http_error(exc) from exc

    response = {
        "status": "connected",
        "source": "zepp",
        "user_id": vitalis_user,
        "vendor_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "auth_mode": "apptoken",
        "entrypoint": "advanced_token_import",
        "connection_state": "backfill_requested" if attempt else "credential_verified",
        "sync_days": req.sync_days,
        "progress_url": "/api/connect/zepp/progress",
        "token_saved": True,
    }
    if attempt is not None:
        response["sync"] = {
            "attempt_id": attempt.id,
            "attempt_status": attempt.status,
            "progress": {"attempt_id": attempt.id, "status": attempt.status},
        }
    return response


@router.get("/zepp/progress", summary="读取首连回填、暖机和首报进度")
def zepp_connection_progress(user_id: str = Depends(require_user_id)) -> dict:
    """Read stored progress only; synchronization and analysis belong to worker."""
    return get_connection_service().progress(user_id)


@router.get("/zepp/import", response_class=HTMLResponse, summary="高级入口：apptoken 导入引导页")
def zepp_import_page() -> str:
    """引导页：说明如何从浏览器 cookie 获取 user_id + apptoken 并提交。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vitalis · 高级手工导入 Zepp 凭据</title>
<style>
*{{box-sizing:border-box;margin:0}}body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f2027;min-height:100vh;display:flex;align-items:center;justify-content:center;color:#e8f1f5}}
.card{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:34px 36px;max-width:520px;width:92%}}
h1{{font-size:20px;margin-bottom:14px}}ol{{padding-left:20px;color:#c8dce6;font-size:13px;line-height:1.9}}
label{{display:block;font-size:13px;color:#9fb8c4;margin:12px 0 4px}}input{{width:100%;padding:10px;border-radius:8px;border:1px solid #3a5563;background:#12232b;color:#e8f1f5;font-size:14px}}
button{{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;background:#4facfe;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
#msg{{margin-top:12px;font-size:13px;min-height:18px}}
</style></head>
<body><div class="card">
<h1>高级入口：手工导入 Zepp 凭据</h1>
<p>常规连接请使用浏览器扩展配对；此入口适用于了解凭据导入操作的用户。</p>
<ol>
<li>在电脑浏览器打开 <b>watchface.zepp.com</b>（或备用 user.huami.com）并登录你的 Zepp 账号</li>
<li>按 F12 打开开发者工具 → Application → Cookies → 找到 <b>hm-user-login-info</b></li>
<li>复制它的值（JSON），其中 <b>userid</b> 填入下方用户 ID，<b>apptoken</b> 填入下方令牌</li>
<li>区域主机缺省中国区 api-mifitcn.zepp.com（非中国区账号按你的区域填）</li>
</ol>
<label>Vitalis 用户 ID</label><input id="localUser" autocomplete="username"/>
<label>Vitalis 访问令牌</label><input id="apiToken" type="password" autocomplete="off"/>
<label>Zepp 用户 ID（userid）</label><input id="uid" placeholder="如 12345678"/>
<label>apptoken</label><input id="tok" placeholder="粘贴 hm-user-login-info 中的 apptoken" style="font-family:monospace"/>
<label>区域主机（可选）</label><input id="region" value="api-mifitcn.zepp.com"/>
<label>首次回填天数（1-730，默认 180）</label><input id="days" type="number" value="180" min="1" max="730"/>
<button onclick="doImport()">验证并同步</button>
<div id="msg"></div>
</div>
<script>
async function doImport(){{
  const localUser=document.getElementById('localUser').value.trim();
  const apiToken=document.getElementById('apiToken').value.trim();
  const uid=document.getElementById('uid').value.trim();
  const tok=document.getElementById('tok').value.trim();
  const region=document.getElementById('region').value.trim();
  const days=Number(document.getElementById('days').value);
  const msg=document.getElementById('msg');
  if(!Number.isInteger(days)||days<1||days>730){{msg.textContent='回填窗口需要 1-730 天';return;}}
  if(!localUser||!apiToken||!uid||!tok){{msg.textContent='请填写 Vitalis 令牌和 Zepp 凭据';msg.style.color='#f6c177';return;}}
  msg.textContent='正在验证登录凭据…';msg.style.color='#9fb8c4';
  try{{
    const r=await fetch('/api/connect/zepp/token',{{method:'POST',headers:{{'Content-Type':'application/json','X-User-Id':localUser,'Authorization':'Bearer '+apiToken}},
      body:JSON.stringify({{user_id:uid,app_token:tok,region_host:region,sync_history:true,sync_days:days}})}});
    const d=await r.json();
    if(!r.ok){{msg.textContent='失败：'+(d.message||'请稍后重试');msg.style.color='#f87171';return;}}
    msg.textContent=d.sync?.attempt_id ? '凭据已保存，同步任务已入队' : '凭据已保存，但同步任务未创建';
    msg.style.color='#6ee7a0';
  }}catch(e){{msg.textContent='网络错误：'+e;msg.style.color='#f87171';}}
}}
</script></body></html>"""
