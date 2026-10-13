# 运维、备份与排障

[文档中心](README.md) | [快速开始](quickstart.md) | [发布与部署](deployment.md) | [安全](../SECURITY.md) | [报告与渠道](reports.md)

本页说明当前预发布环境的配置、进程、备份和故障判断。敏感数据库、健康记录、凭据和投递目标必须使用真实部署的访问控制；示例只使用合成数据。

## 配置主责

默认值只在 `src/vitalis/config.py` 维护：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `ZEPP_MOCK` | dev/test 为 `true`，prod/production 为 `false` | 生产拒绝 `true`；真实模式需要有效加密密钥。 |
| `VITALIS_ENV` | `dev` | 只接受 dev、test、prod、production；生产环境必须显式配置。 |
| `DATABASE_URL` | `sqlite:///./vitalis.db` | 当前数据库；演示请改到新路径。 |
| `VITALIS_TIMEZONE` | `Asia/Shanghai` | 本地日历边界和调度时区。 |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | API 监听地址。 |
| `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` | `02:00` | worker 自动同步时间。 |
| `SYNC_DISPATCHER_INTERVAL_SECONDS` | `60`（最多 `900`） | 同步、分析和投递 dispatcher 间隔；晨报睡眠等待期要求不超过 15 分钟。 |
| `MORNING_WAKE_START_HOUR` / `MORNING_WAKE_START_MINUTE` | `07:30` | 晨报醒来轮询窗口的最早本地时间。 |
| `MORNING_WAKE_END_HOUR` / `MORNING_WAKE_END_MINUTE` | `11:00` | 晨报醒来轮询窗口的最晚本地时间；超时沿用事实兜底/期限策略。 |
| `MORNING_WAKE_POLL_INTERVAL_SECONDS` | `900`（60–1200） | 醒来轮询间隔；每次只创建一个有幂等键的晨报同步尝试。 |
| `MORNING_WAKE_DAILY_REQUEST_LIMIT` | `12` | 每用户每天醒来轮询的 Zepp 请求预算。 |
| `VITALIS_WEEKLY_REPORT_ENABLED` | `false` | 周报自动投递开关。 |
| `VITALIS_WEEKLY_REPORT_HOUR` / `MINUTE` | `10:00` | 周报投递本地时间。 |
| `VITALIS_MONTHLY_REPORT_ENABLED` | `false` | 月报自动投递开关。 |
| `VITALIS_MONTHLY_REPORT_HOUR` / `MINUTE` | `10:30` | 月报投递本地时间。 |
| `VITALIS_PUSH_USER` / `PUSHPLUS_TOKEN` | 空 | 单向 PushPlus 目标和令牌；为空时不外发。 |
| `PUSHPLUS_ACCESS_KEY` | 空 | 可选的 PushPlus 状态查询凭据；不参与发送，缺失时保留 `accepted`。 |
| `PUSHPLUS_QUERY_MAX_ATTEMPTS` | `3` | 状态查询最多轮询次数；不会重发原消息。 |
| `PUSHPLUS_QUERY_INTERVAL_SECONDS` | `60` | 状态查询间隔秒数；只影响状态查询。 |

晨报 `09:30`、晚报 `21:30` 的调度由当前 worker 任务定义；配置和时区不要在其它文档复制成另一套默认值。晨报需要完整睡眠和已核实的前 7 天训练记录，睡眠未到齐时最多再等 30 分钟，并在等待期内自动补同步一次（每天只补一次，失败不重试）；晚报需要当天事实和训练记录。必需数据缺失、同步降级或发送比调度时刻晚 30 分钟以上时，只发仅事实版本（不带训练安排、建议和变化），睡眠仍缺的晨报开头写明“昨夜睡眠还没同步”；没有当天事实时不发并保留延期记录，`/api/deliveries` 的 `failure_code` 给出原因。调度时刻后晨报 3 小时、晚报 6 小时内仍未发出的，本次投递作废（`delivery_deadline_expired`）。这些规则在 `src/vitalis/application/delivery_policy.py` 和 worker 的延期投递协调中执行。真实模式还需要有效的 `VITALIS_TOKEN_ENCRYPTION_KEY`，并将其与数据库分开保存。

