# Vitalis 开发系统

## 1. 目的

本文档是 Vitalis 仓库当前有效的执行契约。它只保留日常工作必须遵守的规则、当前状态、未完成事项和最近验证结果。

2026-08-26 至 2026-09-01 的详细工作会话、历史 TODO 和逐次验证证据已归档到 `docs/SYSTEM_HISTORY.md`，不再放入每次工作都需要读取的活跃上下文。

## 2. 必须遵循的工作流程

每项仓库任务，包括功能、修复、重构、配置、研究和文档，都按以下顺序执行：

1. **检查现状**：先查看 Git 状态、相关代码、现有文档和测试，不覆盖用户已有改动。
2. **形成计划**：非简单任务在改变状态前写出可执行计划和 TODO，并让用户了解范围。
3. **按任务实现**：一次完成一个逻辑完整的任务。可以按步骤跟踪进度，但不得把一个本来完整的任务人为拆成多个交付任务。
4. **补充测试**：为改变的行为增加或更新针对性测试，测试风险和覆盖范围应与改动影响相匹配。
5. **同步文档**：行为、API、配置、项目结构、平台支持、数据契约或测试数量变化时，更新对应 Markdown。
6. **验证结果**：先运行针对性测试，再运行完整测试、编译、schema/OpenAPI 检查或其他适用验证；失败或跳过必须如实记录。
7. **提交并推送**：一个逻辑完整的任务通常对应一个清晰提交。不同的独立功能或修复分别提交并及时推送；不得把多个无关任务堆到一个最终提交，也不得为单一文档任务制造无意义的小提交。
8. **完成交付**：除非用户明确要求只保留本地，或推送受到外部阻塞，否则远程分支包含已验证实现之前，任务不算完成。

## 3. 完成规则

- `[ ]` 表示待处理、部分完成或尚未验证。
- `[x]` 表示实现、测试、文档和针对性验证均已完成。
- 每完成一个阶段就更新 TODO，不把所有进度更新推迟到最后。
- 每项功能或修复都必须具备可追溯的计划、测试、文档和提交记录。
- 推送失败时，交付项保持未完成，并记录准确阻塞原因，不泄露 token、Cookie、用户 ID、设备 ID 或健康数据。
- 用户已有改动必须保留；无关清理和重构不属于当前任务。
- 文档中的测试总数必须来自最近一次真实测试运行。
- 纯文档任务可以不运行应用测试，但必须运行 Markdown/diff 检查，并明确说明未运行应用测试。

## 4. 预生产仅新数据策略

Vitalis 仍处于预生产阶段。所有实现只面向当前契约和当前契约生效后新摄取的数据。

- 不保留已废弃端点、字段、schema、工具、类、代码路径或测试的兼容别名。
- 不默认增加旧数据读取器、版本分支、双读双写、迁移、回填或旧数据转换任务。
- schema 或规范化契约变化时，清理受影响数据并按当前契约重新摄取，不让新代码解释旧表示。
- 不从过时字段、端点名称、文本提示、不相关指标、默认值或其他设备流推断新的必填字段。
- 缺失观测保持显式缺失，并可产生 `INSUFFICIENT_DATA`；不得使用零值或虚构值填充。
- 只有用户明确指定需要保留的旧契约或数据集时，才允许例外处理兼容性。

## 5. Git 与交付约定

- 默认分支是 `main`；功能或修复优先在独立分支完成并验证。
- 不使用 `git reset --hard`、`git checkout --` 或其他方式撤销未知来源的用户修改。
- 提交信息必须清楚描述任务结果，不混入无关文件。
- 每个独立任务完成后及时推送，避免长期只保留本地提交。
- 合并 `main`、推送远端、创建 PR、部署或修改线上服务前，需要用户明确授权。
- 提交必须包含：

  ```text
  Co-Authored-By: Claude Code <noreply@anthropic.com>
  ```

## 6. 跨平台约定

- 核心业务逻辑、数据契约和测试应在 Linux 服务器与 Windows 工作站之间共享。
- 操作系统差异通过小型平台适配层处理，不把平台判断散落到业务逻辑中。
- 文件权限、服务管理、路径、进程信号和 shell 命令必须分别验证 Linux 与 Windows 行为。
- Windows 使用 PowerShell 5.1 兼容语法；Git Bash 命令使用 POSIX 语法。
- Unix 文件权限断言不能被描述为 Windows 上已经通过；平台差异必须单独记录。

## 7. 项目文档职责

