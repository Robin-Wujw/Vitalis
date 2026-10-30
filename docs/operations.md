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
| `MORNING_WAKE_END_HOUR` / `MORNING_WAKE_END_MINUTE` | `13:00` | 晨报窗口结束：仍没有已结束的睡眠时按兜底规则发送；必须晚于开始时间。 |
| `MORNING_WAKE_POLL_INTERVAL_SECONDS` | `900`（60–1200） | 两次“只拉睡眠”探测之间的最短间隔。 |
| `MORNING_WAKE_MAX_PROBES` | `22` | 每用户每天最多探测次数（默认间隔下覆盖 07:30–12:45）；每次探测只发 1 个 Zepp 请求。 |
| `VITALIS_WEEKLY_REPORT_ENABLED` | `false` | 周报自动投递开关。 |
| `VITALIS_WEEKLY_REPORT_HOUR` / `MINUTE` | `10:00` | 周报投递本地时间。 |
| `VITALIS_MONTHLY_REPORT_ENABLED` | `false` | 月报自动投递开关。 |
| `VITALIS_MONTHLY_REPORT_HOUR` / `MINUTE` | `10:30` | 月报投递本地时间。 |
| `VITALIS_PUSH_USER` / `PUSHPLUS_TOKEN` | 空 | 单向 PushPlus 目标和令牌；为空时不外发。 |
| `PUSHPLUS_ACCESS_KEY` | 空 | 可选的 PushPlus 状态查询凭据；不参与发送，缺失时保留 `accepted`。 |
| `PUSHPLUS_QUERY_MAX_ATTEMPTS` | `3` | 状态查询最多轮询次数；不会重发原消息。 |
| `PUSHPLUS_QUERY_INTERVAL_SECONDS` | `60` | 状态查询间隔秒数；只影响状态查询。 |
| `VITALIS_PUBLIC_URL` | 空 | 浏览器能访问的 API 地址（无路径）。设了才生成 Zepp 登录链接和报告里的反馈链接；生产要求 HTTPS，或私有网络地址（如 ZeroTier 的 `http://10.x.x.x`）。 |

晨报跟着醒来时间走，晚报是 `23:00` 的定时任务，先同步当天数据，再分析并投递；配置和时区不要在其它文档复制成另一套默认值。worker 每分钟检查一次晨报窗口：窗口内至多每隔 `MORNING_WAKE_POLL_INTERVAL_SECONDS` 拉一次当天的 band 睡眠摘要（每天至多 `MORNING_WAKE_MAX_PROBES` 次，每次 1 个请求；同一响应还包含分钟心率）。主睡眠至少 3 小时、醒来不早于窗口开始前 3 小时，并且醒来满 60 分钟后的一次成功读取仍包含该日睡眠、同一响应的设备数据已推进到醒来后至少 60 分钟，才排一次完整晨报同步。睡眠时长接近平时不能跳过检查；心率在睡觉时也存在，不能把醒来后 30 分钟心率当成已经清醒。单独心率接口的新数据不能为旧睡眠读数放行。读取时间按请求开始计，空响应、不完整响应、未解析到该日睡眠的响应都不能确认。这里确认的是上传新鲜度，不能保证厂商已经识别出所有续睡。

醒来时间一满 60 分钟，worker 立即补拉一次，不等下一个探测间隔；云端数据未推进就按间隔继续等。新上传把醒来时间往后推时，从新醒来时间重新计。完整晨报同步发现续睡、投递因 `sleep_incomplete` 延期后，继续有限次数探测；有效复读会排分析任务，即使睡眠数值没变也会重新核对完成资格，并恢复原投递意图，不创建第二次完整晨报同步或重复投递。到窗口结束仍没有确认睡眠，也排晨报同步并按兜底规则发送；晨报同步失败或取消且没写入任何记录时再试一次。worker 重启后按已保存的探测和同步记录续跑。

