# 当前架构

[文档导航](README.md) | [数据合同](data-contracts.md) | [运维](operations.md)

Vitalis 是单仓库 Python 服务，HTTP API 与调度 worker 分进程。客户端使用有范围的用户令牌调用 HTTP；健康事实、推断、用户反馈和行动建议各自保留来源和权限边界。项目仍在预发布阶段，本页描述当前代码，而不是[报告与文档改进方案](plans/Vitalis_Reports_Docs_Review.md)中的目标行为。

## 代码与运行边界

| 当前模块 | 责任 |
| --- | --- |
| [`src/vitalis/entrypoints/api/`](../src/vitalis/entrypoints/api/) | FastAPI 路由、请求鉴权、当前 `/api` 合同及 OpenAPI |
| [`src/vitalis/domain/`](../src/vitalis/domain/) | 规范用户、设备、观测与训练模型，不导入 HTTP 或 ORM；`domain/aggregation.py` 还保存纯范围聚合规则 |
| [`src/vitalis/application/aggregation.py`](../src/vitalis/application/aggregation.py) | `range_summary` 用例和 `RangeSummaryReader` 端口；只处理规范日记录，不导入存储适配器 |
| [`src/vitalis/application/health_query.py`](../src/vitalis/application/health_query.py) | 原始健康只读用例；按来源/范围/设备/单位聚合并返回稳定投影，不导入 SQL 或具体适配器 |
| [`src/vitalis/application/jobs.py`](../src/vitalis/application/jobs.py) | 分析任务用例与端口；用户范围、幂等和有界调度 |
| [`src/vitalis/application/sync.py`](../src/vitalis/application/sync.py) | 有界同步用例；通过 HealthConnector 和窄 UoW 端口写入规范日数据 |
| [`src/vitalis/application/delivery_policy.py`](../src/vitalis/application/delivery_policy.py) | 纯日报投递资格、日期/时效/覆盖门禁和 facts-only payload；由调用者传入 today、as-of 和时区 |
| [`src/vitalis/adapters/daily_push.py`](../src/vitalis/adapters/daily_push.py) | 直接手动分析组合、Zepp 同步轮询、PushPlus transport 和直接投递 `.sent` marker；不承载资格计算 |
| [`src/vitalis/adapters/persistence/analysis_jobs.py`](../src/vitalis/adapters/persistence/analysis_jobs.py) | SQLAlchemy 租约/认领适配器，由 [`bootstrap.py`](../src/vitalis/bootstrap.py) 组装 |
| [`src/vitalis/entrypoints/worker.py`](../src/vitalis/entrypoints/worker.py) 和 [`scheduler/jobs.py`](../src/vitalis/scheduler/jobs.py) | 唯一调度所有者；定时入队、到期同步和有界分析处理；调度投递只读取 adapter 并使用数据库 outbox，不使用文件 marker |
| [`src/vitalis/adapters/zepp/`](../src/vitalis/adapters/zepp/) | 厂商网络、协议识别、来源标识和规范化解析 |
| [`src/vitalis/adapters/persistence/`](../src/vitalis/adapters/persistence/) | SQLAlchemy 模型、当前 schema 检查、仓储和持久任务账本 |
| [`src/vitalis/adapters/credentials.py`](../src/vitalis/adapters/credentials.py) | 厂商凭据加密；密钥与数据库分开 |
| [`src/vitalis/application/sync_types.py`](../src/vitalis/application/sync_types.py) | 同步租约和分块结果的纯值对象 |
| [`src/vitalis/intelligence/`](../src/vitalis/intelligence/) | 个人基线、数据资格、状态、趋势、训练决策与不可变报告投影 |
| [`skills/vitalis/`](../skills/vitalis/) | 可离仓安装的薄 HTTP 产品 Skill；不实现健康计算 |

