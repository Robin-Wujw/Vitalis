# Vitalis 开发系统

[English](SYSTEM.en.md)

## 1. 目的

本文档是 Vitalis 当前有效的执行契约。它只保留日常工作必须遵守的规则、已核验的当前状态和仍未完成的事项。

历史任务、已完成 TODO、会话记录与逐次验证结果归档在 `docs/SYSTEM_HISTORY.md`。它们不属于活跃上下文，也不能覆盖本文档。

## 2. 必须遵循的工作流程

1. **检查现状**：先查看 Git 状态、相关代码、现有文档和测试，不覆盖用户已有改动。
2. **形成计划**：非简单任务在改变状态前写出可执行计划和 TODO，并让用户了解范围。
3. **本地完成**：先在本地实现、运行针对性测试和完整验证。服务器只接收已经验证的提交，不作为调试环境。
4. **同步文档**：行为、API、配置、项目结构、平台支持、数据契约或测试数量变化时，更新对应 Markdown。
5. **部署验证**：服务器部署后只做健康检查、schema 检查和受控端到端验证；发现行为错误先回到本地修复和验证。
6. **交付**：每个逻辑完整任务要有测试、文档和可追溯提交。失败或跳过必须如实记录。

## 3. 完成规则

- `[ ]` 表示待处理、部分完成或尚未验证；`[x]` 只能表示实现、测试、文档和针对性验证都完成。
- 活跃 TODO 只保留未完成工作；完成项立即移入 `docs/SYSTEM_HISTORY.md` 或提交记录。
- 文档中的测试数量必须来自最近一次真实运行，不能复制历史数字。
- 纯文档任务至少验证 Markdown、链接和 `git diff --check`；行为改动还要运行针对性和完整测试。
- 用户已有改动必须保留；无关清理和重构不属于当前任务。

## 4. 数据与健康边界

- 缺失观测保持显式缺失，可产生 `INSUFFICIENT_DATA`；不得用零值、旧结果或模板内容填充。
- 设备流按来源、scope、设备和单位隔离；不得跨设备平均 HRV 或假设设备可互换。
- 设备、厂商和用户反馈是事实来源；Vitalis 只根据明确数据生成推断和建议。
- Open Health 输出是 shadow-only，不能改变训练决策。
- Hermes 只路由、解释和记录用户明确提供的反馈；不得重新计算指标、趋势、恢复或训练内容。
- 训练建议必须优先遵守疼痛/伤病、恢复不足和数据不足门控。

## 5. 数据库与部署边界

- 生产数据库 schema 必须与当前代码契约匹配；schema 不匹配时停止操作，不用旧库解释新代码。
- destructive database 操作前必须确认精确目标、数据价值、恢复方式和用户授权。
- 服务器数据库、密钥、Zepp 凭据和 PushPlus token 不得输出到日志、文档、提交或对话。
- 服务器运行的是已验证提交；本地先修复 parser、数据契约、同步、晨报和测试，再推送、部署和验证。
- PushPlus 训练处方晨报要求完整睡眠和已核验的前七天训练历史；当睡眠完整但运动来源尚未查全时，仍可发送睡眠、身体读数、按日期标注的活动及已记录训练事实，并在一处说明缺口，不包含训练处方。日期、凭据、睡眠完整性和每日去重门控仍有效；不得用过期或部分验证的训练历史生成今天的训练建议。

## 6. 跨平台约定

- 核心业务逻辑、数据契约和测试应在 Linux 服务器与 Windows 工作站之间共享。
- 操作系统差异通过小型平台适配层处理，不把平台判断散落到业务逻辑中。
- Windows 使用 PowerShell 5.1 兼容语法；Git Bash 命令使用 POSIX 语法。
- 平台差异必须单独验证和记录，不把 Linux 文件权限断言描述为 Windows 已通过。

## 7. 双语 Markdown 强制规则

