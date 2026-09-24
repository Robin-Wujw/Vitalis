# 快速入门

[English](GETTING_STARTED.en.md)

本指南涵盖本地配置、服务运行、部署、调度和验证。产品定位请参阅根目录的 [README](../README.md)；健康智能契约和计算边界请参阅 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 要求

- Python 3.11 或更高版本
- 本地开发使用 SQLite，持久化部署使用 PostgreSQL
- 进行真实的浏览器扩展配对时，需要浏览器信任的 HTTPS 源

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m vitalis.main
```

默认的 `ZEPP_MOCK=true` 模式不需要供应商凭据，并会提供确定性的开发数据。API 在 `http://127.0.0.1:8000` 上提供服务；FastAPI 文档位于 `/docs`。

重要配置：

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | SQLite 或 PostgreSQL 连接字符串 |
| `ZEPP_MOCK` | 设为 `true` 时使用确定性开发连接器 |
| `VITALIS_TIMEZONE` | 智能计算使用的本地日期边界 |
| `HOST` / `PORT` | 应用程序监听地址与端口 |
| `VITALIS_PUBLIC_URL` | 配对页面使用的公共 HTTPS 源 |
| `ZEPP_PAIRING_PROCESSING_LEASE_SECONDS` | 被中断的配对提交可被重新接管前的等待时间 |
| `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` | 夜间同步入队时间 |
| `SYNC_DISPATCHER_INTERVAL_SECONDS` | 持久化账本调度器程序各轮次之间的间隔 |
| `SYNC_DISPATCHER_BATCH_CHUNKS` | 每轮公平调度最多处理的分块数 |
| `SYNC_LEASE_SECONDS` / `SYNC_ATTEMPT_LEASE_SECONDS` | 分块和尝试的隔离租约时长 |
| `VITALIS_NO_SCHEDULER` | 设为 `1` 时禁用后台任务和重试恢复 |

确定性分析引擎不需要 LLM。Hermes 或其他智能体会读取结构化的 Vitalis 结果，并单独进行呈现。

### 现有数据库身份迁移

当前架构要求每个非空 Zepp 供应商身份只能有一个本地所有者。新数据库会在 `init_db()` 期间获得唯一索引。对于由旧版本创建的数据库，请先停止 API、工作进程和调度器，并完成经过测试的备份，再启动新代码。在不读取令牌值的情况下审计现有映射：

```bash
python -m vitalis.storage.identity_migration audit
```

