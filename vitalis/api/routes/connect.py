"""POST /api/v1/connect/zepp —— 连接 Zepp 并同步数据。"""
from __future__ import annotations

import html as html_mod
import zipfile
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from vitalis.config import settings
from vitalis.connectors import get_connector
from vitalis.connectors.zepp import AuthRequired, ZeppAuthError, ZeppConnector
from vitalis.services import SyncService
from vitalis.storage import HealthRepository, session_scope
from vitalis.api.deps import require_user_id

router = APIRouter(prefix="/connect", tags=["connect"])


class ConnectRequest(BaseModel):
    """连接数据源请求。"""

    source: str = Field(default="zepp", description="数据源：zepp / garmin / ...")
    token: str = Field(default="", description="厂商 access token（走扫码时留空）")
    start: date | None = Field(default=None, description="历史起始日，缺省为最近 sync_days 天")
    end: date | None = Field(default=None)
    sync_history: bool = Field(default=True, description="同步历史数据并建档")
    sync_days: int = Field(default=14, ge=1, le=730, description="同步天数，最大 730 天（2 年）")


def _connector() -> ZeppConnector:
    return get_connector("zepp")  # type: ignore[return-value]


@router.get("/zepp/authorize", summary="扫码授权：返回二维码 URL")
def zepp_authorize(user_id: str = Depends(require_user_id)) -> dict:
    """生成扫码授权地址（返回给前端/Agent 渲染二维码）。

    用户用 Zepp App 扫这个二维码并确认授权后，
    Zepp 会回调 /connect/zepp/callback?code=...&state=...
    """
    connector = _connector()
    url, state = connector.authorize_url()
    with session_scope() as db:
        HealthRepository(db).save_oauth_state(state, user_id, connector.source)
    return {
        "status": "scan_required",
        "user_id": user_id,
        "authorize_url": url,
        "state": state,
        "hint": "用 Zepp App 扫描二维码授权；或在浏览器打开该地址登录授权",
    }


@router.get("/zepp/scan", response_class=HTMLResponse, summary="网页扫码页（渲染二维码）")
def zepp_scan_page(request: Request, user: str = Query(..., min_length=1)) -> str:
    """扫码网页：展示二维码 PNG，自动轮询授权结果，成功后自动同步。

    公网部署时浏览器访问：
      http://<公网IP或域名>:8000/api/v1/connect/zepp/scan?user=001
    二维码内容：真实模式 -> Zepp 授权页；mock 模式 -> 本服务模拟授权页（可扫通）。
    """
    connector = _connector()
    try:
        url, state = connector.authorize_url()
    except ZeppAuthError:
        # Real Zepp has no public OAuth flow. Pair an extension that reads the
        # official login cookie in the user's browser and hands it to the cloud.
        from .zepp_pairing import create_pairing

        pairing = create_pairing(user, sync_days=30)
        cloud_base = _public_base_url(request)
        return _cloud_pairing_html(user, cloud_base, pairing)
    with session_scope() as db:
        HealthRepository(db).save_oauth_state(state, user, connector.source)
    # mock 模式下二维码也指向本机可扫通的模拟授权页，保证真实扫码体验
    real_qr = not settings.zepp_mock
    return _scan_page_html(user, state, real_qr=real_qr)