- 仓库中的每一项 Markdown 新增、删除、重命名或语义更新，都必须在同一变更中同时更新中文和英文版本；不得先合并一种语言，也不得让译文落后于当前契约。
- 每对文件以无语言后缀的 `.md` 保存简体中文（zh-CN），以 `.en.md` 保存英文。唯一例外是 `docs/README.md`，它在同一文件中内联维护完整的中文和英文导航，不创建 `docs/README.en.md`。
- 每个成对文件都必须提供可见的双向语言切换链接：中文文件链接到英文文件，英文文件链接回中文文件。除该切换链接和内联文档中心外，本地 Markdown 链接必须留在当前语言中。
- 两种语言必须保持语义与结构对等。标题层级、复选框、表格、围栏代码块、链接以及日期、提交 hash、测试数量、版本、API 路径、命令、文件路径、schema/字段名和其他技术字面量必须对齐；翻译不得删减、压缩或重新解释内容。
- 许可证和第三方声明中的权威许可证正文不得翻译或改写。`THIRD_PARTY_NOTICES.md` 与 `THIRD_PARTY_NOTICES.en.md` 中的 MIT 正文必须保持逐字节一致（仅允许换行符规范化差异）。
- `skills/vitalis/SKILL.en.md`、`skills/vitalis/knowledge/evidence.en.md` 和 `skills/vitalis/workflows/*.en.md` 仅是英文阅读 sidecar，不是运行时入口。Skill frontmatter、工具路由和运行时工作流始终由无后缀中文文件定义；运行时代码不得加载 `.en.md` sidecar。
- 任何 Markdown 变更在交付前都必须运行 `tests/test_bilingual_markdown.py`、完整的本地链接/锚点检查、适用的完整测试套件以及 `git diff --check`。固定清单、双向切换、结构对等、语言内链接、Skill 路由和许可证不变量失败时，不得交付。

## 8. 文档职责

- `README.md` / `README.en.md`：产品定位、主要体验、信任边界、项目状态和文档入口。
- `docs/README.md`：唯一的内联中英双语文档中心和按受众导航。
- `docs/GETTING_STARTED.md` / `docs/GETTING_STARTED.en.md`：本地启动、部署、调度与验证。
- `docs/ZEPP_INTEGRATION.md` / `docs/ZEPP_INTEGRATION.en.md`：Zepp 配对、凭据生命周期、数据覆盖和设备边界。
- `docs/API.md` / `docs/API.en.md`：HTTP API 导读；完整接口参考以 OpenAPI 为准。
- `docs/ARCHITECTURE.md` / `docs/ARCHITECTURE.en.md`：系统边界、数据流、智能策略和契约。
- `docs/RESEARCH_NOTES.md` / `docs/RESEARCH_NOTES.en.md`：外部证据、研究限制和实现候选项。
- `docs/SYSTEM_HISTORY.md` / `docs/SYSTEM_HISTORY.en.md`：完整、对齐的历史工作和验证归档。
- `SYSTEM.md` / `SYSTEM.en.md`：当前执行契约和未完成事项。

## 9. 当前状态

日期：2026-09-24

- 2026-09-24 `35a9d93` 晨报改进已推送并部署：升级前备份校验、schema/身份审计、编译和三项服务健康检查通过；本地 Python 完整测试 736 项、Balance 2 Node 测试 6 项通过，服务器未运行完整 pytest。今日新版分析快照已明确授权生成，只读验收覆盖昨夜、身体信号、昨天活动与已记录训练、今天截至分析时活动，且无训练处方。新版真实 PushPlus 测试投递被自动模式权限拦截，未由本会话发送；用户此前收到的是旧版晨报。只读同步分区检查显示通用运动入口成功，另 12 个分类入口不可用，因此不能把已记录训练当作完整历史或据此生成训练剂量。
- 2026-09-23 本地复盘已封闭 `AUD-001` 的条件性跨用户内置投递风险；Windows 完整 Python 测试 705 项、双语检查 47 项及 Balance 2 Node 测试 6 项通过。用户确认完整 API 当前仅本机/内网可达；未核查线上服务、执行部署或发送真实消息。已跟踪源码结构不搬迁；未解决的事项见第 10 节，完成证据见 `docs/SYSTEM_HISTORY.md`。
- `2d98418` 已部署，Windows 与服务器 Linux 完整套件各通过 `674` 项，包含 `47` 项双语 Markdown 与本地链接/锚点检查。真实晚报 API 已验证有限动作名称显示；事实版晨报已由现有调度成功投递，并确认不含训练计划和当天去重标记存在。
- 本地当前契约为 `WorkoutDetail 5.1`（共享 `WORKOUT_DETAIL_SCHEMA_VERSION`）、Daily 14.0、Weekly 6.0、Monthly 3.0、MorningBriefing 4.0、Agent Context 6.0、Intelligence 14.0、StrengthAnalysis 2.0、Decision Policy 9.0。`GET /intelligence/evening-briefing`、`GET /intelligence/weekly-briefing` 和 `GET /intelligence/monthly-briefing` 返回 `ReportBriefing 1.0`，并与 HTML 使用相同的 `sections`。
- 本轮字段可用性检查不展开个人健康数值；可以描述一般能力，但不把个人健康数值、真实记录或未取得的 App 修正动作写入仓库，也不声称已取得真实 App 动作数据。
- 仅当 `training_family=strength` 且 lap 行恰为 62 列时，才从 0-based 21、22、28 读取有限的重量、次数和动作 code 观测；这些观测保留组序并进入独立的 `observed_sets`，不作为 `explicit_exercises`、肌群覆盖或训练处方依据。
- 周/月活动汇总按本地日期归属；同日不同训练热量相加，日汇总重复观测仍取中位数，不混合来源。报告保留指标单位和部分覆盖限制，未知热量单位不补为千卡。
- 活动缺失保持 `None`；旧 `ActivityRecord` 默认零没有观测证据时不是真实测量。热量泛称保持 `role=unspecified`，不当作总能耗，不合并重复入口或 workout 热量；没有摄入记录时不判断热量赤字。不同 source/scope/device/unit 始终分流。
- ProfileLoader 的训练历史覆盖范围为 56 个本地日；同步账本和分块仍受各自运行限制，范围不足时降级并保留限制，月报未知日不解释为休息日。Monthly renderer 是显式能力，不新增 cron；retrospective 路径继续保留安全边界。