晨报需要已结束的睡眠和已核实的前 7 天训练记录，窗口结束前睡眠没到齐就继续等；晚报需要当天事实和训练记录。窗口结束后睡眠仍缺或仍未结束、必需数据缺失、同步降级，或发送比计划晚 30 分钟以上（晨报以窗口结束、晚报以 23:00 计）时，只发仅事实版本（不带训练安排、建议和变化），开头一句话说明原因，例如“昨夜睡眠还没同步”“昨夜睡眠可能还没同步完整”；没有当天事实时不发并保留延期记录，`/api/deliveries` 的 `failure_code` 给出原因。晨报在窗口结束后 90 分钟（默认 14:30）、晚报在 23:00 后 90 分钟（次日 00:30，夏令时切换日按实际经过时长；用户 2026-10-18 定：不在睡觉时推送，晚到的训练由次晨的晨报说明）仍未发出的，本次投递作废（`delivery_deadline_expired`）。这些规则在 `src/vitalis/application/delivery_policy.py`、worker 的晨报窗口任务和延期投递协调中执行。真实模式还需要有效的 `VITALIS_TOKEN_ENCRYPTION_KEY`，并将其与数据库分开保存。

`GET /api/data-status` 的每个流还会返回预期间隔、实际观测年龄、`freshness` 和基于真实存储观测的近 7 天覆盖；没有观测时显示未知，不补零。`workout_details` 给出训练总数、已有明细数、缺明细数、最早缺明细的训练时间和最近一次因体积超限跳过的时间。

worker 每 30 分钟检查一次数据流新鲜度：周期性的流（睡眠、密集心率文件、夜间 RMSSD、压力、血氧点、日汇总等）超过预期间隔仍无新数据时，补拉一次最近 2 天（`trigger=freshness`）；补拉后又过 6 小时、且距补拉至少 1 小时仍停在同一条最后观测上，记一条 `stream_stale` 告警（同一停更只记一次，有新数据后重新计）。补拉记录是 `stream_refetch_queued`、状态 `suppressed` 的事件，只用于去重。按本地日零点标时间的日值流（日汇总、PAI、呼吸频率、ODI）次日才到，预期间隔按 48 小时算。`hrv`（Zepp 手动单次 HRV 测量，SDNN）、`heart_rate/minute_endpoint`（佩戴上传密集文件的手表这里一直为空，分钟心率来自密集文件）、乳酸阈、VO₂max、运动负荷汇总、训练、训练明细和设备列表按需出现，不做新鲜度告警。

## 投递目标与告警

目标是晨报在窗口结束后 30 分钟内（默认 13:30）、晚报在次日 00:00 前被 PushPlus 受理（业务 `code=200`）。告警检查不在 worker 里：`vitalis-alerts.timer` 每 10 分钟启动一次 `vitalis alerts check`，独立进程，worker 冻住时照样运行。午夜后的整天检查前一天的晚报，保留原报告日期；告警进程在投递截止后才恢复，也会补记漏发。到目标仍未受理、PushPlus 明确拒绝、24 小时内分析失败（计算中途有新数据、任务自动重排的不算），或 worker 心跳超过 30 分钟未更新时各记一次事件，同一事件不重复。没有投递意图的漏发在 `GET /api/deliveries` 里显示为 `status=missed` 的记录，`failure_code` 为 `worker_not_running`、`snapshot_unavailable` 或 `delivery_not_created`。

数据流停更（`stream_stale`）和 Zepp 凭据失效（`credential_expired`）也写进同一张告警表。第二告警通道（邮件或 Hermes）用户决定暂不接入（2026-10-13，2026-10-14 再次确认）：事件只写入当前数据库，用 `GET /api/delivery-alerts` 查看，`external_channel` 为 `unconfigured`；`journalctl -u vitalis-alerts` 里每个新事件一行，只含原因类别和日期。

漏发按下面的 SOP 排查；发布和版本切换仍由[发布 SOP](deployment.md)负责。

### 日报、晨报和晚报缺失排查 SOP