@router.get("/zepp/mock-authorize", response_class=HTMLResponse, summary="模拟 Zepp 授权页（仅 mock 模式，手机扫码可达）", include_in_schema=False)
def zepp_mock_authorize(request: Request, state: str) -> str:
    """本地演示用的模拟授权确认页：手机扫 mock 二维码后打开此页。

    展示授权申请（scope），点「同意」即跳转 callback，完成 OAuth 流程。
    真实模式（ZEPP_MOCK=false）此页不可用，二维码指向真实 Zepp。
    """
    if not settings.zepp_mock:
        raise HTTPException(status_code=404, detail="真实模式请使用 Zepp 官方授权页")
    with session_scope() as db:
        if not HealthRepository(db).oauth_state_exists(state):
            raise HTTPException(status_code=404, detail="state 不存在或已过期，请重新扫码")
    base = str(request.url).split("/api/v1")[0]
    agree_url = f"{base}/api/v1/connect/zepp/callback?code=mock-scan-001&state={state}"
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
    <li>✓ 睡眠数据（时长 / 深睡 / REM / 评分）</li>
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
    <a class="download" href="/api/v1/connect/zepp/extension.zip">下载 Vitalis Zepp 登录扩展</a>
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
    const r=await fetch('/api/v1/connect/zepp/pair/{pairing_code}',{{headers:{{'X-User-Id':'{user_esc}'}}}});
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
    source_dir = Path(__file__).resolve().parents[3] / "browser_extension"
    if not source_dir.is_dir():
        raise HTTPException(status_code=404, detail="浏览器扩展未随部署发布")
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, Path("vitalis-zepp-login") / path.relative_to(source_dir))
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vitalis-zepp-login.zip"'},
    )


@router.get("/zepp/qrcode.png", summary="扫码二维码图片", include_in_schema=False)
def zepp_qrcode_png(request: Request, state: str) -> Response:
    """二维码 PNG：内容为该 state 对应的授权 URL（mock 时指向本服务模拟授权页）。"""
    with session_scope() as db:
        if not HealthRepository(db).oauth_state_exists(state):
            raise HTTPException(status_code=404, detail="state 不存在或已过期，请刷新扫码页")
    if settings.zepp_mock:
        base = str(request.url).split("/api/v1")[0]
        content = f"{base}/api/v1/connect/zepp/mock-authorize?state={state}"
    else:
        content = _connector().authorize_url_for(state)
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
  <div class="qr-wrap"><img class="qr" src="/api/v1/connect/zepp/qrcode.png?state={state}" alt="二维码"/></div>
  <div class="status warn" id="status">请用 Zepp App 扫描上方二维码授权…</div>
  <div class="meta" id="scanHint">二维码内容：{qr_desc}</div>
  <button id="mockBtn" style="display:none" onclick="mockAuth()">模拟扫码授权（本地演示）</button>
  <div class="meta">授权回调：{redirect_uri}</div>
</div>
<script>
const user = "{user_esc}";
const mock = {mock_flag};
if (mock) document.getElementById('mockBtn').style.display = 'inline-block';