分析任务与范围聚合已由应用端口和 SQL 适配器分开；同步用例也通过 `HealthConnector` 与窄 UoW 端口隔离具体 Zepp/SQL 实现。`bootstrap.py` 只为显式合成数据 demo 组装该用例；demo 的 fetch 在数据库写事务之外执行，随后由一个显式提交的 UoW 写入用户和规范日数据。生产 HTTP 同步仍由 worker 拥有持久 attempt、分块和租约生命周期，不能把 demo 用例当作 API 调度入口。日报投递的资格与 payload 变换集中在无副作用的 `application/delivery_policy.py`；`adapters/daily_push.py` 只组合直接手动分析、Zepp 同步、PushPlus 和文件 marker。调度 worker 传入已保存快照并设置 `scheduled_delivery=True`，只使用通知 outbox 的 CAS 状态，不触碰文件 marker，也不重新同步或分析。当前健康查询、账号/同步任务/反馈和报告读取由路由调用 application 用例，具体 Zepp、SQL 与通知能力在 bootstrap 组装的 adapter；架构测试禁止 domain/application 与普通 API 路由直接导入具体存储或厂商适配器。`GET /api/health/range` 由路由调用范围聚合用例，具体 SQL 日记录读取在 `bootstrap.py` 组装。新代码不应再增加路由层分析或 Skill 侧重算。现有只读请求从数据库读取快照；显式分析请求入队，worker 执行确定性引擎并保存新的 `AnalysisRun` 和报告。当前快照查询核对运行记录中的资料 revision、用户输入 revision 和非秘密策略摘要；资料、反馈、训练偏好、有效来源事实与同步覆盖更新后旧结果保留供审计，但不再作为当前结果。幂等重放、无变化的写入和无资格的同步状态不推进输入 revision；规则/目录/时区变化由配置摘要拦截，分析最终事务再核对这些资格。直接厂商请求在同步流程中发生，分块结果及租约状态保存在数据库；不同流的空、不可用、失败和部分成功不合并为一个成功布尔值。

```text
Zepp 云端 → 规范观测与训练 → 分块账本与覆盖状态
                                           ↓
用户资料与反馈 → 数据资格 → 基线/趋势/决策 → AnalysisRun / 快照
                                           ↓
                         /api 报告、薄 Skill、通知展示
```

### A15 deterministic pure analysis

`vitalis.application.analysis.analyze(dataset, request, policy)` is the single
calculation entry point for Daily, calendar Weekly and calendar Monthly,
morning, training-response, personal-model, personal-association, and
shadow-only Open Health projections. The report engines also accept an explicit
rolling mode, which is labeled as near 7 days or near 28 days in report metadata. `AnalysisDataset` is prepared by the
application layer from detached normalized facts, feedback, recommendation
links, and prior active-event state; it contains no ORM session or network
client. `AnalysisRequest` supplies the user, target date, run ID, UTC as-of,
timezone, revisions, and policy digest. `AnalysisPolicy` supplies the explicit
zone, algorithm versions, evidence references, and shadow-rule switch.

The pure entry does not read the database, network, process settings, or current
clock, and does not create operational UUIDs. With the same detached dataset,
request, and policy it produces the same serialized result and same-run report
IDs/timestamps. Event lifecycle transitions are computed from the detached
prior state; the application publication transaction persists those proposed
transitions and observations, saves all snapshots, completes the run, and keeps
the existing input-revision/profile/config fencing and notification outbox
contract. Read-only reports continue to render saved snapshots rather than
recompute health facts.

The application `IntelligenceCommand`, `IntelligenceQuery`, and
`IntelligenceAction` receive their transaction factory from bootstrap. SQL
session/ORM mapping stays in the persistence intelligence adapter; the pure
analysis entry remains free of database, network, process-settings, and clock
access.

### A27 durable notification outbox

Scheduled nightly, morning, evening, and opt-in weekly/monthly syncs finish by writing one deterministic `AnalysisJob` in the same terminal sync transaction. The job carries an optional `morning`, `evening`, `weekly`, or `monthly` delivery period; manual analysis jobs carry no period. A claimed scheduled analysis saves its run, immutable snapshots, job success, and one keyed notification intent in the same final transaction. The intent identity combines the user, report kind, and logical period end: the local day for morning/evening, Sunday for a calendar week, or the last day of a calendar month. The worker marks delivery deferred when PushPlus is disabled or unconfigured. User-initiated analysis never creates a notification intent, but a successful run can refresh an existing unsent calendar intent or rearm an existing intent deferred because its snapshot was unavailable.