1. **固定日期、类型和截止时刻**：用部署的 `VITALIS_TIMEZONE` 确定目标本地日，再判断是否到了该期发送时间。日报是可回看的分析快照，自动推送的是晨报和晚报；分别检查 `GET /api/reports/daily/state?day=YYYY-MM-DD`、`GET /api/deliveries` 和 `GET /api/delivery-alerts`，不要把尚未到点的晚报判为漏发。
2. **核对实际版本**：记录本地候选或 `origin/main` SHA、服务器仓库 SHA、发布验收记录，以及 API、worker、alerts 的 `ExecStart`、`WorkingDirectory`、`EnvironmentFiles`。对 API 和 worker 的实际进程读取允许公开的 `PYTHONPATH`，在各自服务环境下导入 `vitalis` 确认源码路径；核对 release 的源码与锁文件摘要。配置只记录时区、数据库后端和 PushPlus、反馈入口是否已配置，不打印完整环境、令牌或投递目标。
3. **沿链路定位**：先看告警原因、投递状态和 `failure_code`，再查 `doctor` 的心跳、目标日同步 attempt/chunk、分析任务和快照状态，最后看对应时段 worker 日志和 `GET /api/data-status`。心跳正常和 `/ready` 成功不能证明报告已生成或渠道已受理；数据库内部时间按 UTC 读取后转换到部署时区。

| 证据 | 判断与处理 |
| --- | --- |
| 同步未成功、需要认证或无当天事实 | 按来源状态修复认证或同步；保留缺失资格，等待 worker 按投递期限协调。 |
| 同步完成，分析失败、长时间运行或没有日报快照 | 查分析任务、租约、失败类别和内存；修复后按影响日期排分析，不清库、不清空整个队列。 |
| 日报快照存在，投递 `failed/render_failed` | 优先用合成数据走完整 `PushService` 渲染及发送前校验；同时覆盖生产启用的反馈链接和 HTML/Markdown，不能只验证 `render_report` 或不带链接的测试补发。确认最终正文摘要与运输预算包含链接。 |
| 已核验动作仍显示代码或名称未确认 | 分别核对运行版本的动作表、普通与流式解析、已存训练明细和报告快照；用合成流式响应复现。只补已核验且为空的名称，保留原始代码、组次、重量资格、获取时间和逐秒样本；经仓储写入并按受影响日期排分析任务，不让 renderer 临时查表，不改写旧快照。 |
| `accepted`，有供应商流水号 | PushPlus 已受理；继续查供应商最终状态。未配置查询凭据时只能确认受理，不能声称送达。 |
| `uncertain` 或响应丢失 | 保留原意图并在供应商侧核对，不重置次数、不新建意图盲重发。 |
| 无投递意图、`missed` 或 `deferred` | 按 `worker_not_running`、`snapshot_unavailable`、`delivery_not_created` 或延期原因查调度及资格；核对该期同步是否已存在。 |

4. **恢复与验收**：修复在 feature branch 先复现失败，再按发布 SOP 验证候选和切换所有服务。没有 schema 差异时保留现库。已过投递期限的历史记录和失败证据保留，历史日报仍可回看；历史补发需要明确的目标日期和授权，不能把旧训练建议当成今天的安排。仍在期限内的意图只通过现有协调规则恢复，不直接修改状态伪装成功。
5. **留存结果**：私密发布验收记录保存目标日期/类型、失败类别、最终 SHA、命令退出码、合成渠道验收和实际投递状态。下一个定时窗口核对是否受理；尚未发生的投递写为待验证。第二告警通道未配置时明确记录该限制，沿用现有用户选择。

2026-10-15 排查实例：10 月 14 日晚报和 15 日晨报的同步、分析与日报快照均成功，投递各三次 `render_failed`，没有供应商流水号；配置反馈入口后追加的 HTML 链接未通过标签校验，且最终正文摘要仍为追加前的值。服务器仓库、源码、三个服务和发布记录的 SHA 一致。此类故障应修复发送前完整链路，重启或重复同步不能解决。