- `README.md`：产品定位、主要体验、能力、信任边界、项目状态和技术文档入口。
- `docs/GETTING_STARTED.md`：安装、本地运行、部署、调度和验证命令。
- `docs/ZEPP_INTEGRATION.md`：Zepp 连接、凭证生命周期、规范化数据覆盖、训练心率和 Balance 2 桥接。
- `docs/API.md`：当前 HTTP API、身份要求和请求示例。
- `docs/ARCHITECTURE.md`：系统边界、数据流、智能策略和当前架构。
- `docs/RESEARCH_NOTES.md`：外部证据、研究结论、限制和后续实现候选项。
- `docs/SYSTEM_HISTORY.md`：历史工作会话、旧 TODO 和逐次验证记录，只用于追溯。
- `SYSTEM.md`：当前有效执行规则、当前状态和未完成事项。

## 8. 当前实现状态

日期：2026-09-01

- Zepp 浏览器配对、凭证续期、重新登录状态，以及带稳定 chunk、重试退避、续租 fencing、取消和重启恢复的按用户持久同步已经实现。
- 规范化健康数据、设备隔离、训练明细、确定性健康智能、跑步/力量分析、训练响应、个人模型、时间线和 Hermes 渲染边界已经实现。
- 当前 canonical workout 身份为 `(source, workout_id)`；指标 provenance 保留 `source`、`source_scope`、`device_id` 和 `unit`。
- 当前训练日汇总来自所有 canonical workout，并使用 `VITALIS_TIMEZONE` 的本地日期边界。
- 当前 schema 变更要求使用全新数据库/schema 并重新摄取供应商数据；不提供旧库在线迁移或兼容读取。
- Zepp 数据正确性修复提交 `e218bf7`（`Fix Zepp data contract propagation`）已推送到 `origin/fix/zepp-data-correctness`。

## 9. 最近验证结果

最近任务的最终验证：

- Zepp 持久同步可靠性完整测试套件：368 项全部通过；OpenAPI 45 条路径。
- Zepp 数据正确性审查后针对性测试：157 项通过。
- Windows daily push 权限契约针对性测试：16 项通过。
- Open Health Insights 完整测试套件：345 项全部通过。
- Linux 继续严格验证状态目录 `0700` 和标记文件 `0600`；Windows 验证代码请求了相同权限，并由 NTFS ACL 继承负责实际访问控制。
- Python 与 Skill 工具编译通过。
- OpenAPI 生成 43 条路径，包含 revision-aware UserProfile PATCH 和训练偏好 PATCH。
- 全部 13 个 Skill JSON schema 解析通过。
- 全新内存 SQLite schema 的 UserProfile 修订、AnalysisRun profile revision 和来源感知 workout 约束通过验证。
- `git diff --check` 通过。

## 10. 已知未完成事项

- [ ] 完成共享核心/平台适配层审计，明确 Linux 与 Windows 的服务、安全和文件权限验证矩阵。
- [ ] 为持久同步账本补充生产备份/恢复演练和长期数据保留策略；chunk ledger、重试/退避和进程重启恢复已实现。

## 11. 当前文档精简任务

日期：2026-09-01

目标：将过长的 `SYSTEM.md` 精简为中文活跃执行契约，把完整历史移到单独归档，并完成远程交付。

### TODO

- [x] 将完整长篇历史复制到 `docs/SYSTEM_HISTORY.md`，保留所有历史事实和验证记录。
- [x] 将根目录 `SYSTEM.md` 重写为简洁的中文执行契约。
- [x] 验证标题、链接、checkbox、Markdown 和 Git diff。

交付步骤：创建一个文档提交并推送当前分支，然后合并到 `main` 并推送 `origin/main`。最终交付状态以 Git 分支和提交记录为准，不为更新本清单额外制造提交。

### 验证日志

- 历史归档保留 32 个二级标题、128 个已完成 checkbox 和 3 个未完成 checkbox，与原提交完全一致，并增加了“当前规则以根目录 `SYSTEM.md` 为准”的说明。
- 根目录 `SYSTEM.md` 已从两千余行精简为中文活跃执行契约，并链接历史归档。
- 两个文件均未发现控制字符；`git diff --check` 通过。
- 本任务只修改 Markdown，不运行应用测试，也不改变应用代码、数据库、服务、部署或运行时配置。

## 12. Windows daily push 权限契约修复

日期：2026-09-01

目标：在不降低 Linux 文件权限要求的前提下，让 daily push 状态文件测试正确表达 Windows 的 NTFS ACL 语义。

### TODO

- [x] 保留 Linux 状态目录 `0700` 和标记文件 `0600` 的严格 mode 断言。
- [x] 在所有平台验证实现明确调用 `chmod(0700/0600)`。
- [x] Windows 不再使用无意义的 POSIX `st_mode` 数值判断 NTFS ACL。
- [x] 运行针对性测试和完整测试套件。

### 验证日志

- `tests/test_daily_push.py` 16 项测试通过。
- Windows 完整测试套件 309 项全部通过，产生 472 条既有 `datetime.utcnow()` 弃用警告。
- 本任务只修改测试和执行契约，不改变 daily push 运行时权限调用。

