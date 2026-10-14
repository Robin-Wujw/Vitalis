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
| `SYNC_DISPATCHER_INTERVAL_SECONDS` | `60`（最多 `900`） | 同步、分析和投递 dispatcher 间隔；醒来探测和晨报同步也由它执行，建议保持 60 秒。 |
| `MORNING_WAKE_START_HOUR` / `MORNING_WAKE_START_MINUTE` | `07:30` | 晨报窗口开始：最早从这一刻起探测睡眠、发送晨报。 |
| `MORNING_WAKE_END_HOUR` / `MORNING_WAKE_END_MINUTE` | `11:00` | 晨报窗口结束：仍没有可信睡眠时按兜底规则发送；必须晚于开始时间。 |
| `MORNING_WAKE_POLL_INTERVAL_SECONDS` | `900`（60–1200） | 两次“只拉睡眠”探测之间的最短间隔。 |
| `MORNING_WAKE_MAX_PROBES` | `14` | 每用户每天最多探测次数；每次探测只发 1 个 Zepp 请求。 |
| `VITALIS_WEEKLY_REPORT_ENABLED` | `false` | 周报自动投递开关。 |
| `VITALIS_WEEKLY_REPORT_HOUR` / `MINUTE` | `10:00` | 周报投递本地时间。 |
| `VITALIS_MONTHLY_REPORT_ENABLED` | `false` | 月报自动投递开关。 |
| `VITALIS_MONTHLY_REPORT_HOUR` / `MINUTE` | `10:30` | 月报投递本地时间。 |
| `VITALIS_PUSH_USER` / `PUSHPLUS_TOKEN` | 空 | 单向 PushPlus 目标和令牌；为空时不外发。 |
| `PUSHPLUS_ACCESS_KEY` | 空 | 可选的 PushPlus 状态查询凭据；不参与发送，缺失时保留 `accepted`。 |
| `PUSHPLUS_QUERY_MAX_ATTEMPTS` | `3` | 状态查询最多轮询次数；不会重发原消息。 |
| `PUSHPLUS_QUERY_INTERVAL_SECONDS` | `60` | 状态查询间隔秒数；只影响状态查询。 |
| `VITALIS_PUBLIC_URL` | 空 | 手机能访问的 API 地址（无路径）。设了才生成手机登录链接和报告里的反馈链接；生产要求 HTTPS，或私有网络地址（如 ZeroTier 的 `http://10.x.x.x`）。 |

晨报跟着醒来时间走，晚报是 `21:30` 的定时任务；配置和时区不要在其它文档复制成另一套默认值。worker 每分钟检查一次晨报窗口：窗口内至多每隔 `MORNING_WAKE_POLL_INTERVAL_SECONDS` 只拉一次当天的睡眠（每天至多 `MORNING_WAKE_MAX_PROBES` 次）；库里一出现可信的昨夜主睡眠，就排一次完整的晨报同步，同步结束后分析并投递。可信指有醒来时间、睡眠至少 3 小时、醒来不早于窗口开始前 3 小时：手机夜里同步时 Zepp 会留下以上传时刻为“醒来”的半截睡眠（2026-10-11 02:00 的记录一直保留到 09:57 醒来后才更新），不能据此发晨报。到窗口结束仍没有可信睡眠，也排这次同步并按兜底规则发送；晨报同步失败或取消且没写入任何记录时再试一次。worker 重启后按已保存的探测和同步记录续跑，晨报不需要补建。

晨报需要完整睡眠和已核实的前 7 天训练记录，窗口结束前睡眠没到齐就继续等；晚报需要当天事实和训练记录。窗口结束后睡眠仍缺、必需数据缺失、同步降级，或发送比计划晚 30 分钟以上（晨报以窗口结束、晚报以 21:30 计）时，只发仅事实版本（不带训练安排、建议和变化），开头一句话说明原因，例如“昨夜睡眠还没同步”；没有当天事实时不发并保留延期记录，`/api/deliveries` 的 `failure_code` 给出原因。晨报在窗口结束后 90 分钟（默认 12:30）、晚报在 21:30 后 6 小时仍未发出的，本次投递作废（`delivery_deadline_expired`）。这些规则在 `src/vitalis/application/delivery_policy.py`、worker 的晨报窗口任务和延期投递协调中执行。真实模式还需要有效的 `VITALIS_TOKEN_ENCRYPTION_KEY`，并将其与数据库分开保存。

`GET /api/data-status` 的每个流还会返回预期间隔、实际观测年龄、`freshness` 和基于真实存储观测的近 7 天覆盖；没有观测时显示未知，不补零。`workout_details` 给出训练总数、已有明细数、缺明细数、最早缺明细的训练时间和最近一次因体积超限跳过的时间。