### 周报没有收到时

周报只有在 worker 启动时 `VITALIS_WEEKLY_REPORT_ENABLED=true` 才注册每周一的定时任务，发送时刻由配置表中的时、分和 `VITALIS_TIMEZONE` 决定。分析会保存周报快照，但只有周报投递任务才创建对应意图；有快照、日报正常或 API 就绪，都不能证明自动周报已触发。

- 先核对实际 worker 的开关、时区和下次周报触发时间，以及 PushPlus 目标是否已配置、来源是否仍有授权；只记录非敏感配置和布尔状态，不打印令牌或目标。
- 到点后依次查 `trigger=weekly` 的同步记录、`delivery_period=weekly` 的分析任务、上一完整周的快照，以及 `period=weekly` 的投递意图；核对意图的报告结束日、状态、`last_error`、发送次数和供应商标识。按上面的漏发流程定位中断阶段，不能用一次手动补发代替原因调查。
- 当前周报没有跨 worker 重启的补触发，缺失意图的到点告警也未覆盖周报；空投递列表或没有告警不能证明正常。相关改进见[周报可靠性计划](plan/weekly-delivery-reliability.md)。

历史排查记录：2026-10-19 发现生产采用周报默认关闭配置，当天显式开启并核对调度。该次记录截止时尚未到发送时刻，因此只证明配置生效，不证明首次自动投递成功。后续是否正常须查实际同步、分析和投递记录，不能因为日期已经过去就补写“验收通过”；剩余改进与验收见[周报计划](plan/weekly-delivery-reliability.md)。

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

worker 把 `vitalis.*` 的 INFO 日志写到 stderr（systemd 下即 journal），APScheduler 只记警告。日志包含：晨报/晚报任务开始，同步结束（每个睡眠日分片的解析状态，`empty` 表示来源当时还没有睡眠），每个分析任务的开始、耗时和处理后的内存，每次投递的状态和原因，延期投递的处理结果，以及约每 10 分钟一行的内存（进程 RSS/swap、cgroup 用量与 `memory.high` 触发次数）。晨报探测和同步各记一行（`morning_probe queued`、`morning report sync queued: … reason=sleep_found|window_end`）。按时间段查看：`journalctl -u vitalis-worker --since 07:25 --until 13:30`。

worker 的内存上限写在 `deploy/systemd/vitalis-worker.service`（`MemoryHigh=500M`、`MemoryMax=640M`，超过后被 OOM 结束并自动重启）。分析日志行里的 `cgroup` 用量和 `high_events` 增长说明正在逼近上限。

## 日常分析路径与离峰任务

日常分析读入库时写好的日级特征（`daily_signal_features`）：HRV、血氧、压力和 Charge 这类密集样本流，只有目标日和前一天读原始样本，更早的日子每个流每天一行（合格值的中位数、最新值、样本数、覆盖分钟数），不再截断样本。特征和样本在同一个事务里写入；首次部署或时区变化后用 `vitalis db rebuild-features` 从已存样本重算（约 1 年数据 2 分钟）。每次分析在 `analysis_runs` 记录耗时 `compute_seconds` 和进程峰值内存 `peak_rss_mb`；耗时超过 30 秒或分析新增的峰值内存（峰值减开始时常驻）超过 150 MB 时，`budget_exceeded` 写明超出项，worker 日志记一行警告。生产库副本上单次分析约 7–8 秒、含解释器的进程峰值约 140–150 MB（旧路径约 26 秒、320 MB）。

影子模块不在日常路径计算，也不进报告：worker 每天 03:30 跑离峰批处理，算前一天的开放健康洞察（EWMA 准备度、TRIMP 负荷、多变量异常）、个人反应模型和四项系统质量指标，并记录当天的 0–100 准备度和身体信号预警（这两项已进入日常分析和晨报，批处理只留跟踪记录），各存一行 `shadow_results`。`vitalis shadow run --day D --days N` 补算或在历史上回放策略改动，`vitalis shadow report readiness|body_signal --user U --days 14` 输出对比（准备度对计票结论的一致率、对厂商 Readiness 的秩相关；预警日列表需要用户确认才能算误报率）。