## 13. 开放健康洞察与用户档案

日期：2026-09-02

目标：基于现有规范化数据和用户确认的 `MALE/HRmax 190`，增加透明、可审计、shadow-only 的公开算法洞察，并让 Agent 对缺失档案字段进行明确追问和持久化。

### TODO

- [x] 新增 revision-aware UserProfile GET/PATCH、审计历史和 typed missing inputs。
- [x] 实现稳健 EWMA、nightly lnRMSSD readiness、多信号异常和睡眠基础洞察。
- [x] 实现用户确认 HRmax 驱动的 Banister TRIMP、ATL、CTL 和 TSB。
- [x] 接入 Daily 10.0、Weekly 4.0、Monthly 2.0、Agent Context 5.0 和晨晚报。
- [x] 修复 Hermes workout source 与训练偏好 PATCH 契约。
- [x] 验证算法、API、Skill schema、fresh schema 和完整测试套件。

### 安全边界

- 所有 Open Health 输出均为 `shadow_only=true`，Decision Policy 保持 7.0，现有恢复状态、动作、规则和 ActionPlan 不变。
- 不复刻 WHOOP 0–100/0–21 分数，不实现 Healthspan Age、Journal 因果结论、RR/加速度睡眠分期或 ACWR 伤病阈值。
- 当前 Zepp apptoken 云端不调用未经验证的 profile endpoint；用户确认值优先于设备/训练观测候选。
- 上游同步覆盖未由持久 chunk 账本验证时，训练负荷只能标记为 PARTIAL 下界估计。
- 当前真实数据库仍是旧 schema；本任务未连接、迁移、清理或启动真实数据库。真实启用需要另行授权的 fresh-schema 重建和重新摄取。

### 验证日志

- Open Health/档案/报告/Skill 相关回归通过，Windows UTF-8 完整套件 345 项全部通过。
- Python 与 Skill 工具编译通过；OpenAPI 43 条路径；13 个 Skill JSON schema 解析通过。
- fresh SQLite schema 验证了 UserProfile 修订表、AnalysisRun profile revision 和 `workout_metric_samples.source`。
- `THIRD_PARTY_NOTICES.md` 保留 OpenStrap analytics MIT 许可、固定上游 commit 和 Vitalis 修改边界。
- `git diff --check` 通过。

交付步骤：本任务使用一个提交推送 `feat/open-health-insights`；是否合并 `main` 由用户单独确认。

## 14. Zepp 持久同步可靠性

日期：2026-09-02

目标：让 API 配对、手动同步和定时同步共享可恢复的持久 attempt/chunk 账本，并在并发、失败、取消和进程重启后保持正确状态。

### TODO

- [x] 接入配对、手动 API、浏览器续期和调度器 attempt 创建/状态/取消流程。
- [x] 实现稳定 chunk、条件 claim/finalize、attempt/chunk 续租 fencing、重试退避和成功 chunk 隔离提交。
- [x] 修复全窗口统计/训练请求放大、dense 归档选择、设备清单刷新和 calendar-day 结果语义。
- [x] 将 dispatcher 收敛为有界公平批次，并由 FastAPI lifespan 统一启动/关闭以支持直接 uvicorn 重启恢复。
- [x] 恢复 nightly 分析与 morning/evening 分析推送终态动作，并统一浏览器 link 状态投影。
- [x] 补齐可靠性、调度、API 和 Windows UTF-8 契约测试并更新公开文档。
- [x] 运行完整测试、编译、OpenAPI 和 diff 验证。

### 安全与数据边界

- lease token/epoch 是内部 fencing 信息，不通过公开状态 API 返回；过期 worker 不能继续 claim、finalize 或覆盖 stream diagnostics。
- transient failure 保留已完成 chunk 并进入 `retry_wait`；明确 authentication rejection 才进入 `needs_reauth`。
- 同一 stream 混合 success/unavailable 是 `partial`，不会伪装为完整覆盖。
- 当前仍执行预生产 fresh-schema 策略；新增表/列不提供旧数据库在线迁移，升级前按本文第 4 节重建并重新摄取。

### 验证日志

- 同步 coordinator、账本、scheduler 和 API 针对性回归 72 项通过。
- Windows 完整测试套件 368 项全部通过；剩余 2521 条警告来自既有 `datetime.utcnow()`、SQLite adapter 和 pytest cache 权限提示。
- Python 与 Skill 工具 `compileall` 通过。
- OpenAPI 生成 45 条路径，包含 `POST /health/sync`、`GET /health/sync/{attempt_id}` 和 `POST /health/sync/{attempt_id}/cancel`。
- `git diff --check` 通过，仅保留 Windows 的 LF/CRLF 标准化提示。