Each dispatcher pass drains analysis and notification work before its bounded sync batch, so that batch cannot invalidate a freshly analyzed report before the same pass prepares its delivery. Snapshot freshness still excludes results invalidated by concurrent input changes.

The worker claims notification intents with a database compare-and-swap lease, validates user ownership and the latest eligible saved Daily, Weekly, or Monthly snapshot for that intent's logical period, and conditionally retargets a still-running intent before sending when a newer run has completed. Morning/evening delivery retains the local-day expiry and facts-only/coverage gates; calendar delivery uses the saved calendar report without another health computation. Push transport runs outside the database transaction. Confirmed provider success is `succeeded`; a definite rejection is bounded-retry `failed`; timeout, process death, or any outcome that may have reached the provider is `uncertain` and is never automatically retried. Disabled or unconfigured delivery is `deferred`. Filesystem `.sent` markers remain only for the direct test/manual push helper and are not scheduled-delivery authority.

`GET /api/data-status` 是来源覆盖和任务结果，不等同于 `/live` 进程探针或 `/ready` schema 探针。`GET /api/reports/{kind}` 只读已保存的 Daily、晨间、晚间、日历周期 Weekly 和 Monthly 等现有报告；`GET /api/deliveries` 只返回当前用户的通知意图状态和已清洗的调度元数据；`POST /api/analysis-runs` 返回持久任务 ID，`GET /api/jobs/{job_id}` 查询其状态。其它原始指标、资料、事件和 Zepp 配对能力按同一个 `/api` 前缀保留，完整路径与字段以运行服务的 `/openapi.json` 为准。有效数据的日期窗口、训练事实与用户确认优先级见[数据合同](data-contracts.md)。

原始健康查询沿 `entrypoints -> application/health_query -> adapters/persistence/health_reader` 方向流动。`HealthReader` 端口只传递 detached 值对象；指标聚合的流式端口在上下文内持有只读 session、逐条脱离 ORM 后交给应用层，退出时关闭 session。`HealthQuery` 负责来源限定的时间序列分桶、状态投影和 workout 详情投影，路由不持有 ORM session，也不调用其它路由函数。指标查询采用半开 UTC 窗口，日聚合通过配置时区保留 DST 的本地日边界；raw/密集文件预算显式标记截断，聚合结果超预算明确拒绝。token status 只读取存储元数据，不解密厂商 secret 或验证网络。


## 身份、数据库与故障

Bearer 访问令牌在库中只保存摘要，并绑定用户、期限和 `read` / `analyze` / `sync` / `feedback` / `manage` 用途；浏览器配对码是独立凭据，不可拿来读健康报告。API 默认只监听回环地址。厂商凭据与数据库属于敏感资产，部署前需要设置分离的加密密钥、TLS 和网络访问控制，不能将本地 CORS 配置解释为授权。参见[安全说明](../SECURITY.md)。

新 SQLite 数据库由 `vitalis db init` 建立当前基线；非空旧库或版本、表、列、索引、唯一键、外键不符会在写入前拒绝，而不会自动迁移或清理。当前自动化运行在新临时库；PostgreSQL 的等价约束与恢复尚未完成 CI 证明，不列入已验证部署列表。分析任务在创建 `AnalysisRun` 后立即把 run 绑定到当前租约；租约被回收时，只将该任务绑定且仍为 RUNNING 的旧 run 标记为 `analysis_lease_reclaimed`，避免遗留 RUNNING 元数据污染报告。严格 exactly-once 仍不宣称为远程 PushPlus 传输语义。

## 研究边界

`open_health_insights` 始终 `shadow_only`，不改变当前训练决策。模型与设备选择规则的证据条目在 [`intelligence/evidence.py`](../src/vitalis/intelligence/evidence.py) 的 `EVIDENCE_REFS` 中；来源论文支持有限的测量解释，不构成任何特定型号的独立验证，也不构成医疗结论。Zepp 的已证实 payload 变体、APK 动作目录与未知字段处理见[Zepp](zepp.md)。
