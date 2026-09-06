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

日期：2026-09-06

- 本轮四报告数据层、报告路由和 Skill 工作流已完成集成验收并以 `41a603e` 部署到现有服务器。Windows 与 Linux 完整 Python 套件各 `591` 项通过，包含双语 Markdown、本地链接/锚点、schema 导出和报告链路测试；新版晚报接口返回 HTTP 200、`ReportBriefing 1.0` 和四个报告段落。
- 当前契约为 Daily 13.0、Weekly 6.0、Monthly 3.0、MorningBriefing 3.0、Agent Context 6.0、Intelligence 12.0、Decision Policy 9.0。新增的 `GET /intelligence/evening-briefing`、`GET /intelligence/weekly-briefing` 和 `GET /intelligence/monthly-briefing` 返回 `ReportBriefing 1.0`，并与 HTML 使用相同的 `sections`。
- 本轮字段可用性检查不展开个人健康数值；可以描述一般能力，但不把个人健康数值、真实记录或未取得的 App 修正动作写入仓库，也不声称已取得真实 App 动作数据。
- 周/月活动汇总按本地日期归属；同日不同训练热量相加，日汇总重复观测仍取中位数，不混合来源。报告保留指标单位和部分覆盖限制，未知热量单位不补为千卡。
- 活动缺失保持 `None`；旧 `ActivityRecord` 默认零没有观测证据时不是真实测量。热量泛称保持 `role=unspecified`，不当作总能耗，不合并重复入口或 workout 热量；没有摄入记录时不判断热量赤字。不同 source/scope/device/unit 始终分流。
- ProfileLoader 的训练历史覆盖范围为 56 个本地日；同步账本和分块仍受各自运行限制，范围不足时降级并保留限制，月报未知日不解释为休息日。Monthly renderer 是显式能力，不新增 cron；retrospective 路径继续保留安全边界。

以下为当前核验与仍保留的历史基线：

- 当前工作分支为 `fix/zepp-identity-ownership`；本轮文档只同步当前本地契约，不改变英文 sidecar 的运行时角色。
- Windows 与服务器 Linux 完整 Python 套件各通过 `591` 项；新版服务器 API 和同步 worker 均 active，`healthz=ok`，Zepp 身份映射和 SQLite schema 审计正常。部署前 SQLite 备份通过 `quick_check`，未执行结构迁移。
- 已确认的 Zepp 语义继续有效：压力日汇总来自 `all_day_stress` 字段，曲线来自显式时间戳 `data` 数组；`Charge/stress_data` protobuf 和 `Charge/insight_data` 仍无可证明语义，继续不请求。
- 使用用户于 2026-09-06 明确给出的新版部署和单次测试晚报授权完成投递：PushPlus 接受发送，测试工具返回 `test_sent`，`quality=SUFFICIENT`，`sync_status=incomplete`、`sync_degraded=true`，`scheduled_delivery_unchanged=true`。报告已注明同步不完整，使用符合日期要求的已保存数据；正式投递去重标记未改变。此授权不延伸到后续额外测试推送。

## 10. 当前未完成事项

- [ ] 根据用户对新版实际测试晚报的内容反馈进行后续调整；额外测试推送需要新的明确授权。
- [ ] 为持久同步账本建立生产备份/恢复演练与长期数据保留策略。
- [ ] 取得 Zepp App 修正力量动作组的可验证数据来源；当前云端空 `strengthSets` 不能证明 App 没有记录，也不能用识别评估字段补造动作或重量。
