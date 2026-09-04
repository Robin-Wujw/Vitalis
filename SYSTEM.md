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
- PushPlus 晨报只在当前数据同步和分析满足契约时发送；不得用过期或部分验证的训练历史生成今天的训练建议。

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

日期：2026-09-04

- 当前工作分支为 `fix/zepp-identity-ownership`，以已部署的 `aef653c` 为基线；本次文档工作将全部 39 个项目 Markdown 收敛为 19 对中英文件和唯一的内联双语入口 `docs/README.md`。
- 变更后的完整 Python 套件通过 473 项；`tests/test_bilingual_markdown.py` 的 47 项双语契约测试和 Balance 2 的 6 项 Node 测试全部通过。Zepp 身份唯一性与迁移 hardening、中英文文档入口、SQLite 当前 schema 迁移和 `all_day_stress` 本地日时间序列均已验证。
- 同账号、同设备、日期匹配的 Zepp 数据与界面对照确认：压力日汇总来自 `all_day_stress` 字段，曲线来自其显式时间戳 `data` 数组。`Charge/stress_data` protobuf 和 `Charge/insight_data` 仍无可证明语义，继续不请求。
- 本地和服务器 SQLite schema/身份审计均 clean；真实 `zepp-sync-v4` 压力流 fetch/parse/write success，2026-09-03 本地日写入 234 点、范围 5-65。完整 attempt 因可选 capability unavailable 为 `partial`、failed chunk 为 0；确定性分析为 `SUFFICIENT` / `TRAIN_NORMAL`，Morning 与 Evening 投影均成功。
- 服务器已部署 `fix/zepp-identity-ownership@aef653c`，API/worker active、`healthz=ok`、错误日志为 0；Morning `PushPlus --test` 返回 `test_sent` 且未改变正式调度标记。

## 10. 当前未完成事项

- [ ] 为持久同步账本建立生产备份/恢复演练与长期数据保留策略。