以下为当前核验与仍保留的历史基线：

- 当前工作分支为 `fix/zepp-identity-ownership`；本轮文档只同步当前本地契约，不改变英文 sidecar 的运行时角色。
- 已核验晨报调度持续执行但返回 `stored_data_incomplete`：通用历史入口成功，而其余运动分类入口不可用，前七天训练历史未获完整证明。事实版不会把不可用解释为空记录，也不会修改历史覆盖标记。服务器备份和 schema 审计通过，未执行结构迁移。
- 已确认的 Zepp 语义继续有效：压力日汇总来自 `all_day_stress` 字段，曲线来自显式时间戳 `data` 数组；`Charge/stress_data` protobuf 和 `Charge/insight_data` 仍无可证明语义，继续不请求。
- 2026-09-07 部署验收只在当时明确授权的范围内执行备份、部署、服务重启和报告验收；晚报测试不写正式标记，事实版晨报成功后按正常每日标记去重。不复用当时的单次推送授权；2026-09-23 本地复盘没有服务器操作或真实推送。

## 10. 当前未完成事项

以下为 2026-09-23 本地代码审计发现的待办；级别按触发条件下的影响评估，不代表线上已发生事故。实际完整 API 仅本机/内网可达；未检查服务器密钥或个人健康值。已修复的条件性 P0 记录在 `docs/SYSTEM_HISTORY.md`，此处只保留未完成项。

