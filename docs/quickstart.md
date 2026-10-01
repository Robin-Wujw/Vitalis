# 快速开始

[文档导航](README.md) | [运行与排障](operations.md)

## 合成数据：第一次分析

声明的 Python 范围为 3.11-3.13；当前自动化基线在 3.13 上验证。从仓库根目录使用唯一锁文件安装（PowerShell 激活命令为 `.venv\Scripts\Activate.ps1`）：

```bash
python -m pip install 'uv==0.12.9'
uv sync --locked
source .venv/bin/activate
vitalis demo --database demo.db --day 2026-09-26
```

`demo` 仅在**尚不存在**的 SQLite `.db`/`.sqlite` 文件中写入模拟 Zepp 数据、用户 `demo` 和该日的分析快照；输出包含 `analysis_run_id`，但不是完整报告。已有数据库不会被覆盖。`python -m vitalis` 与 `vitalis` 使用同一 CLI。报告正文按[报告阅读与渠道](reports.md)的周期和只读渠道规则生成；仓库中的[四类报告示例](reports.md#示例与验证边界)是合成设计样例，不含真实健康记录。

在同一个终端为后续命令指定刚生成的库，检查 schema，然后启动仅绑定本机的 API：

```bash
export DATABASE_URL=sqlite:///./demo.db
vitalis doctor
vitalis serve
```

Windows PowerShell 用 `$env:DATABASE_URL = 'sqlite:///./demo.db'` 代替 `export`。`doctor` 校验 schema，并将尚未启动过的 worker 显示为 `not_seen`；有心跳时报告近期活动，但不证明数据已经更新。服务提供 `/docs`（OpenAPI）和当前用户接口 `/api`。在另一个终端发请求时，同样指定 `DATABASE_URL`；服务和令牌签发必须指向**同一个**库。可信本地操作员可按[运维的令牌步骤](operations.md)为已有用户 `demo` 签发短期 `read` 令牌并私下设置 `VITALIS_ACCESS_TOKEN`，然后读取第一份快照：

```bash
curl 'http://127.0.0.1:8000/api/reports/daily?day=2026-09-26' \
  -H "Authorization: Bearer $VITALIS_ACCESS_TOKEN"
```

`GET` 只读已有快照；若所选日期没有分析，返回 `404`，不会自动同步或推断。`X-User-Id` 可省略；填写时必须等于令牌所属用户，不能以它代替令牌。完整路径、参数及响应字段以服务的 `/docs` 为准。

## 连接真实 Zepp

演示以外，先在**新的、独立的**数据库设置 `ZEPP_MOCK=false` 和私有的 `VITALIS_TOKEN_ENCRYPTION_KEY`（有效 Fernet 密钥；不要加入版本库，否则真实连接在启动时拒绝），执行 `vitalis db init`、`vitalis user create --id <local-user-id>`，再通过 `vitalis token issue --user <local-user-id> --scope manage --output <new-private-file>` 签发专用于管理/配对的 Bearer 令牌；这些命令必须指向同一数据库，令牌文件存于仓库外，不能当作公开链接。`POST /api/connect/zepp/pair` 可创建绑定该用户的一次性配对会话，再用返回的 `scan_url` 打开页面并按[浏览器扩展](../clients/browser_extension/README.md)的步骤登录 Zepp 官方页面。扩展 Origin 须按实际扩展 ID 加入 `VITALIS_PAIRING_ALLOWED_ORIGINS` 并重启 API；真实浏览器配对需受浏览器信任的 HTTPS 源以及与鉴权兼容的网关，不可直接公开完整 API。在另一个终端以相同私有环境运行 `vitalis worker`；配对仅创建持久同步任务，API 不会在 HTTP 请求中执行厂商同步。手动同步统一使用带 `Idempotency-Key` 的 `POST /api/sync-jobs`（`days` 为 1..730，或提交明确的 `from`/`to` 本地日期窗口）；取得 `job_id` 后用有 `read` 权限的令牌查询 `GET /api/jobs/{job_id}` 和 `GET /api/data-status`，需要停止时使用有 `sync` 权限的 `POST /api/jobs/{job_id}/cancel`，不能把入队误认为数据已更新。账号密码和验证码只进入 Zepp 官方页面，不提供给 Vitalis。区域、身份绑定和覆盖限制见 [Zepp 指南](zepp.md)。

完整运行参数、备份和同步排查见[运维](operations.md)；Hermes 接入前请阅读[智能体集成](agents.md)中的权限与手动验收边界。PushPlus 只读推送不需要 Hermes 或用户回复；真实 Hermes 安装、发现和调用仍需单独的授权 smoke test。