## Zepp 网页登录与重新登录

API 只在本机监听，电脑和手机通过 ZeroTier 私有网络访问：`deploy/systemd/vitalis-private-proxy.socket` 只在 ZeroTier 地址的 80 端口监听（模板里的地址换成实际 ZeroTier 地址），由 `systemd-socket-proxyd` 转发到 `127.0.0.1:8000`；`VITALIS_PUBLIC_URL` 设为 `http://<ZeroTier 地址>`。ZeroTier 端到端加密，所以这条私有链路允许 HTTP；公网地址仍必须 HTTPS。`vitalis connect zepp --user U [--minutes 60] [--push]` 生成登录链接（在连着 ZeroTier 的电脑上打开；`--push` 另经 PushPlus 发一份），密码只经过自己的服务器一次、不保存。

最近一次同步返回需要重新认证时，worker 每小时先用保存的 `login_token` 换新的 `app_token`；换不了（没有 `login_token`、接口拒绝或返回别的账号）就经 PushPlus 发一个 24 小时有效的重新登录链接，每天最多一次，并记一条 `credential_expired` 告警。

## 报告反馈链接

配置了 `VITALIS_PUBLIC_URL` 时，晨报和晚报末尾带一个一次性反馈链接（36 小时有效）：今天感觉 1–5 分、训练 RPE、酸痛部位和行为标签（饮酒、晚餐晚、咖啡因、生病、出差）。链接只存令牌摘要，用一次即失效；只有点提交才写反馈，打开或不填都不算。提交后重新分析对应日期及受它影响的报告，须等待任务完成，不能把入队当作已经更新。

页面还会根据当天训练类型是否匹配主要安排显示完成提示，不让用户填写。当前措辞会把“未记录到匹配训练”写成“未完成”，也可能把“有同类训练”说成完整执行了安排；这两种结论都超出记录所能证明的范围，待修正见[反馈状态计划](plan/feedback-record-status.md)。手机提交链路的实际验证另见[反馈验收](plan/feedback-tags.md)。

## 训练明细补拉与原始响应回放

`vitalis sync details --user U` 在 worker 运行时逐个排“只拉明细”的手动同步（每次最多 4 条），直到没有缺明细的训练或某次没有进展，进度看 `GET /api/data-status` 的 `workout_details`。

解析器修正后：`vitalis replay source-journal --source-user U --target-user R --start A --end B --as-of T --allow-production` 用新解析器把加密原始响应回放进独立用户 R；`vitalis replay diff --source-user U --replay-user R --start A --end B` 逐表列出新增、删除和变化的事实；确认后 `vitalis replay apply … --confirm` 用回放结果替换 U 在该窗口（最多 14 天）的事实，被替换的行压缩保存在 `replay_applications`，`vitalis replay undo --application ID --confirm` 恢复。apply 和 undo 都会重算日级特征并排对应日期的重新分析。原始响应随数据库保存、不另设期限；回放用户和 `replay_applications` 在确认无误后按[部署](deployment.md)清理旧数据的方式删除，不长期保留。

每次写入心跳都会喂 systemd 看门狗（unit 设 `WatchdogSec=600`）：进程 10 分钟没有心跳（例如超过 `MemoryHigh` 后被内核节流到停住）会被中止并按 `Restart=on-failure` 重启，中止时 faulthandler 把所有线程的 Python 调用栈打进 journal；未完成的分析在租约过期后重跑。worker 漏掉 23:00 晚报定时任务时（冻结或在调度时刻之后才启动），启动时和之后每 5 分钟检查一次：配置了 PushPlus 投递且目标日期还没有晚报定时同步或投递记录时，为投递对象补建一次同步。跨午夜后仍按前一天的日期同步、分析和投递，在次日 00:30 的投递截止时刻停止；已延期的晚报也按同一窗口恢复。

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