- [ ] `AUD-002` **P1 / 条件性 P0：公网鉴权边界**。`vitalis/api/deps.py` 的 `X-User-Id` 只选身份；直接公开完整 API 会允许跨用户读取、修改及设备令牌签发。现已移除双语指南中的整站 Quick Tunnel 示例，但代码尚无身份认证。完成：在任何公网入口启用前设计并测试所有敏感路由的鉴权、用户绑定及浏览器配对兼容性。
- [ ] `AUD-003` **P1 / 部署**。`deploy/systemd/vitalis-worker.service` 依赖 `vitalis.service`，仓库提供的却是 `vitalis-api.service`；API 单元禁用内置调度，worker 仅执行到期分块，没有定时入队者。完成：统一服务名、明确调度所有权，并在全新安装上验证入队、重启和恢复。
- [ ] `AUD-004` **P1 / 投递恢复**。`vitalis/services/zepp_sync_coordinator.py` 先终结同步尝试，再分析/投递；异常仅写日志，终态不进入恢复队列。完成：持久化独立的下游任务及结果，覆盖终态提交后崩溃、发送失败和重复外部投递的恢复测试。
- [ ] `AUD-005` **P1 / 隐私日志**。`vitalis/services/push_service.py::_log_handler` 在 INFO 日志写入完整健康报告正文。完成：仅记录非敏感投递元数据，并验证日志不含个人报告内容。
- [ ] `AUD-006` **P1 / 浏览器重新配对**。`browser_extension/background.js` 新配对时保留旧 `browserLinkToken`，提交凭据优先走旧连接，可能更新错误账户或向新服务发送旧令牌。完成：明确切换时废止旧连接，增加可执行的重新配对/换服务测试。
- [ ] `AUD-007` **P1 / 内置晨报重试**。`vitalis/scheduler/jobs.py` 的内置晨报仅在 09:30 运行，睡眠不完整时当天不会自动重试；Hermes 每小时任务是另一入口。完成：确认内置调度的预期策略并测试延期后的重试与每日去重。
- [ ] `AUD-008` **P1 / 备份与保留**。仍需为持久同步账本建立生产备份/恢复演练和长期保留策略；`vitalis/services/daily_push.py` 的标记在数据库外。完成：验证隔离恢复、schema、调度标记和覆盖证据，再定义不破坏历史证明的清理规则。
- [ ] `AUD-009` **P1 / 训练历史覆盖**。真实运动分类接口不可用时前 7 天训练历史无法完整核实；晨报可展示已记录事实，但不生成训练剂量，且不得把不可用当作无训练。完成：取得可核验的厂商响应或替代完整来源，并验证覆盖门控仍有效。
- [ ] `AUD-010` **P2 / 手工导入页**。`vitalis/api/routes/connect.py` 的回退页面含双大括号 JavaScript 和固定的 `X-User-Id: '001'`。完成：使用真实页面交互验证解析、提交和身份选择；不影响已存在的优先配对入口。
- [ ] `AUD-011` **P2 / 健康与可观测性**。`vitalis/api/app.py` 在调度器启动失败时继续提供固定 `healthz=ok`，缺少队列停滞和 worker 就绪信号。完成：区分存活/就绪，覆盖数据库、调度所有权、积压及启动失败告警。
- [ ] `AUD-012` **P2 / 验证缺口**。仓库无自动 CI；`tests/conftest.py` 禁用调度并使用内存 SQLite，Node 设备测试和真实进程重启不在 Python 套件中。完成：建立可重复的跨平台 Python/Node 验证及进程级恢复测试。
- [ ] `AUD-013` **P2 / PostgreSQL 路径**。`.env.example` 默认 PostgreSQL 但本地步骤未提供预置；`vitalis/storage/schema_migration.py` 仅实现 SQLite 审计/迁移。完成：提供可启动的本地默认和 PostgreSQL 安装、升级、并发与恢复验证。
- [ ] `AUD-014` **P2 / 心率满页边界**。`vitalis/connectors/zepp/fetcher.py` 和 `vitalis/services/zepp_sync_coordinator.py` 把满页游标恰达独占窗口终点误判为 `partial`。完成：区分终点与无进展，并分别覆盖两条拉取路径的 1000 行边界回归。
- [ ] `AUD-015` **P2 / 指定时区按天同步**。`vitalis/services/zepp_sync_coordinator.py::_window` 在 `create_attempt(days=1, timezone_name=...)` 中先按应用时区建窗，再按请求时区发日期。完成：同一时区计算今日和窗口，并测试跨本地日期/DST 的分界。
- [ ] `AUD-016` **P3 / 回归隔离**。`tests/test_fetcher.py` 的混合秒/毫秒游标样例未覆盖毫秒样本更早的顺序，`tests/test_vitalis_skill.py` 的显式用户测试依赖环境中 `VITALIS_USER` 未设置。完成：补真实混合顺序/后继游标和显式环境隔离测试。
- [ ] `AUD-017` **P2 / 力量动作证据**。完善 Zepp 动作字典及 App 修正组来源；当前只有有限、已核验的显示名对照，未知 code 不泛化，`unit` 和未映射动作的 `name` 仍未知。完成：取得同设备同训练的脱敏对照样本并验证映射及来源优先级。
- [ ] `AUD-018` **P3 / 待用户反馈**。根据新版真实测试晚报的内容反馈再调整；额外测试推送需要新的明确授权。完成：得到反馈后增加对应报告断言，不自行发送真实消息。
- [ ] `AUD-019` **P2 / 旧依赖目录**。忽略的 `.codex_pydeps/` 含旧 Vitalis 安装快照；历史 `PYTHONPATH` 引用可能载入过期代码。完成：核对外部启动器均不再引用后再退役；当前不删除用户环境。
