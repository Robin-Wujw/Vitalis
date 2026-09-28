"""Zepp connection, pairing, and credential routes."""
from __future__ import annotations

import html as html_mod
from importlib import resources
import zipfile
from io import BytesIO
from pathlib import Path

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from vitalis.config import settings
from vitalis.bootstrap import (
    get_connector,
    get_connection_service,
    get_source_account_service,
)
from vitalis.application.connection import ConnectionOperationError
from vitalis.application.ports import CredentialProvider
from vitalis.entrypoints.api.deps import require_scope, require_user_id

router = APIRouter(prefix="/connect", tags=["connect"])


def _connector() -> CredentialProvider:
    return get_connector("zepp")  # type: ignore[return-value]


def _connection_status(error: ConnectionOperationError) -> int:
    if error.retry_after is not None or error.kind == "rate_limited":
        return 429
    if error.kind in {"identity_conflict", "conflict", "busy"}:
        return 409
    if error.kind == "not_found":
        return 404
    if error.kind == "expired":
        return 410
    if error.kind in {"network", "service"}:
        return 503
    if error.kind == "timeout":
        return 504
    return 400


@router.post("/zepp/authorize", summary="扫码授权：创建 state 并返回二维码 URL")
def zepp_authorize(user_id: str = Depends(require_scope("manage"))) -> dict:
    """生成扫码授权地址（返回给前端/Agent 渲染二维码）。

    用户用 Zepp App 扫这个二维码并确认授权后，
    Zepp 会回调 /connect/zepp/callback?code=...&state=...
    """
    return get_connection_service().authorize(user_id)


@router.get("/zepp/scan", response_class=HTMLResponse, summary="显示现有 Zepp 配对会话")
def zepp_scan_page(request: Request, code: str = Query(..., min_length=1)) -> str:
    """Render a previously created pairing code without creating state."""
    try:
        status = get_connection_service().pairing_status(code)
    except ConnectionOperationError as exc:
        raise HTTPException(
            status_code=404 if exc.kind in {"not_found", "expired"} else 400,
            detail="配对会话不存在或已过期",
        ) from exc
    pairing = {"pairing_code": code, "expires_at": status.expires_at.isoformat() + "Z"}
    return _cloud_pairing_html(status.user_id, _public_base_url(request), pairing)


@router.post("/zepp/scan", response_class=HTMLResponse, summary="创建模拟扫码页")
def zepp_mock_scan_page(user_id: str = Depends(require_scope("manage"))) -> str:
    """Start the explicit mock OAuth QR flow; real pairing uses POST /pair."""
    if not settings.zepp_mock:
        raise HTTPException(status_code=404, detail="真实 Zepp 配对请创建一次性配对码")
    try:
        authorization = get_connection_service().authorize(user_id)
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
    connector: ZeppConnector = Depends(_connector),
) -> Response:
    """Zepp 授权回调入口（redirect_uri）。

    流程：校验 state -> code 换 token -> 保存 -> 自动同步最近数据。
    浏览器直接访问时返回 HTML 成功页，API 调用返回 JSON。
    """
    try:
        service = get_connection_service(provider=connector)
        user_id, auth, attempt = service.complete_oauth(code, state)
    except ConnectionOperationError as exc:
        status_code = _connection_status(exc)
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=status_code, detail=str(exc), headers=headers) from exc
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

    支持两种输入方式：
      1. 粘贴完整的 cookie 值（推荐）：自动解析 userid/apptoken/region
      2. 分别填写 user_id + app_token（兼容旧方式）
    """

    cookie: str = Field(default="", description="完整的 hm-user-login-info cookie 值（URL 编码或纯 JSON）")
    user_id: str = Field(default="", description="Zepp 用户 id（cookie 中的 userid）")
    app_token: str = Field(default="", description="Zepp apptoken（cookie 中的 apptoken）")
    region_host: str = Field(default="", description="区域主机，如 api-mifitcn.zepp.com（缺省自动探测）")
    sync_history: bool = Field(default=True, description="导入后自动同步")
    sync_days: int = Field(default=14, ge=1, le=730)


@router.post("/zepp/token", summary="导入 Zepp 凭据（真实接入主入口）")
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
        return {"status": "error", "detail": "当前为 mock 模式（ZEPP_MOCK=true），无需导入；请设 ZEPP_MOCK=false 接真实 Zepp"}
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
        raise HTTPException(
            status_code=_connection_status(exc),
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None,
        ) from exc

    response = {
        "status": "connected",
        "source": "zepp",
        "user_id": vitalis_user,
        "vendor_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "auth_mode": "apptoken",
        "token_saved": True,
    }
    if attempt is not None:
        response["sync"] = {
            "attempt_id": attempt.id,
            "attempt_status": attempt.status,
            "progress": {"attempt_id": attempt.id, "status": attempt.status},
        }
    return response


@router.get("/zepp/import", response_class=HTMLResponse, summary="apptoken 导入引导页")
def zepp_import_page() -> str:
    """引导页：说明如何从浏览器 cookie 获取 user_id + apptoken 并提交。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Vitalis · 导入 Zepp 凭据</title>
<style>
*{{box-sizing:border-box;margin:0}}body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f2027;min-height:100vh;display:flex;align-items:center;justify-content:center;color:#e8f1f5}}
.card{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:34px 36px;max-width:520px;width:92%}}
h1{{font-size:20px;margin-bottom:14px}}ol{{padding-left:20px;color:#c8dce6;font-size:13px;line-height:1.9}}
label{{display:block;font-size:13px;color:#9fb8c4;margin:12px 0 4px}}input{{width:100%;padding:10px;border-radius:8px;border:1px solid #3a5563;background:#12232b;color:#e8f1f5;font-size:14px}}
button{{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;background:#4facfe;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
#msg{{margin-top:12px;font-size:13px;min-height:18px}}
</style></head>
<body><div class="card">
<h1>导入 Zepp 凭据（apptoken）</h1>
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
<label>同步天数（可选，1-730）</label><input id="days" type="number" value="14" min="1" max="730"/>
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
  const days=parseInt(document.getElementById('days').value)||14;
  const msg=document.getElementById('msg');
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
