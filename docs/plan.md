# Vitalis 预发布整改计划

> 状态：Phase 0–3 已实现，本地分项发布检查已通过；服务器验收待明确发布授权
> 适用范围：报告产品、健康数据迭代、任务与快照、连接流程、文档、部署和 agent 协作
> 当前原则：预发布阶段允许破坏性更新，不保留旧版兼容链

## 0. 本轮已落地

- 已新增本计划、发布部署 SOP 和 Claude 入口；Codex/Claude 的完整规则只维护在根 `AGENTS.md`。
- 已删除旧 schema 一次性迁移工具、对应测试和打包引用；服务器换库改按备份、候选库、验收、切换流程执行。
- 已移除未使用的 repository 旧方法名、用户模型旧身份投影和连接验证旧 provider fallback，并补充回归测试。
- 已更新文档导航、打包清单、发布说明和文档架构测试。

以上内容是上轮文档和冗余清理的基线。本轮已完成 Phase 0–3：公开报告统一投影、来源/coverage/as_of、T+0/T+1/T+2/T+3 训练反应、个人模型和 FDR 关联、跑步/力量趋势、目标与反馈闭环、产品指标、原始输入清单和离线回放。本地分项检查、文档/生成物、客户端和打包验收已通过；服务器候选库部署仍需具备发布授权和实际服务器参数。

## 1. 目标

Vitalis 要从“能够同步和计算健康数据的模块化后端”收敛成一个用户每天能够读懂、能够持续修正、能够验证建议是否有效的健康分析工具。

最终闭环是：

```text
连接 → 验证 → 历史回填 → 首次分析 → 晨报 → 训练/活动
→ 晚报 → Hermes 显式反馈或纠错 → 影响范围重算
→ 下一版报告 → 周/月趋势验证
```

PushPlus 只负责单向推送已生成的结果，不能提问，也不能把已读、沉默或投递状态当作反馈。需要追问、确认动作、记录 RPE/疲劳/酸痛、确认建议完成时，使用 Hermes。

## 2. 公开报告契约

### 2.1 报告名称

| 类型 | 产品定义 | 目标日 | 默认渠道 |
| --- | --- | --- | --- |
| `morning` | 今天的恢复背景、训练安排和一个观察重点 | 当前日，依据昨夜睡眠和昨日训练 | PushPlus、Hermes |
| `daily` | 本地日的完整事实快照，可审计、可回看 | 本地日 `d` | API、Markdown、Hermes |
| `evening` | 面向阅读的当日复盘和明日重点 | 本地日 `d` | PushPlus |
| `weekly` | 完整周的睡眠、恢复、活动、训练和反馈趋势 | 目标日之前的完整周或明确标注的 rolling 窗口 | API、Markdown、可选 PushPlus |
| `monthly` | 28/60/90 天趋势、训练反应、个人关联和目标进度 | 目标月之前的完整月或明确标注的 rolling 窗口 | API、Markdown、可选 PushPlus |

`daily` 不得在 CLI、API 或示例生成器中偷偷映射成 `evening`。如果保留某个别名，必须在 API contract 中明确并禁止生成第二套语义。

### 2.2 统一公开投影

内部分析可以保存完整 facts、features、trends、events、decision 和 shadow 结果，但渠道只能消费统一的 `PublicReportView`/`ReportBlock` 投影。每个 block 至少包含：

```text
section_id
title
priority
status
facts
comparisons
interpretation
action
source
unit
observed_at
as_of
coverage
```

Markdown、HTML、API、PushPlus 和 Hermes 只负责不同排版，不得重新查库、重新计算趋势或各自决定健康资格。不要把所有 internal sections 直接改成公开；公共投影应按周期选择 3–5 个高价值区块，详细内部事实留给 API/Hermes。

### 2.3 晨报要求

晨报先回答“今天怎么安排”：

1. 标题、目标日、数据截至时间和当前状态；
2. 睡眠、HRV/RHR、可用恢复信号、昨日训练/活动四类关键卡片；
3. 2–3 条由数据支持的变化；
4. 主计划、可选计划和一个观察重点；
5. 缺口只标在对应卡片，不能输出整篇内部校验说明。

恢复状态必须能追溯到 HRV、近期睡眠、静息心率、训练负荷和覆盖情况。没有足够数据时生成事实型晨报，关闭没有证据的行动结论。

### 2.4 日报与晚报要求

日报保存完整事实：睡眠、活动、训练时间线、训练明细、恢复背景、反馈、数据覆盖、版本和修正链。

晚报按以下顺序展示：

1. 今日概览；
2. 活动：步数、距离、活动分钟、非训练能量、日内压力/心率；
3. 训练时间线；
4. 跑步或力量明细；
5. 恢复背景；
6. 2–3 条跨域变化；
7. 明日一到两条重点。

晚报不能被同一力量动作次数比较垄断。睡眠、HRV/RHR、活动、跑步配速/心率/区间、训练负荷、训练反应和主观反馈都可以贡献 finding，但必须通过各自 coverage gate。

### 2.5 周报与月报要求

周报和月报必须公开：

