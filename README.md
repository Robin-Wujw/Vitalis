# Vitalis

[English](README.en.md) | [文档中心](docs/README.md)

Vitalis 把 Zepp 的睡眠、活动和训练记录整理为可追溯的个人分析与报告。它是预发布的个人趋势工具，不是医疗设备；缺失观测保持缺失，客户端不能自行编造健康事实或训练处方。

## 看一份报告

以下摘录来自程序生成的[合成日报](docs/examples/reports/daily.md)，没有使用真实健康记录：

> **本地日完整事实快照**
>
> 2026-10-07 · 数据截至 21:20 · 合成数据
>
> 睡眠时长：7 小时 26 分钟；步数：6,076 步；力量训练时长：45 分钟。
>
> 日报同时保留已保存事实、数据质量与覆盖、版本和修正记录；力量动作明细保留 12 / 10 / 8 次的真实组次分布。

完整示例：[晨报](docs/examples/reports/morning.md)、[日报](docs/examples/reports/daily.md)、[晚报](docs/examples/reports/evening.md)、[周报](docs/examples/reports/weekly.md)、[月报](docs/examples/reports/monthly.md)；也可打开[日报 HTML 预览](docs/examples/reports/daily.html)。

## 最短演示

以下命令在仓库根目录执行，使用合成数据，不接触真实 Zepp 账号，也不会发送通知。需要 Python 3.11–3.13；安装使用锁定的 `uv` 依赖。首次安装 `uv` 和依赖需要联网；已有 `uv` 及完整依赖缓存时，可用 `uv sync --locked --offline --extra dev` 安装。安装完成后的演示和本地报告导出不需要网络。

```bash
python -m pip install 'uv==0.12.9'
uv sync --locked --extra dev
```

PowerShell 设置显式 mock 环境后，在一个新路径创建数据库并生成报告：

```powershell
$env:ZEPP_MOCK = 'true'
$env:VITALIS_ENV = 'test'
$env:DATABASE_URL = 'sqlite:///./demo.db'
uv run --locked --extra dev vitalis demo --database .\demo.db --day 2026-10-07
uv run --locked --extra dev vitalis report daily --user demo --day 2026-10-07 --format markdown --output .\daily.md
Get-Content .\daily.md
```

`demo` 只接受不存在的 `.db`/`.sqlite` 文件；`report` 从已保存分析读取，写入新文件并拒绝覆盖，不启动同步、不发送 PushPlus。`Get-Content` 会显示刚生成的可读 Markdown。晨报、晚报、周报和月报把 `daily` 替换为相应 kind；日报保留完整本地日事实快照，晚报是面向阅读的当日复盘。完整 API 读取、令牌和真实连接步骤见[快速开始](docs/quickstart.md)。

## 产品边界

- Zepp 数据经同步、规范化和来源核对后进入持久分析快照；API、PushPlus 和 Hermes 读取同一结果。
- 用户范围的 HTTP 请求使用绑定用户的 Bearer 令牌；`X-User-Id` 不能代替鉴权。调度器只由独立 `worker` 启动。
- 报告区分事实、比较、分析和建议；发送被受理不等于已送达、已读、接受或目标完成。
- Hermes 是可选对话入口，只在用户明确授权时记录反馈；产品 Skill 是可离仓的薄 HTTP 客户端，不重新计算健康事实。

## 按角色查找

- 第一次运行和 API 读取：[快速开始](docs/quickstart.md)
- 报告周期、PushPlus 与 Hermes：[报告与渠道](docs/reports.md)
- 产品、数据和报告整改：[预发布整改计划](docs/plan.md)
- 模块职责与数据流：[当前架构](docs/architecture.md)
- 字段、单位、缺失和时间资格：[数据合同](docs/data-contracts.md)
- Zepp 连接与协议边界：[Zepp](docs/zepp.md)
- Skill、Bearer 和客户端边界：[智能体集成](docs/agents.md)
- 配置、worker、备份和排障：[运维](docs/operations.md)
- 本地合并、服务器发布和换库：[发布与部署 SOP](docs/deployment.md)
- 仓库开发：[AGENTS.md](AGENTS.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)
- 安全报告：[SECURITY.md](SECURITY.md)；第三方原文：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

Vitalis 本身当前没有在 `pyproject.toml` 或仓库根目录声明许可证；不要把第三方 MIT/Apache 声明误读为 Vitalis 的许可证。