如果报告并非完全正常，请使用 [ZEPP_INTEGRATION.md](ZEPP_INTEGRATION.md#来源身份所有权) 中记录的显式 `resolve`、`resolve-local`、`assign-missing`、`resolve-projection` 或 `clear-projection` 命令。每项冲突都由操作人员核实解决后，应用迁移：

```bash
python -m vitalis.storage.identity_migration migrate --apply
```

当重复映射妨碍创建唯一索引时，启动会因 `SourceIdentityMigrationRequired` 而失败。不要绕过该检查，也不要通过删除数据库来掩盖冲突。迁移会保留双方用户的历史健康数据，仅释放非规范凭据和浏览器链接。

### 现有 SQLite 架构迁移

`create_all()` 会创建全新的当前架构，但不会更改现有 SQLite 表。使用较新代码启动长期使用的数据库之前，请停止 Vitalis 进程，并审计列、唯一约束、检查约束和索引：

```bash
python -m vitalis.storage.schema_migration audit
```

对于已知的旧版布局，请运行显式迁移，并提供历史数据源和必需的备份路径：

```bash
python -m vitalis.storage.schema_migration migrate \
  --legacy-source zepp \
  --backup backups/vitalis-before-current-schema.db \
  --apply
```

该命令会在更改表之前创建并验证 SQLite 备份。它会依据当前 SQLAlchemy 元数据重建发生漂移的表，映射已知的新增列，保留行数，规范化旧版空设备 ID，重新创建当前索引，并以架构和外键检查收尾。它拒绝重新解释非空的旧版 `sync_attempts` 或 `sync_chunks` 账本。未知的架构差异需要单独审查，不得强制迁移。

再次运行审计，并且仅在报告 `clean=true` 时启动 API/工作进程。

## Hermes 运行时

以仓库中的 Skill 为唯一事实来源。在 Linux 上从仓库根目录将其链接到 Hermes 的本地 `skills` 发现目录：

```bash
mkdir -p "$HOME/.hermes/skills/health"
ln -s "$PWD/skills/vitalis" "$HOME/.hermes/skills/health/vitalis"
```

原生 Windows 的 Hermes 使用当前 `HERMES_HOME`（通常为 `%LOCALAPPDATA%\\hermes`）。在该目录的私有 `config.yaml` 中合并以下配置；路径指向仓库中 Skill 的**父目录**，不要复制 Skill 或覆盖其他配置：

```yaml
skills:
  external_dirs:
    - D:/MyCodes/Vitalis/skills
```

在同一配置目录的私有 `.env` 中指定环回 API 和**选定的**本地用户，不要提交用户身份到仓库：

```dotenv
VITALIS_API=http://127.0.0.1:8000
VITALIS_USER=<local-user-id>
NO_PROXY=127.0.0.1,localhost
```

先审计数据库架构，在环回地址启动 Vitalis；然后开启新的 Hermes 会话验证发现与只读读取：

```bash
hermes skills list --source local --enabled-only
hermes --skills vitalis prompt-size --json
python skills/vitalis/tools/daily.py --user YOUR_LOCAL_USER_ID --date YYYY-MM-DD
hermes chat --skills vitalis -q "昨天的睡眠有哪些已记录的事实？"
```

独立 shell 不会自动读取 Hermes 私有 `.env`：将 `YOUR_LOCAL_USER_ID` 换成已选的本地用户 ID，或先在当前 shell 设置 `VITALIS_USER` 和 `VITALIS_API`。工具命令在仓库根目录、Vitalis Python 环境下执行；原生 Windows 可显式使用 `D:/MyCodes/Vitalis/.venv/Scripts/python.exe`。没有快照时返回 `status=snapshot_missing`，不得自动同步、分析或改用其他日期。今日训练建议取已保存的晨报，解释依据取 `explain`；Hermes 只编排和复述 Vitalis 的结构化证据，不计算、合并、诊断或虚构训练计划。

`VITALIS_API` 必须保持环回访问，不能将未经鉴权的健康智能路由暴露在公共反向代理后；`X-User-Id` 只选择身份，不验证调用方，包括同一机器的其他进程。设置 `VITALIS_USER` 后，工具拒绝模型传入其他 `--user`。Hermes 可能保存对话，并将必要的结构化健康摘要发送到其配置的模型提供方；提问前应确认该提供方符合个人数据要求。

### 每日 PushPlus 报告

本地生产配置可使用 Hermes Cron 作为每日调度入口；也可使用 Vitalis 内置调度器，但两者是替代入口，不应同时负责投递。使用 Hermes 时，Vitalis 作为环回系统服务运行，并禁用其嵌入式调度器。Hermes 晨间任务在 `Asia/Shanghai` 时区的 09:30 至 21:30 之间每小时运行一次，为明确指定的 `VITALIS_USER` 同步两天数据，并且只分析当前本地日期。当今天的睡眠状态不可用或缺少 `wake_time` 时，它不会发送任何内容。下一次每小时运行会再次同步和检查；睡眠完成后，私有状态标记会跳过该日期已记录的晨间投递。晨报按时间分别展示昨夜睡眠与身体读数、昨天已记录的活动和训练、今天截至分析时的活动；当天尚未有 workout 不是缺项。昨天的记录不会冒充今天的训练。

睡眠完整但前七天训练历史仍无法完整确认时，晨报继续展示睡眠、身体读数、按日期标注的活动与已记录训练，并用一句话说明运动来源尚未查全；不提供今天的训练动作、强度或重量处方。睡眠未完成、日期过期或凭据失效仍阻止投递。事实版成功投递也写入当天晨报去重标记，后续每小时重试不会重复发送；测试模式保持正式标记不变。

另一个 Hermes 任务在 22:30 运行，同步一天数据，并发送一份当天的晚间报告。晚间报告按 `started_at` 回顾当天真实训练明细；有返回时展示跑步指标、确认力量记录和有序 `observed_sets`，确认记录优先，保留 `source` literal、code、缺失与未知单位，不补写 `kg` 或动作名称。它还回顾每日活动和压力、七晚睡眠 HRV 趋势，以及截至今天的滚动训练负荷，然后给出切实可行的恢复行动，并将明天的强度安排留到下一次完整夜间评估。两种报告均采用 PushPlus 的 HTML 模板和可移植的内联样式发送；报告值会在生成 HTML 之前进行转义。HTML 根元素自带高对比度浅色背景，因此 PushPlus 深色模式不会将深色报告文字直接置于黑色背景上。晚间报告不会显示仅睡眠时段的 RMSSD 曲线，也不会根据稀疏样本推断连续的日间 HRV、压力或情绪。

### 四报告读取与反馈

`GET /api/v1/intelligence/evening-briefing`、`GET /api/v1/intelligence/weekly-briefing` 和 `GET /api/v1/intelligence/monthly-briefing` 返回 `ReportBriefing 1.0`，并与 HTML renderer 使用相同的 `sections`。它们是显式读取能力，不从 raw profile 自由拼接，也不新增 cron；Monthly renderer 只有在被明确调用时运行。报告不会自动请求主观反馈或主动催填 RPE。用户主动提供反馈时，仍可调用 `tools/feedback.py add`，已有反馈分析继续保留。

将 PushPlus 令牌添加到 Hermes 私有的 `~/.hermes/.env`：

```dotenv
PUSHPLUS_TOKEN=<private-pushplus-token>
```

cron 工具在执行时读取该私有文件，因此添加或轮换令牌无需重启 Gateway。使用以下命令验证持久化运行时并检查投递历史：

```bash
systemctl status vitalis.service hermes-gateway.service
hermes cron status
hermes cron list
hermes cron runs <job-id>
```

要发送一次真实的手动测试，同时不读取或写入计划投递标记，请用 `--test` 运行报告工具：

```bash
/root/Vitalis/.venv/bin/python /root/Vitalis/skills/vitalis/tools/daily_push.py \
  --period evening --test
```

需要跨日补发时，可在 `--period evening --test` 后添加 `--date YYYY-MM-DD`。仅支持最近 7 天，工具会自动扩大同步范围以覆盖指定日期；补发仅展示该日期的历史事实，不输出过期训练处方、今晚恢复或明天衔接建议，也不改变正式投递标记。晨报、非测试投递和未来日期不支持此模式。

运行 `hermes cron run <job-id>` 属于正式的计划调用：成功投递会写入每日标记，并防止该时段被重复发送。

当缺少 `VITALIS_USER` 或 `PUSHPLUS_TOKEN`、同一配置来源的用户与令牌未成对、`--user` 与已配置身份不一致，或进程环境与 Hermes 私有配置的用户/令牌冲突时，该工具会在同步之前退出。配置的令牌必须属于该用户；令牌绝不会传递给模型、包含在 URL 中，也不会写入仓库文件和日志。

内置调度器的投递配置与 Hermes 私有 Cron 不同：在内置调度器所在进程的私有环境中同时设置 `VITALIS_PUSH_USER`（接收者对应的本地用户 ID）和 `PUSHPLUS_TOKEN`。一个全局令牌只绑定一个本地用户；其他用户仍可同步和分析，但不会向该令牌投递。任一配置缺失时，内置晨报和晚报均不投递，也不写成功标记。Hermes 工具仍使用明确指定的 `VITALIS_USER` 和它自己的私有令牌，不依赖 `VITALIS_PUSH_USER`。

## 公共部署

当前完整 API 仅适合可信本机或内网访问。`X-User-Id` 不是鉴权；即使上游监听 `127.0.0.1`，将其直接接到 Cloudflare Quick Tunnel 或公开的 HTTPS 反向代理，也会把健康数据读取、修改和设备令牌签发接口一并暴露。不要将完整应用直接公开，且不要仅靠 TLS、临时隧道地址或 CORS 保护它。`VITALIS_PUBLIC_URL` 应保持为空，除非所有敏感路由前已有可信的身份鉴别及本地用户授权绑定；浏览器配对需要与该网关相容的单独端到端设计。

## 计划任务

同步、分析和推送呈现是相互独立的阶段。下表是内置调度器的实际时间；Hermes 的
09:30-21:30 每小时晨间任务和 22:30 晚间任务是替代入口，部署时只启用一个投递入口：

| 本地时间 | 任务 | 行为 |
| --- | --- | --- |
| 02:00 | 夜间同步 | 将 7 天的数据同步任务入队；持久化尝试成功后进行分析 |
| 09:30 | 晨间（单次） | 将 2 天的数据同步任务入队；睡眠未完整时不投递，内置调度当天不会自动重试 |
| 21:30 | 晚间（单次） | 将 1 天的数据同步任务入队；同步成功后分析并尝试投递晚报 |

未设置 `VITALIS_NO_SCHEDULER=1` 时，FastAPI 生命周期在 `python -m vitalis.main` 或直接执行 `uvicorn vitalis.api.app:app` 时启动内置调度器。每轮调度最多处理 `SYNC_DISPATCHER_BATCH_CHUNKS` 个到期分块，并按尝试的最后更新时间轮换。网络操作在数据库事务之外运行；可续期的尝试/分块租约可防止陈旧进程在任务被接管后继续认领或完成其他工作。仓库提供的 `vitalis-api.service` 显式禁用内置调度器，`vitalis-worker.service` 只执行到期同步分块；仅安装这两个单元不会创建定时报表任务，还需要另一个明确负责入队和投递的入口（例如已配置的 Hermes Cron）。

信息不足的资料仍然是信息不足。调度器不会用更早的结果、默认分数或通用训练模板来替代它。

## 仓库布局

```text
vitalis/
|-- connectors/          Source authentication, fetch, and normalization
|-- models/              Current normalized health contracts
|-- storage/             SQLAlchemy persistence
|-- intelligence/        Deterministic health intelligence pipeline
|-- services/            Synchronization, aggregation, and push services
|-- api/                 FastAPI routes
`-- scheduler/           Independent sync, analysis, and push jobs
skills/vitalis/          Hermes Read / Analyze / Act integration
tests/                   Unit and API coverage
browser_extension/       Official-page Zepp browser pairing
zepp_os/balance2_bridge/ Balance 2 device-side heart-rate bridge
```

## 验证

`schema_export.py` 从 Pydantic 契约生成 7 份确定性导出，使用标准本地 `$ref`，调用方不得假定只有 explanation 一份导出。该导出只反映当前契约，不包含个人健康数值或真实记录。

运行完整测试套件：

```bash
.venv/bin/python -m pytest -q
```

当前已验证的结果记录在 `SYSTEM.md` 中。测试套件涵盖连接器解析与同步、浏览器配对、健康数据 API、设备隔离、基线、每日/每周/每月智能、健康事件生命周期、训练响应、个人关联、不可变快照、有界 Context、Timeline、推送呈现和 Hermes Skill 契约。

交付前使用的其他检查：

```bash
.venv/bin/python -m compileall -q vitalis skills/vitalis/tools
.venv/bin/python /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/vitalis
git diff --check
```

## 开发契约

本仓库处于预生产阶段，且仅支持当前契约。它不维护旧版端点、自动回填、双重读取或旧数据适配器；历史训练明细仅按明确的手动请求有界补采。契约变更后，应重新摄取可丢弃的本地数据；已知旧版 SQLite 布局必须先使用上面的显式 schema migration 命令审计并迁移。缺失的观测必须保持缺失，不得用零或伪造的测量值替代。

从较早的代码检出版本升级时，停止所有 API、调度器和同步工作进程，保留经过验证的备份，并运行 `vitalis.storage.schema_migration audit`。对于已知布局，按历史数据源运行 `vitalis.storage.schema_migration migrate` 并提供 `--backup`，再次审计且仅在报告 `clean=true` 时启动当前代码。它会保留可迁移的历史数据；未知差异必须单独审查，不得强制迁移。不要让新旧版本同时写入同一个数据库。对于无法迁移的契约变化，才创建新的空 SQLite 数据库或 PostgreSQL 应用程序架构，让 `init_db()` 创建表，然后重新连接 Zepp、同步所需历史记录并运行新的分析。

重新摄取数据后，请验证每张每日数据表对于每个 `(user_id, date)` 只有一行，来自不同来源/作用域/设备的相同时间指标仍彼此分离，限定来源的训练详情和用户链接可以独立解析，并且每份每日训练摘要均与按 `VITALIS_TIMEZONE` 分组的所有规范训练记录一致。

Open Health Insights 还要求使用当前的训练详情架构，包括 `workout_metric_samples.source`。在真实安装环境中启用它之前，请重新构建全新架构，重新同步至少所需的 42 天负荷窗口（建议 180 天），然后 PATCH 用户确认的资料字段，例如 `sex` 和 `confirmed_hrmax_bpm`。不要让训练观测或设备区间候选值在无提示的情况下填充已确认的资料值。

所需的计划、测试、文档、提交和交付工作流记录在 [SYSTEM.md](../SYSTEM.md) 中。