- 可用日数、期望日数和 coverage；
- 睡眠/HRV/RHR/活动/训练负荷趋势；
- 训练状态、VO₂max、PAI、乳酸阈值的变化；
- T+1/T+2/T+3 训练反应分布；
- 主观反馈和客观信号的关系；
- 个人关联的样本数、coverage、混杂比例和多重比较控制；
- 下一周期一个可验证的实验重点。

`open_health_period_summary` 当前是“目标日加周期覆盖下的影子信号”，不能把它误称为完整周/月健康结论。真正的周期结果必须从每日 bundle/series 聚合。

## 3. 数据和健康分析要求

### 3.1 信号注册表

为每个源字段建立 signal registry，记录：

```text
source_field
normalized_metric
unit
calendar_semantics
qualification
source/device
observed_at/fetched_at
coverage rule
visible in morning/evening/Hermes
enters decision: yes/no/shadow
missing/late behavior
```

优先把现有 parser 已经能够获取的数据公开：睡眠阶段和规律性、HRV/RHR、呼吸频率、皮肤温度、SpO₂/ODI、步数/距离/活动分钟、压力/密集心率、跑步配速/区间/漂移、力量组次重量、训练负荷、VO₂max、PAI、乳酸阈值和训练反应。

### 3.2 基线和质量

- 日常比较使用 28 天个人中位数和稳健离散度；
- 短期趋势使用最近 7 天与前 7 天；
- 长期趋势使用 28/60/90 天；
- 每个信号保存当前值、基线、偏差、方向、有效日数、期望日数、覆盖率、来源和 `as_of`；
- 使用 `AVAILABLE`、`PARTIAL`、`STALE`、`UNKNOWN`、`INSUFFICIENT`，不要只用一个全局 `SUFFICIENT`；
- 缺失不等于 0，未知训练日不等于休息日，未知动作不补名称；
- vendor readiness/Charge 单独标来源，不能替代 Vitalis 的解释性状态；
- Open Health 仍先保持 `shadow_only`，满足覆盖和用户确认条件后再逐步进入解释层。

### 3.3 训练反应

每个训练 session 记录剂量质量、T+1/T+2/T+3 窗口状态、逐指标观察、期望/已观察分母、重叠训练、混杂原因、confidence 和主观反馈。

必须区分：

- 尚未到达观察窗口；
- 窗口已到但数据缺失；
- 初次回到基线；
- 持续回到基线；
- 训练或活动混杂。

当前只汇总 T+1 的 PersonalModel 必须扩展到 T+2/T+3，并保留 RPE、体力疲劳、精神状态和肌肉酸痛的独立分布。

### 3.4 个人关联

个人关联必须明确 `calendar_day`、`sleep_day` 和 `activity_day`。对少量预注册假设运行 60/90 天分析，输出样本数、覆盖、混杂比例、系数、置信度和 Benjamini–Hochberg q 值。结果始终写成 observed association，不写成因果。

## 4. 数据迭代和快照

### 4.1 输入事件

同步、晚到源数据、Hermes 反馈、动作确认、用户资料变更、训练偏好变更都产生 `InputEvent`，至少包含：

```text
user_id
event_type
source
occurred_at
affected_dates
affected_streams
payload_ref
```

### 4.2 范围化分析任务

每个输入事件创建 `AnalysisInvalidation`，明确直接日期、影响域、依赖窗口和原因。典型范围：

| 输入 | 直接重算 | 额外窗口 |
| --- | --- | --- |
| 睡眠晚到 | 当日、晨报 | 7/28 天基线 |
| 新训练 | 当日、晚报 | T+1/T+3、7 天趋势 |
| RPE/酸痛 | 训练日 | 训练反应、个人模型 |
| 动作确认 | 训练日动作 | 力量趋势、关联分析 |
| 偏好/目标变化 | 新建议 | 目标相关周期投影 |

历史 snapshot 永远保留。报告读取应能返回 `current`、`stale`、`queued`、`running`、`failed` 或 `missing`，并提供 `last_good_snapshot`、`stale_since`、`job_id` 和 `next_action`。

### 4.3 原始数据回放

保留脱敏或加密的不可变 source record、payload hash、观察/获取时间、raw schema version 和 parser version。分析 run 保存 input manifest，使字段解析修正可以离线回放，不依赖重新请求 Zepp。

## 5. 连接和调度

连接状态机统一为：

```text
connected → credential_verified → backfill_requested → backfill_progress
→ bootstrap_analysis_queued → first_report_ready → daily_cadence
```

首次连接按源能力分批回填，目标 180 天；报告显示实际覆盖和暖机进度。OAuth、pairing、token import 的窗口和错误合同必须统一；手工 token 入口只能作为明确的高级入口。

晨报、晚报分别有 required signals、cutoff、允许延迟、partial fallback 和 `as_of`。21:30 晚报跨午夜时不能静默丢弃，应生成延迟/部分报告或按策略补发。同步、分析、投递队列要记录 queue latency、compute latency 和 delivery latency。

## 6. 任务分解

### Phase 0：契约和可用性