worker 每 30 分钟检查一次数据流新鲜度：周期性的流（睡眠、密集心率文件、夜间 RMSSD、压力、血氧点、日汇总等）超过预期间隔仍无新数据时，补拉一次最近 2 天（`trigger=freshness`）；补拉后又过 6 小时、且距补拉至少 1 小时仍停在同一条最后观测上，记一条 `stream_stale` 告警（同一停更只记一次，有新数据后重新计）。补拉记录是 `stream_refetch_queued`、状态 `suppressed` 的事件，只用于去重。按本地日零点标时间的日值流（日汇总、PAI、呼吸频率、ODI）次日才到，预期间隔按 48 小时算。`hrv`（Zepp 手动单次 HRV 测量，SDNN）、`heart_rate/minute_endpoint`（佩戴上传密集文件的手表这里一直为空，分钟心率来自密集文件）、乳酸阈、VO₂max、运动负荷汇总、训练、训练明细和设备列表按需出现，不做新鲜度告警。

## 投递目标与告警

目标是晨报在窗口结束后 30 分钟内（默认 11:30）、晚报在 22:30 前被 PushPlus 受理（业务 `code=200`）。告警检查不在 worker 里：`vitalis-alerts.timer` 每 10 分钟启动一次 `vitalis alerts check`，独立进程，worker 冻住时照样运行。到目标仍未受理、PushPlus 明确拒绝、24 小时内分析失败，或 worker 心跳超过 30 分钟未更新时各记一次事件，同一事件不重复。没有投递意图的漏发在 `GET /api/deliveries` 里显示为 `status=missed` 的记录，`failure_code` 为 `worker_not_running`、`snapshot_unavailable` 或 `delivery_not_created`。

数据流停更（`stream_stale`）和 Zepp 凭据失效（`credential_expired`）也写进同一张告警表。第二告警通道（邮件或 Hermes）用户决定暂不接入（2026-10-13，2026-10-14 再次确认）：事件只写入当前数据库，用 `GET /api/delivery-alerts` 查看，`external_channel` 为 `unconfigured`；`journalctl -u vitalis-alerts` 里每个新事件一行，只含原因类别和日期。

漏发按这个顺序查：`GET /api/delivery-alerts` 的原因 → `GET /api/deliveries` 当天记录的 `status` 和 `failure_code` → `systemctl status vitalis-worker` 与 `vitalis doctor` 的心跳 → `journalctl -u vitalis-worker --since 07:25` 里的晨报探测、同步结束（睡眠分片状态）、分析和投递日志 → `GET /api/data-status` 的数据新鲜度。

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

worker 把 `vitalis.*` 的 INFO 日志写到 stderr（systemd 下即 journal），APScheduler 只记警告。日志包含：晨报/晚报任务开始，同步结束（每个睡眠日分片的解析状态，`empty` 表示来源当时还没有睡眠），每个分析任务的开始、耗时和处理后的内存，每次投递的状态和原因，延期投递的处理结果，以及约每 10 分钟一行的内存（进程 RSS/swap、cgroup 用量与 `memory.high` 触发次数）。晨报探测和同步各记一行（`morning_probe queued`、`morning report sync queued: … reason=sleep_found|window_end`）。按时间段查看：`journalctl -u vitalis-worker --since 07:25 --until 11:30`。

worker 的内存上限写在 `deploy/systemd/vitalis-worker.service`（`MemoryHigh=500M`、`MemoryMax=640M`，超过后被 OOM 结束并自动重启）。分析日志行里的 `cgroup` 用量和 `high_events` 增长说明正在逼近上限。

## 日常分析路径与离峰任务

日常分析读入库时写好的日级特征（`daily_signal_features`）：HRV、血氧、压力和 Charge 这类密集样本流，只有目标日和前一天读原始样本，更早的日子每个流每天一行（合格值的中位数、最新值、样本数、覆盖分钟数），不再截断样本。特征和样本在同一个事务里写入；首次部署或时区变化后用 `vitalis db rebuild-features` 从已存样本重算（约 1 年数据 2 分钟）。每次分析在 `analysis_runs` 记录耗时 `compute_seconds` 和进程峰值内存 `peak_rss_mb`；耗时超过 30 秒或分析新增的峰值内存（峰值减开始时常驻）超过 150 MB 时，`budget_exceeded` 写明超出项，worker 日志记一行警告。生产库副本上单次分析约 7–8 秒、含解释器的进程峰值约 140–150 MB（旧路径约 26 秒、320 MB）。

影子模块不在日常路径计算，也不进报告：worker 每天 03:30 跑离峰批处理，算前一天的开放健康洞察（EWMA 准备度、TRIMP 负荷、多变量异常）、0–100 准备度、身体信号预警、个人反应模型和四项系统质量指标，各存一行 `shadow_results`。`vitalis shadow run --day D --days N` 补算或在历史上回放策略改动，`vitalis shadow report readiness|body_signal --user U --days 14` 输出对比（准备度对计票结论的一致率、对厂商 Readiness 的秩相关；预警日列表需要用户确认才能算误报率）。

## Zepp 手机登录与重新登录