PushPlus 的 HTTP 200 只表示请求完成；只有业务 `code=200` 且有有效 provider message id 才记录 `accepted`。记录 `accepted`、provider message id、最终 `delivered`/`failed`/`uncertain` 分开；投递不会写目标完成或主观反馈。周报/月报开关默认关闭，开启前应使用合成目标做一次受理合同验证；真实 PushPlus 受理已在 2026-10-10 用测试补发验证，Hermes 的接入与验收见[智能体集成](agents.md)。报告的排队、计算和投递耗时等产品指标用 `GET /api/product/metrics` 查看。

## 常见故障

### Zepp 名称或运动大类缺失

1. 先核对运行服务的 SHA、实际加载路径和目录版本；查看标准化记录的 `vendor_type_id`、`vendor_type_namespace`，力量逐组记录还要看 `source=lap_62`、`vendor_exercise_code`。不要把设备 ID 当云端类型，或仅在本地补表。
2. 按[Zepp 目录](zepp.md)核对 APK 三份运动资源和官方动作目录的全量覆盖、唯一性及摘要。未知代码保持未知，禁止根据训练习惯补名称。
3. 在已完成候选验收的服务环境下预览，默认只读、无网络、无通知：

   ```bash
   python -m vitalis.zepp_mapping_refresh --user U
   python -m vitalis.zepp_mapping_refresh --user U --apply
   ```

   以上 `python` 必须是当前 release 的锁定虚拟环境，并加载服务配置。先审查 dry-run 的更改数量、受影响 `activity_day`、未解析命名空间及目录外代码数量。apply 是离线维护操作：暂停 API、worker、定时器及所有其他数据库写入者，用已加载服务配置的 release 环境执行，提交后恢复全部服务并检查心跳，防止同步写入与历史更正互相覆盖。无 schema 变化时保留现库，不因显示名刷新创建数据库备份。旧的直接 SQL 力量刷新入口已经移除。
4. 默认刷新只改厂商参考映射字段，保留原始数值、单位、获取时间、逐秒样本、source coverage 和用户确认。旧记录缺少命名空间时，只接受已有识别来源或同用户加密 journal 中匹配的 `sport_history` 原始类型及入口；没有证据或证据冲突时跳过并计数。不会由 `vendor_source` 猜测命名空间。
5. apply 在同一事务内递增输入 revision、追加输入事件并创建 `catalog_refresh` 任务，影响范围含更正日及依赖它的已有分析日，不受普通同步近 7 天刷新上限约束。由 worker 完成重算；历史 last-good 快照保持可回看，不原地覆写。重复 dry-run 应没有可更新记录。
6. 验收自由训练云端 16、力量训练大类、完整目录的普通/流式解析、保存后的逐组名称、分析快照和 API 报告；再次核对服务 SHA、`/live`、`/ready`、doctor 和心跳。报告显示名更新本身不触发真实 PushPlus；补发需要用户已授权的明确投递动作。发布步骤统一按[发布 SOP](deployment.md)。

#### 力量单位与自重刷新

力量单位、自重规则更新时，在同一候选环境额外执行以下 dry run，核对计数后使用 `--apply`：

```bash
python -m vitalis.zepp_mapping_refresh --user U --strength-weights
python -m vitalis.zepp_mapping_refresh --user U --strength-weights --apply
```

这一步只从同用户、同来源模式、同训练与厂商来源的最新完整加密明细重读力量列，校验原始摘要并匹配组序号、动作代码、次数和原重量；不匹配、缺少原始记录或有用户确认的组保持原样并计数。刷新不重解析心率或改变获取时间、原始 journal、source coverage；原厂商重量及单位编码保留供审计。通过仓储写入后按受影响的 `activity_day` 创建分析任务并重建训练日，历史 last-good 快照保留。再次 dry run 应无待更新项；待分析任务完成后再核对最新晚报，不能把“已排队”当作报告已经更新。

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