async function pollToken() {{
  try {{
    const r = await fetch('/api/v1/connect/zepp/token', {{headers: {{'X-User-Id': user}}}});
    const d = await r.json();
    if (d.authorized) {{
      document.getElementById('status').className = 'status ok';
      document.getElementById('status').textContent = '✅ 授权成功，数据同步中…';
      return;  // 回调接口已自动同步数据
    }}
  }} catch (e) {{}}
  setTimeout(pollToken, 2000);
}}
async function mockAuth() {{
  document.getElementById('status').textContent = '模拟授权中…';
  await fetch('/api/v1/connect/zepp/callback?code=mock-scan-001&state={state}');
  pollToken();
}}
window.addEventListener('load', () => setTimeout(pollToken, 1500));
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
        with session_scope() as db:
            repo = HealthRepository(db)
            user_id = repo.consume_oauth_state(state)
            if user_id is None:
                raise ZeppAuthError("state 无效或已被使用，请重新发起扫码")
            auth = connector.exchange_and_save(repo, user_id, code)
        # 授权事务已提交（释放 SQLite 写锁），再执行数据同步
        sync = SyncService(connector).sync_user(user_id, start=date.today() - timedelta(days=7))
        if "text/html" in request.headers.get("accept", "*/*"):
            base = str(request.url).split("/api/v1")[0]
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
<div class="sub">已保存 Zepp 访问令牌，数据同步中，此页可关闭。</div>
<a href="{base}/api/v1/connect/zepp/scan?user={user_id}">← 返回扫码页查看结果</a>
</div></body></html>"""
            return HTMLResponse(body)
        return JSONResponse({
            "status": "authorized",
            "user_id": user_id,
            "source": connector.source,
            "token_saved": True,
            "source_user_id": auth.source_user_id,
            "sync": sync,
        })
    except ZeppAuthError as exc:
        if "text/html" in request.headers.get("accept", "*/*"):
            return HTMLResponse(
                f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'/></head>"
                f"<body style='font-family:sans-serif;text-align:center;padding-top:80px'>"
                f"<h2>授权失败</h2><p style='color:#666'>{exc}</p>"
                f"<a href='/api/v1/connect/zepp/scan'>返回扫码页重新扫码</a></body></html>"
            )
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/zepp/token", summary="查询 token 状态")
def zepp_token_status(user_id: str = Depends(require_user_id)) -> dict:
    with session_scope() as db:
        connector = _connector()
        repo = HealthRepository(db)
        auth = repo.get_token(user_id, connector.source)
        link = repo.latest_browser_link(user_id)
    if auth is None:
        return {
            "user_id": user_id,
            "authorized": False,
            "connection_status": "disconnected",
            "needs_login": True,
            "detail": "尚未连接 Zepp，请打开登录配对页",
        }
    return {
        "user_id": user_id,
        "authorized": True,
        "source_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "scope": auth.scope,
        "expires_at": auth.expires_at.isoformat() if auth.expires_at else None,
        "expired": auth.expired,
        "connection_status": link.status if link else ("expired" if auth.expired else "connected"),
        "needs_login": bool(link and link.status == "needs_login") or auth.expired,
        "connection_message": link.message if link else "凭据已保存",
        "last_verified_at": (
            link.last_verified_at.isoformat() + "Z" if link and link.last_verified_at else None
        ),
        "last_sync_at": link.last_sync_at.isoformat() + "Z" if link and link.last_sync_at else None,
    }


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


def _probe_region_hosts(user_id: str, app_token: str, region_hint: str | None, saved_host: str | None) -> str:
    """探测可用的 Zepp 区域主机（翻译自 ZeppBridge probe_region_hosts）。"""
    from vitalis.connectors.zepp.auth_parser import preferred_region_hosts
    from vitalis.connectors.zepp.client import ZeppAPIClient
    import concurrent.futures
    import threading

    hosts = preferred_region_hosts(saved_host, region_hint)
    if not hosts:
        hosts = ["https://api-mifitcn.zepp.com"]

    result: dict = {"host": None}
    lock = threading.Lock()

    def try_host(host: str) -> None:
        if result["host"]:
            return
        try:
            client = ZeppAPIClient(app_token=app_token, user_id=user_id, region_host=host)
            client.verify()
            with lock:
                if result["host"] is None:
                    result["host"] = host
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), 6)) as executor:
        futures = [executor.submit(try_host, h) for h in hosts]
        # 等第一个成功或全部超时（45秒）
        for future in concurrent.futures.as_completed(futures, timeout=45):
            if result["host"]:
                break

    if result["host"] is None:
        raise ZeppAuthError(
            f"未能在允许的区域主机上验证账号。尝试了 {len(hosts)} 个主机，"
            "请确认已登录 watchface.zepp.com 且凭据未过期。"
        )
    return result["host"]


@router.post("/zepp/token", summary="导入 Zepp 凭据（真实接入主入口）")
def import_zepp_token(req: ImportTokenRequest, vitalis_user: str = Depends(require_user_id)) -> dict:
    """导入并验证 Zepp 凭据，随后同步历史数据。

    凭据来源：浏览器登录 watchface.zepp.com 后，F12 -> Application -> Cookies
    -> 复制 `hm-user-login-info` 的值，粘贴到 cookie 字段即可。
    """
    connector = _connector()
    connector.authenticate()
    if connector.mock:
        return {"status": "error", "detail": "当前为 mock 模式（ZEPP_MOCK=true），无需导入；请设 ZEPP_MOCK=false 接真实 Zepp"}

    # 1. 解析凭据
    from vitalis.connectors.zepp.auth_parser import extract_from_login_info

    vendor_user_id = req.user_id.strip()
    app_token = req.app_token.strip()
    region_hint: str | None = None

    if req.cookie.strip():
        extracted = extract_from_login_info(req.cookie.strip())
        if extracted is None:
            raise HTTPException(status_code=400, detail="无法从 cookie 中解析出有效的 user_id 和 app_token，请确认粘贴的是 hm-user-login-info 的完整值")
        vendor_user_id = extracted.user_id
        app_token = extracted.app_token
        region_hint = extracted.region_hint

    if not vendor_user_id or not app_token:
        raise HTTPException(status_code=400, detail="缺少 user_id 和 app_token；请粘贴 cookie 值或分别填写")

    # 2. 探测区域主机
    try:
        region_host = _probe_region_hosts(
            vendor_user_id, app_token, region_hint,
            saved_host=req.region_host or None,
        )
    except ZeppAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 3. 保存凭据
    try:
        with session_scope() as db:
            repo = HealthRepository(db)
            auth = connector.import_token(
                repo, vitalis_user, app_token,
                vendor_user_id=vendor_user_id, region_host=region_host,
            )
    except ZeppAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    response = {
        "status": "connected",
        "source": "zepp",
        "user_id": vitalis_user,
        "vendor_user_id": auth.source_user_id,
        "region_host": auth.region_host,
        "auth_mode": "apptoken",
        "token_saved": True,
    }

    # 4. 同步
    if req.sync_history:
        try:
            from vitalis.models import User
            with session_scope() as db:
                repo = HealthRepository(db)
                report = connector.sync_with_report(
                    User(id=vitalis_user),
                    days=req.sync_days,
                    repo=repo,
                )
            response["sync"] = {
                "success": report.success,
                "streams": [
                    {
                        "stream": s.stream,
                        "status": s.status,
                        "records_written": s.records_written,
                        "message": s.message,
                    }
                    for s in report.streams
                ],
                "records_written": report.records_written,
                "message": report.message,
            }
            from vitalis.services import IntelligenceService
            response["profile"] = IntelligenceService().daily_profile(vitalis_user).model_dump(mode="json")
        except Exception as exc:
            response["sync_error"] = str(exc)
    return response


@router.get("/zepp/import", response_class=HTMLResponse, summary="apptoken 导入引导页")
def zepp_import_page() -> str:
    """引导页：说明如何从浏览器 cookie 获取 user_id + apptoken 并提交。"""
    return """<!DOCTYPE html>
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
<label>Zepp 用户 ID（userid）</label><input id="uid" placeholder="如 12345678"/>
<label>apptoken</label><input id="tok" placeholder="粘贴 hm-user-login-info 中的 apptoken" style="font-family:monospace"/>
<label>区域主机（可选）</label><input id="region" value="api-mifitcn.zepp.com"/>
<label>同步天数（可选，1-730）</label><input id="days" type="number" value="14" min="1" max="730"/>
<button onclick="doImport()">验证并同步</button>
<div id="msg"></div>
</div>
<script>
async function doImport(){{
  const uid=document.getElementById('uid').value.trim();
  const tok=document.getElementById('tok').value.trim();
  const region=document.getElementById('region').value.trim();
  const days=parseInt(document.getElementById('days').value)||14;
  const msg=document.getElementById('msg');
  if(!uid||!tok){{msg.textContent='请填写用户 ID 与 apptoken';msg.style.color='#f6c177';return;}}
  msg.textContent='验证与同步中…';msg.style.color='#9fb8c4';
  try{{
    const r=await fetch('/api/v1/connect/zepp/token',{{method:'POST',headers:{{'Content-Type':'application/json','X-User-Id':'001'}},
      body:JSON.stringify({{user_id:uid,app_token:tok,region_host:region,sync_history:true,sync_days:days}})}});
    const d=await r.json();
    if(!r.ok){{msg.textContent='失败：'+(d.detail||JSON.stringify(d));msg.style.color='#f87171';return;}}
    msg.textContent='✅ 已连接，同步 '+(d.sync?d.sync.days_synced:'?')+' 天，恢复分 '+(d.profile?d.profile.overall_score:'?');
    msg.style.color='#6ee7a0';
  }}catch(e){{msg.textContent='网络错误：'+e;msg.style.color='#f87171';}}
}}
</script></body></html>"""


@router.get("/zepp", include_in_schema=False)
def zepp_connect_browser(user: str = Query(..., min_length=1)) -> RedirectResponse:
    """Send browser navigation to the user-facing pairing flow."""
    query = urlencode({"user": user})
    return RedirectResponse(url=f"/api/v1/connect/zepp/scan?{query}")


@router.post("/zepp")
def connect_zepp(req: ConnectRequest, user_id: str = Depends(require_user_id)) -> dict:
    """连接 Zepp 并同步数据。

    有已保存 token（或 mock）：直接同步；
    未授权：返回扫码引导。
    """
    connector = _connector()
    connector.authenticate()

    response = {"status": "connected", "source": "zepp", "user_id": user_id}
    if connector.mock:
        response["auth_mode"] = "mock"

    if req.sync_history:
        try:
            with session_scope() as db:
                repo = HealthRepository(db)
                auth = connector.load_token(repo, user_id)
                if auth is None and not connector.mock:
                    return {
                        "status": "token_required",
                        "user_id": user_id,
                        "detail": "尚未导入 Zepp 凭据",
                        "import_page": f"/api/v1/connect/zepp/import",
                        "howto": "登录 watchface.zepp.com 后从 cookie hm-user-login-info 取 userid+apptoken，POST /api/v1/connect/zepp/token 导入",
                    }
                from vitalis.models import User
                if connector.mock:
                    dailies = connector.fetch(
                        User(id=user_id), start=req.start, end=req.end, repo=repo
                    )
                    for d in dailies:
                        repo.save_daily(d)
                    response["sync"] = {
                        "days_synced": len(dailies),
                        "start": str(req.start or date.today() - timedelta(days=req.sync_days - 1)),
                        "end": str(req.end or date.today()),
                    }
                else:
                    report = connector.sync_with_report(
                        User(id=user_id), days=req.sync_days, repo=repo
                    )
                    response["sync"] = {
                        "success": report.success,
                        "streams": [
                            {
                                "stream": s.stream,
                                "status": s.status,
                                "records_written": s.records_written,
                                "message": s.message,
                            }
                            for s in report.streams
                        ],
                        "records_written": report.records_written,
                        "message": report.message,
                    }
            # 查询汇总（session 已提交）
            from vitalis.services import IntelligenceService
            try:
                response["profile"] = IntelligenceService().daily_profile(user_id).model_dump(mode="json")
            except Exception as exc:
                response["profile"] = {"error": str(exc)}
        except AuthRequired as exc:
            return {"status": "scan_required", "user_id": user_id, "detail": str(exc)}
        except Exception as exc:
            response["sync_error"] = str(exc)

    return response


@router.post("/{source}")
def connect_generic(source: str, req: ConnectRequest, user_id: str = Depends(require_user_id)) -> dict:
    """插件化入口：任已注册数据源的通用连接。"""
    try:
        connector = get_connector(source)
    except KeyError as exc:
        return {"status": "error", "detail": str(exc)}
    connector.authenticate()
    if req.sync_history:
        response = SyncService(connector).sync_user(user_id, start=req.start, end=req.end)
        return {"status": "connected", "source": source, "user_id": user_id, "sync": response}
    return {"status": "connected", "source": source, "user_id": user_id}