- [x] 统一 `daily`、`morning`、`evening` 的 CLI/API/示例语义。
- [x] 反馈、手动同步、动作确认、偏好修改和首连同步自动排队分析。
- [x] 将全局输入版本改成受影响日期/域的 invalidation。
- [x] 保留 last-good snapshot，增加 report state 和 failure code。
- [x] 修复 verified days、`queried_days` 和 `upstream_coverage_verified` wiring。
- [x] 生产环境禁止默认 Mock，报告显示 `source_mode`。

当前失效审计保存在持久 `AnalysisJob` 的输入 revision、实际影响日期、信号域和原因中；源事实修正覆盖直接日期、依赖这些输入的已保存目标以及合资格的当前日。worker 仍按一个目标日计算整套结果，不在接口层重算。不同投递周期保留独立意图，已消费任务继续作为旧快照不可重新发布的证据。历史重算不回退当前事件状态。

来源模式绑定到用户数据集并冻结在同步请求和分析快照中；`replay` 目前只是显式离线输入标签，原始响应 journal 和通用回放入口仍属于第 4.3 节的后续工作。数据库使用新 schema，未提供旧库迁移。

### Phase 1：公开报告投影

- [x] 建立 `PublicReportView`/`ReportBlock`。
- [x] 展示晨报睡眠/恢复事实和决策 drivers。
- [x] 展示晚报活动、训练时间线、跑步/力量明细和跨域 findings。
- [x] 周报/月报展示 coverage、趋势和影子分析范围。
- [x] 为每个 section 输出 `value/baseline/deviation/source/coverage/as_of`。

### Phase 2：健康数据迭代

- [x] 训练反应支持 T+0/T+1/T+2/T+3、剂量质量、逐指标 confidence 和持续恢复。
- [x] PersonalModel 使用完整窗口和主观反馈。
- [x] 个人关联明确日期语义、控制混杂并使用 FDR/q 值。
- [x] 加入跑步配速—心率—漂移、力量训练量和训练状态趋势。

### Phase 3：产品验证

- [x] 加入目标类型、目标值、目标日期、可用训练日和疼痛/伤病状态。
- [x] 记录报告 usefulness、数据纠错、建议完成和建议结果。
- [x] 建立覆盖、迟到、冲突、反馈率、报告延迟、采纳率和误报率指标。
- [x] 用回放 fixture 验证不同设备、缺失、部分覆盖、晚到和重复同步。

## 7. 必须通过的测试

- CLI/API/example 中 `daily` 与 `evening` 语义一致且不同；
- 新数据或反馈不会让历史报告 404；
- 所有写入口会创建影响范围正确的 analysis job；
- 首次同步完成会自动生成首份分析；
- Open Health load 在 42 天 verified coverage 下能进入可用/部分可用；
- 每个 signal 都能报告 freshness 和 target-day coverage；
- T+1/T+2/T+3 能区分尚未到达、缺失、混杂和持续恢复；
- Mock、real、replay 来源不会混淆；
- 晚报跨午夜、DST、队列拥堵仍有可见结果；
- Markdown、HTML、API、PushPlus 使用同一公开事实和分析；
- package/sdist 不包含数据库、凭据、旧迁移脚本或冗余兼容入口。

## 8. 文档和冗余清理

文档唯一主责如下：

| 主题 | 主责文档 |
| --- | --- |
| 项目入口 | `README.md` / `README.en.md` |
| Codex/Claude 仓库规则 | `AGENTS.md` / `CLAUDE.md` |
| 本地开发和检查 | `CONTRIBUTING.md` |
| 产品与数据整改 | `docs/plan.md` |
| 报告产品合同 | `docs/reports.md` |
| 模块和数据流 | `docs/architecture.md` |
| 字段、单位、时间和 coverage | `docs/data-contracts.md` |
| Zepp 连接和同步 | `docs/zepp.md` |
| API/Skill/Hermes 集成 | `docs/agents.md` |
| 快速开始 | `docs/quickstart.md` |
| 发布部署 | `docs/deployment.md` |
| 运行、备份和排障 | `docs/operations.md` |

不保留旧版 SYSTEM/API/ARCHITECTURE/GETTING_STARTED/RESEARCH_NOTES/SYSTEM_HISTORY/ZEPP_INTEGRATION 等重复入口。生成的报告样例只能由生成器维护；不要手改 payload、HTML 预览或 fragment。本次整改直接删除旧 schema 的一次性迁移链和对应测试；服务器仍按部署 SOP 备份旧库、创建新库并独立验收。

## 9. 本地合并和服务器部署门槛

本地必须先完成：

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python tools/check.py all --ci
uv run --locked --extra dev python tools/generate_report_examples.py --check
git diff --check
git status --short
```

然后提交 feature branch，审阅 diff，合并到 `main`，推送已验证的 `main` SHA。服务器部署严格按 [docs/deployment.md](deployment.md)：固定 SHA、拉取代码、停服务、备份数据库、用当前 schema 初始化候选库并测试、切换配置、重启 API/worker、检查 `/live`、`/ready`、`doctor`、worker heartbeat 和合成报告。破坏性数据库更新不得运行旧版兼容迁移：备份旧库，使用新库测试，验收成功后才删除旧库，并保留带 SHA 和时间戳的备份回滚。
