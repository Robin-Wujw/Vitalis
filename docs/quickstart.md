# 快速开始

[文档中心](README.md) | [报告与渠道](reports.md) | [运维](operations.md)

本页带你在新目录生成一份合成报告、读取已保存快照，并说明真实 Zepp 连接的边界。合成路径不会发送 PushPlus，也不需要真实账号。

## 生成本地报告

前提：Python 3.11–3.13；命令从仓库根目录执行；使用新建或可丢弃的 SQLite 路径。先安装锁定依赖：

```powershell
python -m pip install 'uv==0.12.9'
uv sync --locked --extra dev
```

在 PowerShell 中显式启用 mock 和测试环境，创建一个新库：

```powershell
$env:ZEPP_MOCK = 'true'
$env:VITALIS_ENV = 'test'
$env:DATABASE_URL = 'sqlite:///./demo.db'
uv run --locked --extra dev vitalis demo --database .\demo.db --day 2026-10-07
```

预期输出是 JSON 元信息，包含 `dataset=synthetic_demo`、用户 `demo`、导入天数和 `analysis_run_id`；它不是可读报告。目标数据库必须不存在，已有文件不会覆盖。

从已保存分析导出一份新 Markdown 文件：

```powershell
uv run --locked --extra dev vitalis report daily --user demo --day 2026-10-07 --format markdown --output .\daily.md
Get-Content .\daily.md
```

`report` 只读取持久化分析，不同步、不重新计算、不发送通知，并拒绝覆盖已有输出文件。`morning`、`evening`、`weekly`、`monthly` 可替换 `daily`；`daily` 导出完整本地日事实快照，`evening` 导出面向阅读的当日复盘，二者不是同一语义。`--format html` 写保守的 inline HTML 片段。成功结果是指定路径存在且内容可直接阅读。

## 读取 API 快照

前提：服务和令牌签发必须使用同一个数据库；本地 API 默认是 `127.0.0.1:8000`。在保持 `DATABASE_URL` 的终端启动服务：

```powershell
uv run --locked --extra dev vitalis serve
```

另开终端，为已有的 `demo` 用户签发只读令牌。令牌文件应放在仓库外的新路径，命令输出不会打印令牌：

```powershell
uv run --locked --extra dev vitalis token issue --user demo --scope read --output C:\Users\Public\vitalis-demo-read.token
$env:VITALIS_ACCESS_TOKEN = Get-Content C:\Users\Public\vitalis-demo-read.token -Raw
curl.exe "http://127.0.0.1:8000/api/reports/daily?day=2026-10-07" -H "Authorization: Bearer $env:VITALIS_ACCESS_TOKEN"
```

预期响应是 JSON 报告快照。`GET /api/reports/{kind}` 只读已有结果；没有指定日期快照时返回 404，不会自动分析。`X-User-Id` 不能代替 Bearer 令牌，若同时提供必须与令牌绑定用户一致。完整请求字段以运行服务的 `/openapi.json` 为准。

## 真实 Zepp 连接

不要把演示库改成真实库。为新的独立数据库设置 `ZEPP_MOCK=false`、有效的 `VITALIS_TOKEN_ENCRYPTION_KEY` 和必要的 Zepp 配置，运行 `vitalis db init`、`vitalis user create --id <用户>`，再用 `manage` scope 令牌调用 `POST /api/connect/zepp/pair`。打开返回的 `scan_url`，只在官方 Zepp 页面登录；密码和验证码不输入 Vitalis。浏览器端步骤见[扩展指南](../clients/browser_extension/README.md)。

真实同步由独立 `vitalis worker` 处理。手动 `POST /api/sync-jobs` 必须带新的 `Idempotency-Key`，得到 `job_id` 后读取 `GET /api/jobs/{job_id}`；受理不等于同步完成。服务默认只绑定回环地址，公网部署还需要 TLS、鉴权、来源白名单和网关配置。

## 常见失败

| 现象 | 处理 |
| --- | --- |
| `demo` 拒绝路径 | 删除或改用一个全新的 `.db`/`.sqlite` 路径；不要覆盖用户库。 |
| `report` 拒绝输出 | 改用新文件名；命令不会覆盖已有文件。 |
| `404` 报告快照 | 确认日期和数据库一致；显式创建分析任务后等待 worker 成功。 |
| `401`/`403` | 检查令牌文件、用户绑定和 scope；不要用 `X-User-Id` 冒充认证。 |
| `doctor` 显示 `not_seen` | API schema 可能正常，但 worker 尚未启动；启动独立 worker 后再诊断。 |
| 真实连接拒绝密钥 | `ZEPP_MOCK=false` 时配置有效 Fernet 密钥，并与数据库分开保存。 |

报告事实、缺失和渠道状态见[报告与渠道](reports.md)；配置、备份和排障见[运维](operations.md)。