API 只在本机监听，手机通过 ZeroTier 私有网络访问：`deploy/systemd/vitalis-private-proxy.socket` 只在 ZeroTier 地址的 80 端口监听（模板里的地址换成实际 ZeroTier 地址），由 `systemd-socket-proxyd` 转发到 `127.0.0.1:8000`；`VITALIS_PUBLIC_URL` 设为 `http://<ZeroTier 地址>`。ZeroTier 端到端加密，所以这条私有链路允许 HTTP；公网地址仍必须 HTTPS。`vitalis connect zepp --user U [--minutes 60] [--push]` 生成手机登录链接（可直接经 PushPlus 发到手机），密码只经过自己的服务器一次、不保存。

最近一次同步返回需要重新认证时，worker 每小时先用保存的 `login_token` 换新的 `app_token`；换不了（没有 `login_token`、接口拒绝或返回别的账号）就经 PushPlus 发一个 24 小时有效的重新登录链接，每天最多一次，并记一条 `credential_expired` 告警。

## 报告反馈链接

配置了 `VITALIS_PUBLIC_URL` 时，晨报和晚报末尾带一个一次性反馈链接（36 小时有效）：今天感觉 1–5 分、训练 RPE、酸痛部位和行为标签（饮酒、晚餐晚、咖啡因、生病、出差）。链接只存令牌摘要，用一次即失效；只有点提交才写反馈，打开或不填都不算，提交后对应日期重新分析。计划是否执行由训练记录判断，页面直接显示，不让用户填。

## 训练明细补拉与原始响应回放

`vitalis sync details --user U` 在 worker 运行时逐个排“只拉明细”的手动同步（每次最多 4 条），直到没有缺明细的训练或某次没有进展，进度看 `GET /api/data-status` 的 `workout_details`。

解析器修正后：`vitalis replay source-journal --source-user U --target-user R --start A --end B --as-of T --allow-production` 用新解析器把加密原始响应回放进独立用户 R；`vitalis replay diff --source-user U --replay-user R --start A --end B` 逐表列出新增、删除和变化的事实；确认后 `vitalis replay apply … --confirm` 用回放结果替换 U 在该窗口（最多 14 天）的事实，被替换的行压缩保存在 `replay_applications`，`vitalis replay undo --application ID --confirm` 恢复。apply 和 undo 都会重算日级特征并排对应日期的重新分析。原始响应随数据库保存、不另设期限；回放用户和 `replay_applications` 在确认无误后按[部署](deployment.md)清理旧数据的方式删除，不长期保留。

每次写入心跳都会喂 systemd 看门狗（unit 设 `WatchdogSec=600`）：进程 10 分钟没有心跳（例如超过 `MemoryHigh` 后被内核节流到停住）会被中止并按 `Restart=on-failure` 重启，中止时 faulthandler 把所有线程的 Python 调用栈打进 journal；未完成的分析在租约过期后重跑。worker 漏掉 21:30 晚报定时任务时（冻结或在调度时刻之后才启动），启动时和之后每 5 分钟检查一次：配置了 PushPlus 投递且当天还没有晚报定时同步或投递记录时，为投递对象补建一次同步，补跑在投递期限和当天午夜之前停止。

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

生产服务器不长期保留备份：只有破坏性迁移的发布才在切换前备份，验收通过后删除；服务器只保留当前数据库，见[发布与部署 SOP](deployment.md)。原始响应使用加密不可变 journal 保存，回放副本和临时生产只读副本放在仓库外的受控目录，回放/等价性验收完成后校验删除，不保留凭据、Cookie、PushPlus 内容或未脱敏个人样例。

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
| 同步需要认证 | worker 会先用 `login_token` 续期，失败时经 PushPlus 发重新登录链接；也可运行 `vitalis connect zepp --user U --push`。不要在日志或聊天中传 Cookie/apptoken。 |
| 报告延期或漏发 | 从 `/api/deliveries` 的 `failure_code` 看原因（如 `sleep_incomplete`、`stored_data_incomplete`、`morning_fallback_no_facts`、`stale_plan_expired`；`missed` 记录为 `worker_not_running`、`snapshot_unavailable`、`delivery_not_created`），再按上文“漏发排查顺序”查；可用已保存事实时只生成合资格内容。 |
| 投递 uncertain | 保留不确定状态。响应丢失且没有流水号时无法自动查询，需在供应商侧人工核对；不创建新意图盲重发。 |
| 投递 failed | 从 `/api/deliveries` 查看 `provider_status`、`send_attempt_count` 和 `next_attempt_at`，结合 worker 的非敏感错误类别排查凭据/模板/目标；明确拒绝的意图由 worker 按退避和最大次数重试。 |

开发检查从仓库根目录运行 `uv run --locked --extra dev python tools/check.py docs`、目标 pytest 和 `git diff --check`。故障报告包含命令、退出码、非敏感状态和未验证项，不包含数据库、健康记录或令牌内容。