当前第二告警通道暂未配置：worker 只在当前数据库持久化内部告警，不发送邮件、Hermes 或其它网络告警。使用 `GET /api/delivery-alerts` 查看按用户隔离的 `pending`、`suppressed` 和 `acknowledged` 事件；告警不会替代 `/api/deliveries` 的投递账本。`GET /api/data-status` 的每个流还会返回预期间隔、实际观测年龄、`freshness` 和基于真实存储观测的近 7 天覆盖；没有观测时显示未知，不补零。

## 初始化与进程

从仓库根目录、指向一个新数据库：

```powershell
$env:DATABASE_URL = 'sqlite:///./vitalis.db'
uv run --locked --extra dev vitalis db init
uv run --locked --extra dev vitalis doctor
```

`db init` 建立当前 schema，版本号以 `src/vitalis/adapters/persistence/database.py` 的 `SCHEMA_REVISION` 为准，不在文档里抄写；`doctor` 输出 schema 版本、数据库后端、时区和 worker 心跳状态，不输出秘密。旧 schema 不自动迁移，候选库流程见[部署](deployment.md)。启动 API 和 worker 应使用相同环境：

```powershell
uv run --locked --extra dev vitalis serve
uv run --locked --extra dev vitalis worker
```

API 的 `/live` 只表示进程存活，`/ready` 检查当前 schema；`doctor` 的 `not_seen` 表示 worker 尚未写心跳，不等于 API 故障。调度器只在 `worker` 进程启动，不能从 API 应用或普通 GET 触发。

worker 把 `vitalis.*` 的 INFO 日志写到 stderr（systemd 下即 journal），APScheduler 只记警告。日志包含：晨报/晚报任务开始，同步结束（每个睡眠日分片的解析状态，`empty` 表示来源当时还没有睡眠），每个分析任务的开始、耗时和处理后的内存，每次投递的状态和原因，延期投递的处理结果，以及约每 10 分钟一行的内存（进程 RSS/swap、cgroup 用量与 `memory.high` 触发次数）。按时间段查看：`journalctl -u vitalis-worker --since 09:25 --until 10:05`。

worker 的内存上限写在 `deploy/systemd/vitalis-worker.service`：单次分析实测峰值约 360 MB，超过 `MemoryHigh` 时内核会节流整个进程，所以设 `MemoryHigh=500M`、`MemoryMax=640M`（超过后被 OOM 结束并自动重启）。分析日志行里的 `cgroup` 用量和 `high_events` 增长说明正在逼近上限。

每次写入心跳都会喂 systemd 看门狗（unit 设 `WatchdogSec=600`）：进程 10 分钟没有心跳（例如超过 `MemoryHigh` 后被内核节流到停住）会被中止并按 `Restart=on-failure` 重启，中止时 faulthandler 把所有线程的 Python 调用栈打进 journal；未完成的分析在租约过期后重跑。worker 漏掉 09:30/21:30 定时任务时（冻结或在调度时刻之后才启动），启动时和之后每 5 分钟检查一次：配置了 PushPlus 投递且当天该时段还没有定时同步或投递记录时，为投递对象补建一次同步，补跑在投递期限和当天午夜之前停止。

## 发布入口

本页只负责运行、备份和排障。完整的本地检查、服务器候选验证、数据库处理、全部服务切换、运行验收、验收后合并推送 `main`、分支清理和失败回滚见[发布与部署 SOP](deployment.md)。数据库无变化时保留现库，加字段足够时不重建；确实需要破坏性迁移时遵循“备份旧库 → 新库测试 → 验收切换 → 删除旧工作库和备份”，不使用历史 schema 兼容迁移链。

## 备份与恢复

生产 SQLite 运行库启用 WAL，长分析读取不会阻塞 heartbeat 和短事务写入；备份命令仍必须从当前配置库生成到不存在的新路径，并检查 WAL/SHM sidecar。

SQLite 备份必须从当前配置库生成到不存在的新路径：

```powershell
uv run --locked --extra dev vitalis db backup --output C:\secure\vitalis-backup.db
uv run --locked --extra dev vitalis db restore --backup C:\secure\vitalis-backup.db --database C:\secure\vitalis-restored.db
```

命令会检查 schema、完整性、外键和 sidecar；备份、恢复目标和凭据文件不要放入仓库。restore 不覆盖已有文件，也拒绝把备份直接写回当前配置库。恢复后把 `DATABASE_URL` 指向新库，先运行 `doctor`，再启动服务和 worker。不要用文件复制代替带 WAL/SHM 检查的备份。

生产服务器不长期保留备份：发布前生成的备份在验收通过后删除，服务器只保留当前数据库，见[发布与部署 SOP](deployment.md)。原始响应回放仅在离线隔离环境进行；原始响应使用加密不可变 journal 保存，回放副本和临时生产只读副本放在仓库外的受控目录，回放/等价性验收完成后校验删除，不保留凭据、Cookie、PushPlus 内容或未脱敏个人样例。

开发环境若明确要丢弃一个非当前配置的 `.db`/`.sqlite` 文件，必须使用 `db reset --database PATH --confirm-discard-local-data`；生产环境和当前配置库会被拒绝。

## 令牌、同步和投递排障

令牌签发到仓库外的新私密文件：

```powershell
uv run --locked --extra dev vitalis token issue --user demo --scope read --output C:\secure\demo-read.token
```

令牌值不打印到终端或快照。用最小 scope 读取报告；分析、同步、反馈和管理分别需要相应权限。撤销使用非秘密 digest，而不是把令牌放在命令行。

同步排查顺序是：确认来源 token 状态，再查 `GET /api/data-status`，再查 `GET /api/jobs/{job_id}` 的 attempt/chunk，最后查看 worker 日志中的非敏感错误类别。需要重新认证、部分覆盖、超时、取消和未知状态不能互相替代；先保留 `uncertain`，不要盲目重试可能已送达的 PushPlus 请求。

PushPlus 的 HTTP 200 只表示请求完成；只有业务 `code=200` 且有有效 provider message id 才记录 `accepted`。记录 `accepted`、provider message id、最终 `delivered`/`failed`/`uncertain` 分开；投递不会写目标完成或主观反馈。周报/月报开关默认关闭，开启前应使用合成目标做一次受理合同验证；真实 PushPlus 受理已在 2026-10-10 用测试补发验证，Hermes 联调尚未完成。报告的排队、计算和投递耗时等产品指标用 `GET /api/product/metrics` 查看。

## 常见故障

| 现象 | 处理 |
| --- | --- |
| `/ready` 503 | 对照 `DATABASE_URL` 检查 schema；用新库 `db init`，不要自动迁移未知旧库。 |
| worker 无心跳 | 确认独立 worker 进程、同一环境和数据库；查看进程退出原因。 |
| 报告 404 | 指定日期没有已保存快照；创建分析任务并等待 worker，不要把 404 当零数据。 |
| 同步需要认证 | 重新走官方 Zepp 配对；不要在日志或聊天中传 Cookie/apptoken。 |
| 报告延期 | 从 `/api/deliveries` 的 `failure_code` 看原因（如 `sleep_incomplete`、`morning_sleep_retry_queued`、`morning_sleep_retry_failed`、`morning_fallback_no_facts`、`stale_plan_expired`），再查看数据覆盖和 required signals；可用已保存事实时只生成合资格内容。 |
| 投递 uncertain | 保留不确定状态。响应丢失且没有流水号时无法自动查询，需在供应商侧人工核对；不创建新意图盲重发。 |
| 投递 failed | 从 `/api/deliveries` 查看 `provider_status`、`send_attempt_count` 和 `next_attempt_at`，结合 worker 的非敏感错误类别排查凭据/模板/目标；明确拒绝的意图由 worker 按退避和最大次数重试。 |

开发检查从仓库根目录运行 `uv run --locked --extra dev python tools/check.py docs`、目标 pytest 和 `git diff --check`。故障报告包含命令、退出码、非敏感状态和未验证项，不包含数据库、健康记录或令牌内容。
