# Vitalis 未发布阶段重构：Coding Agent 最终执行任务书

> **用途：**交给 coding agent 实际整改 `Robin-Wujw/Vitalis`。
> **产品定位：**Vitalis 是供 Hermes 等 Agent 框架及其他客户端调用的健康与训练数据产品；不是 coding-agent 框架。
> **重构前提：**项目尚未完成、没有旧版本兼容义务，允许破坏性修改内部结构、当前 API、开发数据库和同仓客户端协议。
> **替代关系：**本文件完整替代《Vitalis 架构整改总纲》上一版。上一版的兼容窗口、历史数据库迁移、双读双写、旧客户端支持等要求不再适用。
> **编制日期：**2026-09-26。

**Goal：**建立一套只有一个当前实现、文档主线清楚、可独立部署、可被 Hermes 等框架调用、能够通过自动化验收的 Vitalis 项目。

**Architecture：**单仓库、模块化单体；Python 后端共享领域模型与应用用例；API 与 worker 分进程。厂商接入、存储、通知属于外部适配器；Hermes 等框架通过当前 HTTP 合同及薄工具调用产品，不参与核心算法实现。

**Tech Stack：**保留经本地源码核验的 Python、现有 HTTP/数据库技术和客户端技术；不借本轮重构换语言、换框架或引入分布式基础设施。确切依赖和命令由 T00 从仓库确认，T02 固化。

**Spec：**本文件第 1—7 节是目标设计和硬约束；第 8—10 节是实施任务、执行方法和验收。不要另外生成一份内容相同的设计总纲。

**For agentic workers：**逐任务执行并验证。有 Superpowers 的环境可使用 `executing-plans` 或 `subagent-driven-development`；没有这些工具也必须可以直接执行本任务书，不把安装某个开发 Agent 插件作为前置条件。

---

## 0. 执行范围、证据边界与授权

### 0.1 本次不是从完整源码得到的逐行审计

编制时能读取公开仓库主页和 `docs/ZEPP_INTEGRATION.md`；没有取得完整源码快照，没有运行仓库测试、构建或设备实测。公开目录可确认存在 `vitalis/`、`tests/`、`browser_extension/`、`zepp_os/balance2_bridge/`、`skills/vitalis/`、`docs/`、`deploy/systemd/`、`pyproject.toml`、`run.sh`、中英文 README/SYSTEM 等入口。[R1][R2]

下文的 `src/vitalis/...` 等路径是**指定的目标路径**，不是假称当前文件已经存在。T00 必须读取本地真实源码，建立旧路径到目标职责的映射；T00 是实际执行的第一步，不是要求用户先补一轮资料。

用户已经从 APK 获取一些信息，但本任务书没有拿到或验证其内容。执行者优先检查用户当前工作区提供的提取材料；找不到时仅将“新增映射核验”标为受阻，不阻塞其他重构，也不得编造映射。

### 0.2 授权范围

允许直接：

- 重命名、合并、替换和删除过时的源码、文档、内部接口与旧测试；有价值的能力和语义测试必须在新实现中有去处。
- 统一为一套新的当前 API、配置、数据库 schema 和上传协议，同步修改同仓所有消费者。
- 删除专门服务旧 Vitalis 版本的别名、兼容解析、弃用包装、迁移脚本和旧启动路径。
- 将数据库初始化链重置为一个新的初始基线；开发和测试使用新建数据库。

不等于授权：

- 删除用户现有真实数据库、APK 原件、提取成果、未提交文件、密钥或仓库外文件。
- `git reset --hard`、`git clean -fdx`、强推、清空远程历史、自动发布或修改真实外部账户。
- 绕过授权、提交凭据或把真实健康记录放入公共测试样本。

**允许重构代码，不等于自动授权清空磁盘资料。**重构本身不需要旧库迁移；不触碰已有数据也不构成兼容负担。

### 0.3 执行者如何处理缺失信息

1. 先从当前仓库、配置、测试与已提供材料中解决，不把可自行检查的问题交还用户。
2. 无需逐阶段再问“是否继续”，按本任务书依赖关系执行。
3. 对不影响产品边界的实现细节采用最简单的合理方案，在任务记录说明。
4. 外部凭据、设备权限、许可证授权或真实数据删除等无法自行决定的事项，只阻塞相关验收或发布，不阻塞无关任务。
5. 证据缺失就写明未验证；不得把 mock 测试称为 Zepp、浏览器或 Hermes 实机验收。

---

## 1. 已锁定的产品方向与不做事项

### 1.1 主线

```text
用户授权 / 设备上传 / 厂商同步
               ↓
可追溯的规范化健康、睡眠、训练与反馈数据
               ↓
数据资格与质量 → 个人基线 → 状态/趋势 → 训练决策
               ↓
统一的结构化分析结果与报告
               ↓
HTTP API / Hermes 等 Agent 框架 / 通知和其他客户端
```

核心原则：事实、系统推断、用户确认和行动建议分别表达；Agent 负责调用、解释与提交明确反馈，不自行重算健康事实或制造训练处方。这与当前 README 的产品方向一致。[R1]

### 1.2 必须区分的三个对象

| 对象 | 角色 | 应放内容 |
|---|---|---|
| 本任务书 | coding agent 的一次性整改任务 | 改什么、顺序、目标文件、验收、执行进度 |
| 根 `AGENTS.md` | 开发者/开发 Agent 的仓库工作指南 | 命令、代码边界、验证、安全和阅读导航 |
| `skills/vitalis/SKILL.md` | Hermes 等产品使用方的运行时说明 | 如何查询结果、触发分析、提交反馈、处理失败 |

**不得把本任务书复制到产品 Skill，不得把产品使用工作流写成仓库重构规则，不得为了 coding agent 创建产品内的 Planner/Executor/Reviewer 系统。**

### 1.3 不引入

微服务、多仓库拆分、通用插件市场、LLM 编排平台、复杂事件总线、强制 Redis/Kafka、通用多租户计费、为了未来连接器设计的十层抽象、独立发布的通用 Zepp SDK。

不新增 MCP 服务、第二套 RPC、另一套 CLI 业务后端来证明“支持 Agent”。先把当前 HTTP API 与产品 Skill做好。已有薄工具有用就重构复用；有具体需求以后才增加其他传输层。

### 1.4 对现有能力的处置规则

T00 将发现的能力标为：`保留并重构`、`研究性隔离`、`冗余删除`、`缺少证据`。

已实现且符合主线的同步、训练详情、反馈、日报/周期分析等能力不因“重构方便”被静默删掉。研究性算法不因目录重排自动升级成默认建议。公式、窗口、阈值发生行为改变，必须有单独理由和测试；本任务书不授权凭空发明新的健康算法。

旧错误行为不必保留；旧测试若仅断言过时接口形状，可替换。保护数据语义、幂等、归属和缺失状态的测试必须保留或以等效新测试替代。

---

## 2. 明确取消旧版兼容义务

### 2.1 本轮必须删除或不实现的东西

| 旧负担 | 本轮做法 |
|---|---|
| 旧 Python 导入路径/函数签名 | 所有调用改为新路径；删除 re-export 与 deprecated wrapper |
| 旧 API 别名和旧响应形状 | 改为单一当前合同；同仓调用方一起修改 |
| 旧配置名与旧启动参数 | 单一配置模型；未知旧参数返回明确错误，不静默回退 |
| 旧 schema 的逐版升级 | 新基线从空库初始化；不做旧库升级链 |
| 身份投影修复/历史账号迁移工具 | 当前身份模型直接满足约束；删除为旧库纠错的运行路径 |
| 新旧双读、双写和两套分析后端 | 只保留当前实现，不做灰度兼容切换 |
| 旧版 bridge/扩展上传合同 | 客户端和服务端同次更新；不设旧版本支持矩阵 |
| 新版仍兼容旧算法运行时 | 只运行当前算法；必要元信息用于解释与重算，不加载旧引擎 |
| 为旧版写的升级/降级手册 | 删除；保留当前安装、初始化、备份、恢复和排障 |

代码分成小提交是为了便于验证，不是要求新旧实现同时服务。

### 2.2 不能误删的两种能力

**外部数据适配不是旧 Vitalis 兼容。**Zepp 当前实际可能返回不同形状的载荷；不同设备的协议也可能不同。能够由现有样本证明仍然需要的解析分支应保留，并命名为具体 payload/协议变体，而不是凭 `legacy` 字样批量删掉。[R2]

**溯源标识不是旧版运行支持。**保存 `source_record_id`、解析器修订、目录摘要、分析构建标识等，只为了说明结果来自哪里；不意味着维护全部历史解析器或历史算法。优先用构建 SHA/内容摘要，避免给每层人工管理一套版本矩阵。

### 2.3 当前基线与开发数据

- 一个 schema 初始基线；启动检查当前 schema，发现旧/不匹配 schema 则明确拒绝写入并提示改用新数据库。
- 不在启动时自动清库，不静默转换未知库。
- 默认测试、demo、重构验收都创建新临时目录/数据库。
- 仅提供显式开发重置命令：例如 `vitalis db reset --database <开发库路径> --confirm-discard-local-data`。它不能默认指向实际用户库；测试必须验证未确认时拒绝。
- 若保留迁移工具，仅保留新基线及以后真正发生的变更；不补造尚不存在的迁移链。

---

## 3. 目标代码架构

### 3.1 目标目录

```text
Vitalis/
├── README.md
├── README.en.md                      # 简短英文入口，不再复制整套技术文档
├── AGENTS.md                         # 仓库开发规则
├── CONTRIBUTING.md                   # 人类与 coding agent 共用的开发指南
├── SECURITY.md
├── LICENSE                           # 保留作者已有授权；缺失时不得自作主张授权
├── THIRD_PARTY_NOTICES.md
├── pyproject.toml
├── <唯一的后端依赖锁文件>
├── .env.example
├── src/vitalis/
│   ├── __init__.py
│   ├── __main__.py
│   ├── bootstrap.py                  # 依赖组装
│   ├── config.py                     # 单一配置定义
│   ├── domain/
│   │   ├── identity.py
│   │   ├── observations.py
│   │   ├── workouts.py
│   │   ├── feedback.py
│   │   └── analysis/                 # 质量、基线、趋势、决策等实际存在的规则
│   ├── application/
│   │   ├── ports.py                  # 少量真正需要替换的边界
│   │   ├── sync.py
│   │   ├── analysis.py
│   │   ├── reports.py
│   │   └── feedback.py
│   ├── adapters/
│   │   ├── zepp/
│   │   │   ├── client.py
│   │   │   ├── parsers/
│   │   │   ├── catalog.py
│   │   │   └── data/strength_exercises.json
│   │   ├── persistence/
│   │   │   ├── models.py
│   │   │   ├── repositories.py
│   │   │   └── unit_of_work.py
│   │   ├── credentials.py
│   │   └── notifications.py
│   └── entrypoints/
│       ├── api/
│       │   ├── app.py
│       │   ├── auth.py
│       │   ├── schemas.py
│       │   └── routes/
│       ├── worker.py
│       └── cli.py
├── clients/
│   ├── browser_extension/
│   └── zepp_os/balance2_bridge/
├── skills/vitalis/
│   ├── SKILL.md                      # 产品运行时入口，非开发规则
│   ├── references/api.md             # 从当前合同生成的最小接口参考
│   └── scripts/vitalis_api.py         # 薄 HTTP 调用；复用已有等价实现
├── migrations/                       # 当前基线；沿用已有迁移工具的真实布局
├── tests/
│   ├── unit/
│   ├── contracts/
│   ├── integration/
│   ├── architecture/
│   ├── e2e/
│   └── fixtures/
├── tools/
│   ├── check.py
│   ├── check_docs.py
│   └── zepp/                         # 提取/校验研究材料的工具，不被生产 import
├── docs/
│   ├── README.md                     # 唯一导航
│   ├── quickstart.md
│   ├── architecture.md
│   ├── data-contracts.md
│   ├── zepp.md
│   ├── agents.md
│   ├── operations.md
│   ├── decisions/                    # 仅确实需要保存的设计决策
│   └── plans/rebuild.md              # 本次唯一执行任务书；完成后退出活跃文档
└── deploy/systemd/
```

目录可因真实文件规模做小范围拆分，但不得产生空壳层。`models.py` 真正过大时再按对象拆分；不要从第一天起为每个 DTO 建包。

同一功能只能有一个权威实现。搬入 `src/` 后删除旧根 `vitalis/` 的运行时代码，不留一套能“勉强继续工作”的影子模块。可在一次功能任务内同步移动路径和更新调用方；不要求为了兼容把移动留到最终阶段。

### 3.2 依赖规则

```text
entrypoints → application → domain
adapters → application 中的端口 / domain
bootstrap → 负责组装以上组件
```

- `domain` 不导入 HTTP 框架、ORM、Zepp 客户端、通知服务和框架 Agent SDK。
- `application` 不直接导入具体 `adapters`；通过少量端口访问外部能力。
- API、CLI、worker 不复制健康算法、厂商解析或用户修正规则。
- `adapters/zepp` 不在 import 时请求网络、启动调度或修改数据库。
- `bootstrap`/入口组装位置可以接触具体实现，普通路由不得绕过应用服务直接查厂商 JSON。
- `tools/`、`tests/`、`docs/` 不成为运行时 import 的依赖。
- 不为每个函数创建 `Interface/Service/Manager/Factory` 四件套。只抽供应商、事务/存储、投递、凭据、可控时钟等真实边界。

以架构测试执行这些规则，而不是仅在 Markdown 里声明。

### 3.3 运行时

- **API：**认证、授权、输入校验、查询当前结果、提交持久任务。
- **worker：**唯一负责周期调度、任务认领、超时恢复、同步、分析和投递。
- **CLI：**初始化、诊断、demo、显式本地操作；复用应用用例。
- 默认一个 worker，不能因多起 API 进程重复调度。
- 不保留另一种 API 内嵌生产调度模式。开发组合启动器如确实有用，只负责启动同样的 API/worker 子进程，不形成第二套生命周期。
- 沿用能通过测试的数据库任务账本；本轮不增外部队列。

---

## 4. 数据与业务合同

### 4.1 核心对象

| 对象 | 必须表达 | 不允许混入 |
|---|---|---|
| `User` / `SourceAccount` | 本地所有者、厂商账号、归属关系 | 以 `user_id` 参数代替授权 |
| `Observation` | 指标、时间、单位、来源、设备/范围、质量 | 默认补零、跨设备混合 |
| `Workout` / `WorkoutSet` | 来源训练身份、动作/次数/重量、观测来源 | 凭心率猜动作名、未知单位补 kg |
| `UserFeedback` / 修正 | 用户明确输入、目标记录、时间与修订 | 模型自行猜出的“用户确认” |
| `AnalysisRun` / 结果 | 输入摘要、as-of、规则修订、证据、限制 | 直接覆盖设备观测 |
| `SyncJob` / 尝试与覆盖 | 生命周期、租约、分块进度、失败类别 | 用一个成功布尔值掩盖半失败 |

专门对象保留自己的约束；不把睡眠区间、训练组、任务和时间序列全部塞进万能 JSON/EAV 表。

### 4.2 统一语义

- `reps` 在规范化模型和当前 API 中是整数或 `null`，不再跨层转字符串。
- `weight_value` 与 `weight_unit` 分开；重量单位未知不计算具有确定单位的训练容量。
- 真实 `observed_at` 与接收时间分开；同毫秒多条样本用 `sample_id` 或 `ordinal` 区分，不篡改测量时间伪装设备精度。
- 按来源、范围、设备、测量方法保留差异。设备未知保持未知。
- `0`、`null`、空集合、未支持、未成功查询是不同语义。
- 日窗口为显式时区下的半开区间，测试跨午夜和夏令时；厂商固定槽位协议由专门解析器处理。
- 规范训练、训练组、每日汇总等投影不重复相加；用户修正与厂商值分开存，当前有效值由一处规则决定。
- 幂等键根据来源记录身份/协议定义，不能只用“指标+时间”吞掉合法并发样本；可空设备字段下也必须验证数据库唯一性行为。

### 4.3 原始数据与重算：采用足够用的最小方案

记录来源、解析修订、目录摘要、来源记录键和必要的脱敏证据。保留最小可重解析载荷时，需要显式大小/保留期/权限；不要保存完整 Cookie、认证头、登录响应或签名下载地址。

**不强制建设完整事件溯源平台、永久保存所有原始 HTTP、或实现历史引擎回放。**

当前分析至少保存：生成时间、数据截至时间、输入修订摘要、配置摘要、当前构建/规则标识和结构化结果。生成新结果时建立新的 run 或明确标记覆盖，旧结果不得在没有标记的情况下被解释为新算法输出。缺少原始载荷时只承诺从规范化数据重算，不承诺完整重新解析。

输入摘要必须覆盖用户资料、反馈/修正和策略配置，不能只对设备数据做 hash。具体存储粒度以现有能力和验收需求为准，不为每条请求复制全量历史。

### 4.4 APK 动作目录

运行时目录唯一权威文件：`src/vitalis/adapters/zepp/data/strength_exercises.json`。

最小逻辑 schema：

```text
catalog_revision
entries[]:
  namespace
  vendor_code
  canonical_exercise_id
  labels: {zh-CN?, en?}
  verification: verified | provisional
  provenance:
    app_version?
    artifact_digest?
    extraction_reference?
    observation_reference?
```

规则：

- `namespace` 必须区分运动大类编号、动作目录编号、lap 字段编码和 Android 资源编号；整数相同不证明相同含义。
- 主键为已定义命名空间内的厂商编码；存在经证实的版本差异时再增加适用条件，不凭想象建立多版本目录引擎。
- `verified` 必须有可检查的依据；没有材料的项目保持 `provisional`，默认不进入权威名称映射。
- code 已有名称映射不等于实际动作已由用户确认；保持“目录已知”“自动/显式/回退观测”“用户确认”三个维度。
- 未知 code 返回原 code 和 `unmapped`，不找“最接近”的动作兜底。
- 不手工同时维护 JSON 和 Python 字典；需要代码时从唯一数据文件生成。
- 新文件必须进入 wheel/安装包，并有安装后加载测试。
- 完整 APK、反编译源码树、图像素材和含个人信息的抓包不进入发布物。第三方授权不足时标记发布阻塞，不擅自重新授权。

### 4.5 同步、分析与投递

同步使用“可重复执行、事实幂等写入”，不承诺跨网络绝对只执行一次。

- HTTP 请求在长事务之外；规范化写入、分块提交、游标推进协调在同一短事务中。
- 提交时原子校验租约代次/当前归属，过期 worker 不能写入。
- 相同周期的任务创建需去重；多个 worker 误起时也不能重复认领同一任务。
- 认证拒绝、网络临时失败、可选能力不可用、成功空数据、未知非空载荷分别建模。
- 删除/断开账号与任务写入资格关联，阻止在途任务将已删除数据写回来。
- 分析入口是纯计算：确定输入、as-of 和配置，不自行访问网络/数据库/系统当前时间。
- API、通知、Skill 读取同一结果；模板不再计算一遍指标。
- 报告提交与待投递记录采用同事务持久化；远程推送可能已成功但本地超时时，保留不确定性，不伪造 exactly-once。

---

## 5. 当前 API、Hermes 与其他框架

### 5.1 采用一套当前 API

本轮统一前缀为 `/api`。不是为了同时支持另一套 `/api/v1`；旧路由删除，同仓消费者同步修改。`/live` 和 `/ready` 是运行探针，不替代数据更新状态。

保留当前产品真正需要的全部操作。以下是整改必须统一的最小操作表；T00 发现的有效附加操作按同一规则收口，不静默删除。

| operation_id | 方法与路径 | 语义 |
|---|---|---|
| `get_data_status` | `GET /api/data-status` | 数据覆盖、近期同步结果、缺失原因 |
| `get_report` | `GET /api/reports/{kind}` | 查询已生成结果；参数指定时间范围/报告变体 |
| `create_analysis_run` | `POST /api/analysis-runs` | 提交分析任务，返回 job 标识 |
| `get_job` | `GET /api/jobs/{job_id}` | 查询授权用户的任务状态 |
| `create_sync_job` | `POST /api/sync-jobs` | 显式同步，不能被普通查询隐式触发 |
| `list_workouts` | `GET /api/workouts` | 查询训练摘要 |
| `get_workout` | `GET /api/workouts/{workout_id}` | 查询训练详情与来源 |
| `create_feedback` | `POST /api/feedback` | 记录明确的用户反馈/修正 |
| `ingest_bridge_batch` | `POST /api/bridge/batches` | 结算准确样本身份的上传批次 |

配对、账号撤销、健康原始指标查询等现有功能保持等价用例；T08 从 T00 清单核定并记录具体当前路径。不要由健康 Skill 暴露管理端凭据导入/撤销等高权限操作。

`kind` 支持当前实际实现的报告族和变体；T00/T07 保留其业务意义，不因为改 URL 擅自将固定周期改成自然月或合并早晚报告。

响应/请求 schema 由 HTTP schema 代码生成 OpenAPI；它是语法权威。`docs/data-contracts.md` 解释语义，不能手抄另一套字段类型。

错误具有稳定 `code`、安全 `message`、`retryable`、`request_id`。日期格式、时区、分页、空值语义全 API 一致。HTTP 状态表达真实失败，不能把所有错误都包成 200。

### 5.2 鉴权与权限

- token 绑定当前用户和用途，URL/body 中的 `user_id` 不扩大权限。
- 别人的 report、job、workout 和 source account 均不可访问。
- Agent 默认有读取权限；触发同步、分析、写反馈由独立 scope 控制。
- 仅用于验证的本地随机 token 存摘要；需要发送给厂商的 token 加密保存，密钥与数据库分开。
- 配对码短时、单次、原子消费，绑定发起者/用途；浏览器来源验证和速率限制需测试。
- 默认不在公网监听开发服务；外网部署需要文档化 HTTPS/访问控制。不能以“单人项目”为由无鉴权开放健康数据。

### 5.3 产品 Skill 必须是薄调用层

`skills/vitalis/SKILL.md` 只回答：何时使用、如何配置、先查什么、如何解释状态、哪些操作需要明确授权、什么情况下停止或报告缺失。

不得包含：数据库访问、Zepp token、核心计算公式、仓库重构计划、开发 Agent 角色分工、开发依赖安装、读取全仓的指令。

建议运行调用链：

```text
Hermes / 其他框架
      ↓
Vitalis Skill 或薄工具
      ↓ HTTP + 有范围的用户 token
Vitalis API
      ↓
应用用例 / 已存储的分析结果
```

产品接入不依赖 Hermes 内部对象。服务器不导入 Hermes SDK；支持其他框架时复用 HTTP 合同，而不是复制分析代码。

Hermes 官方 Skill 机制支持主 `SKILL.md` 及同目录附属文件，按需读取引用；因此 Vitalis Skill 应可以单独安装，不依赖仓库外相对路径。[R3][R4]

### 5.4 Skill 安装后的自包含性

- `SKILL.md` 引用的本地文件全部位于 `skills/vitalis/` 内；禁止 `../../docs`、`../../src` 和根 `AGENTS.md`。
- `references/api.md` 由当前 OpenAPI 提取与产品工具有关的子集；源头仍是 schema，不人工同步两份合同。
- `scripts/vitalis_api.py` 只处理 HTTP、JSON、超时、退出码和安全错误；不得导入后端 Python 包、数据库或开发环境。
- 使用服务地址与最小权限 token 环境变量；token 不通过命令行参数、stdout、日志或返回给模型。
- 对有认证的请求不自动跟随跨来源重定向，避免泄露授权头；不要拼接未验证的任意网址。
- Read 的短时网络失败可有限重试；写入仅在有持久幂等键时自动重试，否则返回不确定结果供查询，不重复提交。
- 断网、不足数据、需要重新登录时输出真实状态；不得虚构“今天恢复很好”等替代结论。
- 第三方文本和用户备注是数据，不能改变工具调用目标、权限或规则。

### 5.5 两种产品验收

1. **自动化合同验收：**将 Skill 文件夹单独复制到临时目录，移除仓库导入路径，使用 mock HTTP 服务测试 Read/Analyze/Act 和错误分支。
2. **框架 smoke test：**在用户已有并授权的 Hermes 环境里安装并完成一次真实调用；没有环境时明确 `BLOCKED: Hermes runtime not available`，不能声称已经实测。

前者必须在普通 CI 中通过；后者不强制要求 CI 下载一个完整 Agent 框架。

---

## 6. 文档整改：减少重复，建立明确主线

### 6.1 默认阅读路线

**产品使用者：**`README → docs/quickstart → docs/agents（按需）→ docs/operations（部署时）`。

**集成开发者：**`README → docs/architecture → OpenAPI → docs/data-contracts → docs/zepp（仅接入开发时）`。

**仓库开发者/coding agent：**`AGENTS → CONTRIBUTING → 当前任务所需的专题文档 → 对应源码和测试`。

读者不应为了运行产品先阅读协议逆向、历史迁移、过去几十个任务记录。

### 6.2 每份文档只有一个主责

| 文件 | 权威内容 | 不应该包含 |
|---|---|---|
| `README.md` | 产品是什么、当前状态、最短入口、能力边界 | 全部配置、全部算法、历史重构说明 |
| `README.en.md` | 简短英文介绍与导航 | 未同步的完整第二套手册 |
| `docs/README.md` | 按读者目的导航、主题归属表 | 第三份架构综述 |
| `docs/quickstart.md` | 空环境安装、demo、真实连接入口、第一份报告 | 开发内部类、旧库迁移 |
| `docs/architecture.md` | 当前模块、数据流、运行时和信任边界 | TODO 伪装成已实现、详列全部 API 字段 |
| `docs/data-contracts.md` | 数据来源、单位、时间、缺失、修正、分析输入资格 | 厂商每个原始 JSON 字段的重复列表 |
| `docs/zepp.md` | 当前验证的协议变体、字段、解析证据、限制 | 数据库历史迁移、服务器安装教程 |
| `docs/agents.md` | 框架中立的接入方式、权限、Hermes 安装与验收 | coding-agent 工作规则、重复的核心算法 |
| `docs/operations.md` | 当前配置、运行、诊断、当前基线备份恢复、数据清理 | 不再支持的老版本升级链 |
| `CONTRIBUTING.md` | 安装开发环境、命令、测试、贡献/样本脱敏流程 | 产品用户安装流程的复制品 |
| `AGENTS.md` | 简短开发约束、命令与定向阅读 | 产品使用 Skill、长期任务日志、历史故事 |
| `SECURITY.md` | 当前安全边界与漏洞报告方式 | 虚假的审计或合规认证 |
| `THIRD_PARTY_NOTICES.md` | 实际分发的第三方材料及许可依据 | 未核验的统一授权声明 |
| `docs/plans/rebuild.md` | 本次任务勾选与简短执行记录 | 产品长期能力说明 |

`docs/quickstart.md` 只引用配置定义/operations 的进阶设置。完整配置键说明由配置模型或其生成表维护；`.env.example` 给安全示例，不成为另一套互相矛盾的规格。

### 6.3 现有文档的处理

| 当前可见文件/类别 | 处置 |
|---|---|
| 根 `SYSTEM.md`、`SYSTEM.en.md` | 将仍有效且独有的内容合并到 `docs/architecture.md` / 数据合同；删除旧文件，不保留旧入口别名 |
| `docs/ARCHITECTURE.md`（若本地存在） | 合并为小写的唯一 `docs/architecture.md`；Windows 用临时文件名完成仅大小写改名 |
| `docs/ZEPP_INTEGRATION.md` | 操作步骤归 quickstart；当前协议归 zepp；身份语义归数据合同；旧库迁移说明删除 |
| 重复的 API/开始使用/部署说明 | 语法交 OpenAPI，操作归对应唯一指南；消除全文复制 |
| `SKILL.en.md` 等重复运行时 Skill（若存在） | 只保留一个可安装 `SKILL.md`；人类语言说明放 docs/agents，不生成多个同名动作入口 |
| 旧计划、复盘、DONE/FINAL/V2/V3 总纲 | 提取仍成立的决策；已过时正文删除。Git 保存历史，不另建 archive 垃圾堆 |
| 重复工具规则如 CLAUDE/GEMINI/Cursor（若存在） | 删除重复正文；确需工具入口则仅指向 AGENTS，不复制规则 |
| 客户端 README | 仅保留该客户端独有的构建、硬件安装与限制，公共服务操作链接到指南 |
| 无运行时需求的研究记录 | 不混入用户导航；仅保留能支撑解析规则的最小证据说明 |

删除前完成内容归属检查；不是按文件名批量清空所有历史知识。没有独有内容的直接删，有必要的知识先迁入唯一权威处。

### 6.4 当前事实与计划分离

- 活跃说明文档只讲经过代码/测试核验的当前系统。
- 本任务书存于 `docs/plans/rebuild.md`，它描述目标，不冒充当前实现。
- 每个任务完成时更新受影响说明，不等到最后才修所有文档。
- 整改完成后删除活跃任务书，Git 历史保留执行过程；确有长期取舍写一条简短 ADR。
- 不默认新建 ROADMAP、PROJECT_CONTEXT、MEMORY、STATUS、HANDOFF、FINAL_REPORT 六份同类文件。
- 多会话工作只更新任务书中的任务状态和“下一步”，不反复生成新总纲。

### 6.5 文档质量门槛

- 根 `AGENTS.md` 目标不超过 100 行；主 Skill 目标不超过 150 行。超出首先删除重复信息，而不是机械拆成十几个附录。
- 文档索引中每个主题只有一个 canonical owner；能够机械验证的是文件、链接、路径、生成物和入口数量，语义矛盾仍需人工/审查者核对。
- 本地链接和图片路径有效；不能出现指向已删除旧入口的链接。
- Markdown 示例里的命令必须能由当前代码执行；长时间或真实账号操作须标为手动，不伪装成离线 quickstart。
- 自包含 Skill 的检查范围包括本地引用、脚本和依赖。
- `.env.example`、配置模型、部署单元、指南中的键和入口一致。
- 中英文不再维持两套完整规格。主说明用中文；英文入口保持简短真实；许可证原文不擅改。
- 检查生成的 API 参考与当前 schema 一致，但不检查对旧版是否兼容。

### 6.6 根 AGENTS.md 应采用的内容模板

以下是结构模板。T12 必须替换为实际验证命令和文件，不把未验证命令抄成已通过事实。

```markdown
# Working on Vitalis

## Scope
Vitalis is a wearable-data and training-analysis product used through HTTP
and thin integrations such as the Vitalis Skill. This file is for repository
contributors and coding agents, not for runtime health-assistant behavior.

## Current stage
Pre-release. Breaking changes are allowed. Update all in-repository consumers
and remove superseded paths; do not add compatibility wrappers for old Vitalis.
Protect local user data, credentials, APK materials and unrelated working changes.

## Read only what is relevant
- Development and commands: CONTRIBUTING.md
- Current architecture: docs/architecture.md
- Data semantics: docs/data-contracts.md
- Zepp protocol: docs/zepp.md
- Product integrations: docs/agents.md
- Operations: docs/operations.md

## Boundaries
- entrypoints -> application -> domain; adapters implement external boundaries.
- domain does not import HTTP, ORM, vendor clients or Agent frameworks.
- Do not duplicate analysis in routes, reports, notifications or the Skill.
- Do not invent missing observations, units, exercise mappings or user feedback.

## Verification
- Run the affected tests first, then the relevant check target.
- python tools/check.py quick
- python tools/check.py all --ci
- Show commands and results; distinguish FAILED and BLOCKED from PASSED.
- Replace obsolete contract tests only with an explicit reason and new coverage.

## Documentation
Update the canonical document for a changed behavior, not a new parallel guide.
Runtime Skill instructions live only under skills/vitalis/.

## Safety
No secrets or personal health records in git, fixtures, logs or screenshots.
No destructive cleanup, forced git reset, remote publish or real-data reset
without explicit scope and authorization. Do not commit unrelated local changes.
```

---

## 7. 工程门槛与统一命令

### 7.1 一个检查入口

创建 `tools/check.py`，提供以下可运行目标。它调用显式配置的命令，不猜测目录、不静默安装依赖、不把缺少工具当成功。

| 命令 | 必须执行 |
|---|---|
| `python tools/check.py quick` | 当前 lint、类型/架构快检、单元与纯协议测试 |
| `python tools/check.py backend` | 后端单元/合同/数据库/API/同步集成测试 |
| `python tools/check.py clients` | 扩展、bridge、独立 Skill 的无设备测试及可自动化构建检查 |
| `python tools/check.py docs` | 文档链接/职责表、示例入口、生成参考与自包含性 |
| `python tools/check.py package` | 构建包并在干净环境安装，确认资源和命令可用 |
| `python tools/check.py all --ci` | 以上强制目标及离线端到端验收；不启用真实账号和通知 |

Python 内部优先通过 `sys.executable` 调子命令；shell 调用明确、支持失败码传递。每个目标打印名称、命令、退出状态；超时、缺失依赖、执行失败都返回非零。

代码测试可使用 `python -m pytest ...`。现有测试框架不同则在 T00 明确，优先复用；若无成形方案，默认 pytest。lint/类型工具各一套，不堆叠多个功能重复工具。

前端/设备工具遵循各自真实 manifest 和锁文件。不存在编译步骤的纯脚本项目可执行语法、测试和打包检查；不得造一个始终返回 0 的 build 命令。硬件部署和 Hermes 真机运行不放进离线 `all`，单独列手动结果。

### 7.2 支持范围要由验收决定

SQLite 是个人单机默认基线。若保留并宣传 PostgreSQL 支持，它必须有相同关键数据库约束和集成测试；不能只写“理论支持”。没有验证的支持不在已支持列表中，原有有效实现不因为清文档被无理由删除。

保留一种权威服务部署路径，优先将已有 systemd 路径修正确。没有明确需求不再同时维护 Compose、Kubernetes 和另一套安装器。

### 7.3 安全、发布与数据操作

- CI 不使用真实 Zepp 账号、健康数据库或推送收件人。
- API 授权、配对重放、令牌用途、失效 worker、异常载荷、归档解压边界、日志脱敏都有测试。
- 发布物检查无秘密、APK、反编译整树或真实健康数据。
- 当前基线的备份恢复仍有价值，但仅测试“当前版本备份 → 当前版本恢复”，不支持旧版数据库升级。
- 新库 `init → demo/导入 → 分析 → 读取 → 重启 → 恢复` 是最终闭环。
- LICENSE 缺失或第三方授权不明时，不自动挑一个许可证盖上去；代码整改继续，公共分发标为受阻。

### 7.4 审查重点

下列失败条件必须在指定任务有测试，不得留作口头注意事项：

| 失败条件 | 期望 | 负责任务 |
|---|---|---|
| 同毫秒多样本、空 device_id、重复上传 | 不丢合法样本，不重复累计 | T03/T04/T06/T09 |
| 旧 worker 与删除/撤销竞态 | 旧所有权无法提交 | T06/T08 |
| code 已映射但动作未确认 | 只增加名称信息，不提升实际执行可信度 | T05/T07 |
| Skill 离开仓库、错误 origin/认证重定向 | 独立工作；不泄露 token | T09 |
| 文档重命名、配置变化、包内数据遗漏 | 文档/构建检查失败，而不是用户运行时才发现 | T02/T10/T11/T12 |

---
## 8. 按依赖执行的整改任务

### 统一任务循环

除纯盘点/文档任务外，每项执行：**读当前实现 → 写目标行为测试 → 运行并确认失败原因 → 修改代码及全部受影响调用方 → 运行目标测试 → 更新唯一文档 → 记录结果并提交本任务范围**。

中间提交可以暂时打破尚未重写的旧接口测试，但必须在任务记录列出归属和解决任务，不能将红灯隐藏。当前任务新合同的测试必须通过；T13 时整个保留测试集和完整流水线必须通过。不得为了让旧快照继续成立而增加兼容层。

提交前检查工作区差异，只包含本任务文件。不自动 push/发布；已有用户未提交改动不能被覆盖、回退或混入提交。

### 任务依赖

```text
T00 → T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09 → T10 → T11 → T12 → T13
```

默认顺序执行。只有执行环境确实支持独立 agent、且文件和接口没有冲突时才并行；不要为了使用并行工具重新设计产品。

---

### T00 — 获取本地真实基线，并建立唯一执行记录

**目标：**执行者从完整本地仓库接手，确定真实文件和已有能力；不再把公开网页的局部理解当源码事实。

**文件：**创建 `docs/plans/rebuild.md`（即本任务书）；命令日志保存在被忽略的 `.work/rebuild/`，不再创建其他长期总纲。

**输入：**当前工作树、此任务书、已提供的 APK/提取材料。

**输出：**一份源文件→目标职责映射、能力处置表、工具/测试基线和阻塞项，写入本文件末尾执行记录。

- [ ] 运行 `git status --short`、`git rev-parse HEAD`、`git ls-files`；记录实际 SHA 和未提交改动，不能自动 stash/reset 用户修改。
- [ ] 阅读 `pyproject.toml`、锁文件、入口、配置、数据库模型、测试组织、客户端 manifest、Skill 及全部活跃文档。搜查真实 `AGENTS.md` 等指令文件，但不得用旧的“必须兼容”项目规则覆盖用户已确认的本轮目标。
- [ ] 列出 API 路由/operation、命令、任务生命周期、ORM 表、部署入口和公开配置。对每个关键行为定位至少一个实现文件和相关测试。
- [ ] 记录所有已有产品能力，标记保留/研究隔离/冗余删除/缺少证据；不把计划功能记成已实现。
- [ ] 在本地范围定位 APK 提取结果。只记录路径、类型、必要摘要和是否脱敏，不把大文件或内容自动添加进 Git。
- [ ] 用当前真实命令运行测试、构建与 lint；无依赖或网络时记为 BLOCKED，不捏造通过数量。
- [ ] 建立“旧路径→目标文件/职责”表；旧文档逐个标记合并目标或删除理由。禁止仅以“名字旧”为理由删除厂商解析分支。

**验收：**每个后续任务都能定位真实输入；工作区资料未损坏；只有一份执行记录。不要求旧代码先全部变绿才允许重构。

---

### T01 — 先清理文档入口与重复规则

**目标：**后续 coding agent 不再被互相矛盾的文档引导；只留下一个开发入口和一个产品 Skill 入口。

**修改/创建：**`README.md`、`README.en.md`、`AGENTS.md`、`CONTRIBUTING.md`、`docs/README.md` 及第 6 节指定指南。

**删除：**按 T00 清单确认过的重复 SYSTEM、旧计划、重复工具规则；保留唯一内容后再删。

**测试：**`tests/architecture/test_documentation_layout.py`。

- [ ] 写文档布局测试：唯一主入口、旧重复总纲不再存在、主题归属表无重复 canonical owner、Skill 与仓库规则没有相互混用。
- [ ] 按第 6 节把现有有效内容分配到唯一指南；只写当前已经核验的事实，目标架构暂留本任务书。
- [ ] 重写 README 为“定位→状态→主线→最短入口→文档导航”，删除大段协议和迁移详情。
- [ ] 删除旧版本迁移段落和兼容承诺，不把正文整体搬进 `archive/`。
- [ ] 根 AGENTS 采用短规则；保留真正需要的工具入口时只引用它。
- [ ] 建立 docs 索引及角色阅读路线；开发依赖只放 CONTRIBUTING，产品使用只放 quickstart。
- [ ] 运行 `python -m pytest tests/architecture/test_documentation_layout.py -q`，检查删除文件没有唯一知识遗失。

**验收：**开发者和产品使用者的入口分开；活跃文档没有两份完整系统说明；没有为了凑目录新增空白占位指南。

---

### T02 — 统一打包、配置与代码边界

**目标：**从安装包使用产品；不会因当前工作目录恰好在仓库里才可以运行。

**修改：**`pyproject.toml`、唯一后端锁文件、`.env.example`、入口和所有 import。

**目标：**`src/vitalis/`、`config.py`、`bootstrap.py`、`__main__.py`、`entrypoints/cli.py`、`tools/check.py`。

**测试：**`tests/architecture/test_import_boundaries.py`、`tests/integration/test_installed_package.py`、`tests/unit/test_config.py`。

**接口：**`Settings` 为唯一配置模型；`load_settings(environ: Mapping[str, str]) -> Settings`；CLI 的规范入口是安装后的 `vitalis`，`python -m vitalis` 调用同一 CLI。

- [ ] 写失败测试：从仓库外导入；禁止 domain 反向依赖；旧根包不应遮蔽安装包；无效配置明确拒绝。
- [ ] 移动 Python 包到 `src/vitalis`，按目标职责放置现有实现并更新全部内部调用；不保留旧根包/导入兼容桥。
- [ ] 保留实际使用的 HTTP 和持久化技术，统一元数据和依赖；若已有锁方案沿用，没有则选一种并冻结本轮依赖，不同时维护两个手工锁源。
- [ ] 配置统一从 Settings 读取；环境变量只定义一次。删除旧别名与模块中的零散 `getenv` 业务分支。
- [ ] 打通最小可用 CLI `--help`、配置诊断、包版本；诊断不能输出秘密。
- [ ] 实现第 7.1 节的检查目标；尚未满足的目标明确失败/受阻，不能先做无操作成功桩。
- [ ] 配置 wheel 中的数据资源规则；后续目录新增时测试包内资源。
- [ ] 运行该任务三个测试文件和当前可完成的 `quick`、`package` 子检查；记录未完成目标归属后续任务。

**验收：**安装包可独立导入；只有一个配置源、一套启动代码、一套当前导入路径；架构禁止规则实际会捕获违规。

---

### T03 — 统一领域类型与数据语义

**目标：**先让不同层对同一事实的含义一致，再实现存储和解析适配。

**目标文件：**`domain/identity.py`、`observations.py`、`workouts.py`、`feedback.py`、`domain/analysis/models.py`、`application/ports.py`（均在 `src/vitalis/` 下）。

**测试：**`tests/unit/test_observation_semantics.py`、`test_workout_semantics.py`、`test_feedback_precedence.py`、`test_time_windows.py`。

**输出类型：**`Observation`、`Workout`、`WorkoutSet`、`UserFeedback`、`AnalysisDataset`、`AnalysisRequest`、`AnalysisPolicy`、`AnalysisResult`。字段依据第 4 节及 T00 的有效能力确定，不为未知字段制造默认测量值。

**纯接口：**

```python
resolve_effective_workout(
    observed: Workout,
    feedback: Sequence[UserFeedback],
) -> Workout
```

返回有效视图，不原地覆盖来源观测；实际修订与确认来源应可以查询。

- [ ] 写断言：`reps=8` 仍是整数，未知是 `None`；重量无单位不能产生 kg 容量；零与缺失区分。
- [ ] 写断言：同一时间不同设备/范围不合并；未知设备保留未知；真实毫秒时间不随 ordinal 改变。
- [ ] 写断言：重复厂商同步不覆盖明确用户修正；取消修正后回到有证据的来源值。
- [ ] 写跨午夜和夏令时窗口测试；不把观测首尾时间当完整覆盖证明。
- [ ] 实现领域模型、验证和集中优先规则；删除跨层字符串 reps、默认未知=0/自重/kg 等路径。
- [ ] 把业务共同使用的类型收口；API DTO 和 ORM 可以独立，但转换必须显式，不在每层重复定义语义。
- [ ] 运行四组测试，更新 `docs/data-contracts.md`。

**验收：**一个字段从解析到持久化、分析和 API 保持同一业务含义；变更点由测试证明，不靠注释承诺。

---

### T04 — 重建当前数据库基线与原子存储

**目标：**当前 schema 直接符合模型，不再借旧投影或旧迁移链拼接身份。

**文件：**`adapters/persistence/models.py`、`repositories.py`、`unit_of_work.py`；新初始 schema；CLI 的 `db init` 和受保护的 `db reset`。

**测试：**`tests/integration/test_fresh_database.py`、`test_repository_idempotency.py`、`test_source_identity_constraints.py`、`test_transaction_rollback.py`。

**接口：**`UnitOfWork` 为应用层提供事务及 repository 访问；`commit()` 显式提交，异常退出回滚。领域对象不携带 ORM session。

- [ ] 从空临时库写测试：初始化当前 schema；重复初始化安全；未知 schema 拒绝写入且不改变旧库。
- [ ] 写数据库约束测试：厂商身份唯一归属、本地账号/source 关系唯一、跨用户读写隔离。
- [ ] 写幂等测试：重叠同步、空 device_id、同时间合法多个样本、不同 source 的相同 workout ID。
- [ ] 写训练开始时间修正测试：旧日与新日投影都正确，整日多场训练不被分页顺序覆盖。
- [ ] 建立 `User → SourceAccount → Credential` 的唯一身份关系；删除可独立写入的重复投影。
- [ ] 只保留新初始基线；删除旧 identity migration/兼容读代码及只为旧库服务的测试。
- [ ] 实现真实数据库唯一约束和事务边界；不能只靠 Python 先查后插。
- [ ] 验证开发 reset 没有明确确认时失败，且不能隐式选择真实用户库。
- [ ] 运行该任务测试；宣传 PostgreSQL 时同步运行该后端，不宣传时不声称已验证。

**验收：**空库可重建；当前数据完整性由数据库和测试保证；没有旧库升级要求，也没有自动清库副作用。

---

### T05 — 重构 Zepp 解析器与接入 APK 动作目录

**目标：**厂商网络、数据识别、纯解析、动作目录和诊断彼此清晰，业务层不接触原始 Zepp JSON。

**文件：**`adapters/zepp/client.py`、`parsers/`、`catalog.py`、`data/strength_exercises.json`；`tools/zepp/` 中最小提取/验证工具。

**测试：**`tests/contracts/test_zepp_payloads.py`、`test_strength_catalog.py`、`test_strength_sets.py`、`tests/integration/test_catalog_in_wheel.py`。

**纯接口：**

```python
parse_workout_detail(payload: Mapping[str, object], context: ParseContext) -> ParseResult
resolve_exercise(namespace: str, vendor_code: int) -> ExerciseResolution
```

`ParseResult` 明确区分 `recognized`、`empty`、`unrecognized`，含规范化对象与安全诊断；HTTP 不可用/认证/网络失败由客户端结果表示，不能伪造一个空解析结果。

- [ ] 复用并脱敏已有真实 fixtures；构造测试载荷明确标为 synthetic，不能把它当协议被实测验证的证据。
- [ ] 写正常、空、畸形、未知形状测试；写不同有效 Zepp 变体测试。保留当前实际需要的变体，不保留旧 Vitalis 内部形状。
- [ ] 写力量组测试：显式组优先、回退不重复叠加、未知 code 原样保留、单位未知不补、来源与确认状态分开。
- [ ] 读取用户现有 APK 材料，核对 code 命名空间及对应链；仅把有证据条目记为 verified。不以示例 `65` 为起点推测相邻编号。
- [ ] 将目录唯一维护为 JSON；实现 schema、重复键、空标签、证据字段检查；生成工具不覆盖手工核验状态。
- [ ] 将厂商字段翻译为领域对象限制在 adapter；删除 API/分析/Skill 里的 code-name 字典和原始字段判断。
- [ ] 加入安装包资源测试：在非仓库目录加载目录成功。
- [ ] 将验证过的协议和未确定限制写入 `docs/zepp.md`，不复制用户抓包/完整 APK。

**验收：**解析不需要网络/数据库；未知不会被报告成正常空；目录来源可审查；新增材料缺失时仅该部分标为 BLOCKED。

---

### T06 — 收口同步调度、租约与恢复

**目标：**一个生命周期、一套应用同步流程，不因 API/worker 数量变化重复执行或错误提交。

**文件：**`application/sync.py`、`application/ports.py`、`entrypoints/worker.py`、持久任务 repository。

**测试：**`tests/integration/test_sync_lifecycle.py`、`test_sync_fencing.py`、`test_sync_partial_results.py`、`test_worker_recovery.py`。

**接口：**`SyncCommand` 规定 source account、数据流、半开窗口和请求身份；`SyncResult` 返回每流覆盖及结果。`execute_sync(command, ports)` 复用 T04 的事务及 T05 客户端/解析器；worker 只是调度与调用入口。

- [ ] 写重复周期任务创建、并发认领、API 多实例不调度的测试。
- [ ] 写 worker A 租约失效、worker B 接管后 A 试图提交的竞态测试，必须拒绝 A 的数据/游标提交。
- [ ] 写分块成功后进程崩溃和恢复测试：已提交分块保留，重试不重复累计。
- [ ] 写网络失败不标 needs-login、认证拒绝阻断、成功空与不可用不同、未知非空失败的测试。
- [ ] 写撤销/删除 source account 与在途同步竞态测试，所有权失效后不能重新写入。
- [ ] 将调度、认领、过期恢复统一放 worker；删除 ASGI 启动时的生产调度和旧运行模式配置。
- [ ] HTTP 放事务外；提交时用数据库原子条件校验租约/账号状态并协调写入和进度。
- [ ] 为限流/临时错误做有界退避，禁止未知协议无限重试。
- [ ] 运行测试并用真实任务状态更新架构和运维指南。

**验收：**重启能恢复；租约失效不提交；API 无调度副作用；部分成功不会被另一条流的成功掩盖。

---

### T07 — 收口分析、结果存储与报告

**目标：**分析只实现一次，报告和产品 Agent 不另外计算事实。

**文件：**`domain/analysis/engine.py` 及具体规则；`application/analysis.py`、`reports.py`、`feedback.py`；分析结果 repository。

**测试：**`tests/unit/test_analysis_determinism.py`、`test_analysis_data_gates.py`、`tests/integration/test_report_consistency.py`、`test_analysis_invalidation.py`。

**核心接口：**

```python
analyze(
    dataset: AnalysisDataset,
    request: AnalysisRequest,
    policy: AnalysisPolicy,
) -> AnalysisResult
```

`request` 显式含 as-of/时区/窗口。应用层负责准备输入与保存结果，领域入口不读取当前时间、网络或数据库。

- [ ] 用已验证业务样本写确定性测试；时间由输入给定，相同输入与配置结果一致。
- [ ] 写数据不足、未知单位、不同 HRV 方法、设备冲突等拒绝/限制测试；沿用有证据规则，不新造生理阈值。
- [ ] 写“目录名称变已知但没有新动作确认”测试，分析不能凭此增强训练处方可信度。
- [ ] 写反馈、资料、数据修订变化导致结果重新计算或标记 stale 的测试；没有变更不反复全量计算多年数据。
- [ ] 整合已有 Daily/Weekly/Monthly 与早晚等真实报告能力，保留业务窗口含义。将 shadow/研究规则保持隔离。
- [ ] 将报告模板和通知改为渲染同一结构化结果；删除路由、工具、Skill 中重复算法。
- [ ] 保存必要输入/规则摘要与限制，不保留旧算法运行时，不搭完整事件溯源系统。
- [ ] 当前 API 暂由后续任务接入；先用应用服务验证同一 run 的不同展示事实一致。

**验收：**只有一个分析入口；结果能说明证据和限制；新结构不把“能计算”误称成“经过科学或医疗验证”。

---

### T08 — 统一当前 HTTP API 与权限边界

**目标：**当前 API 是唯一产品合同；所有旧路由、旧 DTO 和不必要管理暴露退出。

**文件：**`entrypoints/api/app.py`、`auth.py`、`schemas.py`、`routes/`；`adapters/credentials.py`；生成参考脚本 `tools/generate_api_reference.py`。

**测试：**`tests/contracts/test_current_api.py`、`tests/integration/test_api_authorization.py`、`test_pairing_lifecycle.py`、`test_api_idempotency.py`。

**输出：**第 5.1 节的 operation_id/当前路径；已实现附加功能的当前路由清单；由 schema 生成的 OpenAPI。

- [ ] 为第 5.1 节写合同测试：路径、状态、字段类型、null/empty、operation_id 唯一。
- [ ] 认证测试使用两个用户和多种用途 token；对 report/job/workout/source/feedback 的跨用户访问全部拒绝。
- [ ] 写配对原子单次消费、过期、错误 origin、重复绑定和凭据撤销测试。
- [ ] 写只读接口无同步/投递副作用；POST 任务在持久化成功后返回 `202` 和 job 标识。
- [ ] 写反馈/同步/分析的持久幂等测试：相同幂等键和相同内容重试返回原结果；相同键不同内容拒绝。
- [ ] 所有路由调用应用用例，直接删除旧路由/别名/DTO；没有旧 API 并存窗口。
- [ ] 明确 secret 的摘要/加密用途与日志脱敏；错误不暴露 token、SQL 或原始健康载荷。
- [ ] 完整核对 T00 API 能力清单，给每个旧功能指明新的当前 operation 或明确删除理由。
- [ ] 实现 `tools/generate_api_reference.py --check`：生成/比较 Skill 用到的最小 API 参考，不手写另一个接口事实源。

**验收：**服务只接受一套当前合同；授权由服务端执行；所有有效功能有落点；OpenAPI、代码和生成的 Skill 参考一致。

---

### T09 — 同步修改扩展、bridge 与产品 Skill

**目标：**全部同仓消费者使用当前 API；运行时集成不依赖开发仓库环境。

**文件：**`clients/browser_extension/`、`clients/zepp_os/balance2_bridge/`、`skills/vitalis/`；原 `browser_extension/` 和 `zepp_os/` 的旧运行目录删除。

**测试：**客户端原生测试；`tests/contracts/test_skill_bundle.py`、`test_skill_http_client.py`、`test_bridge_contract.py`。

**工具接口：**薄脚本提供 `status`、`report`、`analyze`、`job`、`feedback`，确有产品需要时提供显式 `sync`。输入通过 JSON/stdin 或明确参数传递，输出为 JSON；语义来自 T08 operation，不另建第二套计算。

**配置名：**客户端使用 `VITALIS_API_BASE_URL` 和 `VITALIS_ACCESS_TOKEN`；仅为客户端配置。迁移现有名字后删除旧别名，不把这两项误当服务端 Zepp 凭据。

- [ ] 先修改客户端测试，使用新路径/字段；旧版接口测试删除或替换，不保留 v1/v2 兼容矩阵。
- [ ] 更新所有 manifest、构建路径、测试、文档和部署引用；仅大小写文件移动需兼顾 Windows。
- [ ] bridge 以真实 sample ID/ordinal 结算；测试重复批次、同毫秒不同样本、部分接受、永久拒绝和暂时失败时本地不丢未确认样本。
- [ ] 浏览器扩展限制 origin/主机权限、配对用途和日志；网络故障与失效认证不得混淆。
- [ ] 重写产品 SKILL：触发、状态检查、读取/分析/反馈、授权、失败和限制。删除开发工作流、后端源码路径及公式。
- [ ] 实现/复用自包含薄 HTTP 脚本：固定允许的操作、超时、安全 JSON 错误、不输出 token；跨 origin 认证重定向拒绝。
- [ ] 写请求重试测试：反馈/分析有幂等键才安全重试；不得重复写用户反馈。
- [ ] 把整个 Skill 文件夹复制到临时目录，在不设置仓库 PYTHONPATH 的情况下用 mock 服务调用并断言三类操作。
- [ ] 在可用且授权的 Hermes 环境运行 smoke test；否则仅标这条 BLOCKED，不谎称已测试。

**验收：**Skill 可单独分发；三个客户端与同一次服务端构建一致；没有重复后端业务，也没有 coding-agent 架构混入产品。

---

### T10 — 当前部署、可重复安装与最小数据运维

**目标：**用户按当前文档能够安装、运行、排障；不再承担旧版升级负担。

**文件：**`deploy/systemd/`、规范 CLI、配置、通知适配器、`run.sh` 的处置、`docs/quickstart.md`、`docs/operations.md`。

**测试：**`tests/integration/test_runtime_startup.py`、`test_demo_lifecycle.py`、`test_current_backup_restore.py`、`test_delivery_retry.py`。

- [ ] 写新库离线 demo 测试：无需 Zepp 凭据，不调用外网，不发真实通知，完整产生可查询分析结果。
- [ ] 提供当前 `vitalis db init`、`vitalis serve`、`vitalis worker`、`vitalis doctor`、`vitalis demo`；具体参数写入 CLI 帮助并测试。
- [ ] `doctor` 检查配置、schema、worker 最近活动与必要资源，不打印秘密；API 可用不意味着同步数据新鲜。
- [ ] 将 systemd 指向规范入口；API 和 worker 两个服务共用配置但不共同调度。启动不下载最新依赖、不自动升级 schema。
- [ ] `run.sh` 若无独有价值则删除；需要便捷入口时只转发规范 CLI，不复制初始化、迁移或调度逻辑。
- [ ] 写当前基线备份恢复测试，在新的路径恢复，默认禁用外发；SQLite 使用能获得一致性备份的实现，不盲拷活动数据库。
- [ ] 通知采用已保存结果与持久投递状态，重试与不确定成功状态可查询；不以推送失败回滚已保存的分析。
- [ ] quickstart 所有示例在干净环境执行；真实登录/硬件步骤标为手动，不能阻塞离线 demo。
- [ ] 明确当前支持的数据库/部署环境；不新增未经验证的部署途径。

**验收：**从新库启动闭环；没有旧版本升级说明；启动可重复，恢复可验证，真实数据和秘密未被触碰。

---

### T11 — 把工程规则变成 CI 门槛

**目标：**重构后边界、合同、安装和文档不会靠个人记忆维持。

**文件：**`.github/workflows/`、`tools/check.py`、`tools/check_docs.py`、依赖锁/开发配置、`SECURITY.md`、第三方声明和 PR 模板（需要时）。

**测试：**`tests/architecture/test_check_runner.py`、`test_doc_checks.py`、`tests/integration/test_release_contents.py`。

- [ ] 为 check runner 写失败传播测试：子命令失败/缺失/超时不能返回成功；`--ci` 不自动跳过强制项。
- [ ] CI 使用 T07—T10 形成的当前单一合同，运行第 7.1 节全部强制目标。
- [ ] 架构检查能发现反向 import；文档检查能发现旧路径、缺失引用、生成参考漂移、Skill 逃出目录引用。
- [ ] 包构建后在仓库外安装，测试命令、模块、动作目录均可用；检查包内不含个人数据库、APK、秘密和测试敏感材料。
- [ ] 依赖和工具固定；不可信 PR 不接触生产秘密；工作流用最小权限。需要下载第三方动作时固定经过检查的版本/提交，不使用不受控脚本。
- [ ] LICENSE 保留原授权；缺失时记录未决授权，禁止替作者自动选择。第三方声明只列实际分发材料；语言重复版本收口但不改许可原文。
- [ ] 贡献与 PR 模板要求“行为变化、目标测试、文档更新”；删除“必须提供旧版兼容/迁移策略”固定栏。
- [ ] `python tools/check.py all --ci` 执行一次，结果写入任务记录；任何未完成项列出责任任务，不打勾。

**验收：**检查不是空脚本；文档和包内容受到 CI 保护；未决授权只影响分发声明，不伪装成法律审查完成。

---

### T12 — 文档最终收口与新读者测试

**目标：**最终文档描述真实完成的当前项目，而不是同时展示旧系统、目标系统和半年前计划。

**文件：**第 6 节全部 canonical 文档、AGENTS、产品 Skill、客户端必要 README；本任务书执行记录。

**测试：**`tests/architecture/test_documentation_layout.py`、`test_doc_checks.py`、独立 Skill 测试。

- [ ] 用 `git ls-files` 重新盘点文档与工具规则，和 T00 清单对照；每份旧文档都有合并/删除/保留理由。
- [ ] 删除根 SYSTEM、重复架构/接口说明、旧迁移指南、过期任务正文和不必要的框架规则副本；不建立归档堆替代删除。
- [ ] architecture 只写当前代码结构；README 的“已支持”能力必须有实现/测试，手动验证项明确区分。
- [ ] 确认 AGENTS 是开发指南，SKILL 是产品说明，docs/agents 是集成指南，三者没有复制同一大段正文。
- [ ] 从“首次使用”“新增字段”“接入 Hermes”“排查同步”四种角色检查导航；读者最多经索引与一个专题入口到达主责文档。
- [ ] 核对类型、环境变量、CLI、API、运行时调度归属在所有文档一致；不以文档行数检查代替语义核对。
- [ ] 执行 `python tools/check.py docs` 及 Skill 自包含测试，纠正全部旧路径与生成物漂移。
- [ ] 下一轮已无具体任务时，不新增 ROADMAP/STATUS/MEMORY 等替代性总纲；本次遗留阻塞只在任务书和最终交付中清楚列明。

**验收：**唯一文档主线可用；没有重复权威规则；没有“已做完”但源码尚不存在的功能说明。

---

### T13 — 全项目离线验收、审查和交付

**目标：**交付能运行的新当前基线，而不只是目录重排和一堆报告。

**测试：**`tests/e2e/test_fresh_install_flow.py`、`test_agent_product_flow.py`、`test_restart_recovery_flow.py`。

**主闭环：**

```text
新环境安装 → 创建空库 → 加载明确标为 demo 的脱敏/合成数据
→ 应用同步/导入 → 生成分析 → HTTP 读取
→ 独立 Skill 读取/请求分析/提交明确反馈
→ worker 完成任务 → 结果更新 → 重启仍可读取
```

- [ ] 写并运行上述离线端到端测试；不需要真实账号或真实推送。
- [ ] 运行 `python tools/check.py all --ci`，记录实际测试数、退出码和构建结果；不把之前某次通过当这次结果。
- [ ] 从工作区 diff 检查：是否只改了本任务范围，有无泄露、无授权删除、重复实现、空壳抽象和旧兼容分支。
- [ ] 核对 T00 产品能力表；每项保留能力有当前实现与测试，每项删除有理由，不可通过大面积删除功能换取绿色 CI。
- [ ] 核对第 9 节所有验收 ID，逐一记录 PASS/FAIL/BLOCKED/NOT-APPLICABLE 及依据。
- [ ] 有独立审查 agent 时进行全分支审查；没有时做自审并明确标注，不声称完成独立审计。
- [ ] 实际设备、Zepp 线上、Hermes 环境或第三方授权缺失，只能标相应项 BLOCKED；强制自动化失败则不能称项目整改完成。
- [ ] 完成任务记录并提交；全部完成后从活跃文档移除 `docs/plans/rebuild.md`，在 Git 历史保留这份任务书。没有完成则保留它作为唯一进行中计划。
- [ ] 移除已完成任务书后，再运行 `python tools/check.py docs` 和 `python tools/check.py all --ci`；测试不得强制要求临时计划文件存在，以最终工作树为验收证据。
- [ ] 最终汇报遵循第 10 节，不额外生成多份永久总结文件，不自动发布或推送。

**验收：**当前基线可安装、运行和验证；所有强制项实际通过，任何外部限制没有被隐藏。

---
## 9. 最终验收矩阵

PASS 必须有对应实际命令/结果或可检查差异；FAIL 代表实际不满足；BLOCKED 代表没有运行所需外部条件，不等于通过。NOT-APPLICABLE 仅可用于明确不提供的可选能力，并说明原因。

| ID | 检查项 | 强制性 | 最少证据 |
|---|---|---|---|
| A01 | 当前 API、导入、配置只保留一套 | 必须 | 路由清单、依赖检查、旧实现删除差异 |
| A02 | 无旧库迁移和旧客户端兼容链 | 必须 | 当前初始基线、清理差异、当前客户端测试 |
| A03 | 原始资料和未提交改动没有被破坏 | 必须 | T00 与最终工作区核对、操作记录 |
| A04 | src 包在仓库外安装可用 | 必须 | 新环境安装、CLI help、模块导入 |
| A05 | domain/application/adapters 依赖受约束 | 必须 | 架构测试通过及测试能捕获故意违规的证据 |
| A06 | 事实/推断/反馈、null/0、设备/方法正确区分 | 必须 | 领域合同测试 |
| A07 | 新数据库完整性、回滚、幂等正确 | 必须 | 空库与 repository 测试 |
| A08 | 旧/未知 schema 不被自动破坏 | 必须 | 启动拒绝及文件/数据未变断言 |
| A09 | Zepp 当前有效载荷变体受支持，未知可解释 | 必须 | 纯 parser 测试、fixture 来源分类 |
| A10 | 目录资产可加载、有证据状态、未知不猜 | 必须 | catalog schema/解析/安装测试 |
| A11 | 用户新增 APK 映射实际核验 | 材料可用时 | 提取材料摘要、对应链和验证结果；缺失标 BLOCKED |
| A12 | worker 唯一调度，过期租约不能提交 | 必须 | 多实例/竞态/恢复测试 |
| A13 | 认证、空数据、不可用、网络失败不混淆 | 必须 | 同步结果分类测试 |
| A14 | 重复、同毫秒、空设备、训练改日不丢不重 | 必须 | 解析/存储/上传/投影测试 |
| A15 | 分析可重复，模板/Skill 不重算事实 | 必须 | 固定输入测试、报告一致性测试 |
| A16 | 未确认动作不因目录更新变成已确认 | 必须 | 动作证据门槛测试 |
| A17 | 资料/反馈/数据变更正确失效旧结果 | 必须 | 输入摘要和 stale/重算测试 |
| A18 | 所有资源归属及 token 用途受服务端校验 | 必须 | 两用户/多 scope/撤销测试 |
| A19 | 在途任务不复活已删除账号数据 | 必须 | 删除/撤销与提交竞态测试 |
| A20 | 扩展、bridge、Skill 对齐当前合同 | 必须 | 客户端测试与生成 API 参考检查 |
| A21 | 产品 Skill 脱离仓库仍可工作 | 必须 | 独立临时目录的 mock HTTP 调用 |
| A22 | Hermes 实际环境调用 | 环境可用时 | 实际安装和调用记录；没有环境标 BLOCKED |
| A23 | Zepp 线上及手表/浏览器设备实测 | 有授权环境时 | 手动 smoke 结果，不能用 mock 代替 |
| A24 | 文档主题归属唯一，无失效导航 | 必须 | doc checker + 四类读者人工检查 |
| A25 | AGENTS / SKILL / 集成指南职责分开 | 必须 | 内容检查、自包含检查 |
| A26 | 无重复总纲、旧迁移手册和开发规则副本 | 必须 | 全文档盘点与删除/合并表 |
| A27 | 当前版备份恢复、重启与投递可验证 | 必须 | 新路径恢复、无真实外发的集成测试 |
| A28 | 所有宣传支持的数据库都经测试 | 必须 | 支持表与相应 CI 矩阵；未提供的不宣传 |
| A29 | 发布包/日志/fixtures 不含秘密与个人记录 | 必须 | 内容检查和脱敏测试 |
| A30 | 许可证与第三方分发依据明确 | 公开分发前必须 | 实际许可文件与材料来源；缺失不能自动授权 |
| A31 | `all --ci` 在最终工作树成功 | 必须 | 完整实际命令、退出码、测试/构建结果 |
| A32 | 有效产品能力未被静默删除 | 必须 | T00 能力处置表逐项闭环 |

**注意：**A02 不是删除所有包含 `v1`、`legacy` 或 `compat` 的文本。Zepp 外部 URL/当前设备协议本身可能使用这些名称；检查的是不必要的旧 Vitalis 实现和支持义务。

---

## 10. 执行记录与最后汇报

### 10.1 仅使用一个短执行记录

在执行仓库的 `docs/plans/rebuild.md` 末尾维护以下字段；命令完整日志可放 `.work/rebuild/`，不提交真实账户日志。

```text
Baseline commit:
Working tree notes:
Confirmed stack and commands:
Source-to-target mapping:
Capability disposition:
Documentation merge/delete mapping:

Task | State | Changed paths | Test command/result | Remaining blocker
T00  | ...
...
T13  | ...

Next task:
External-only blockers:
```

`State` 使用 `NOT_STARTED / IN_PROGRESS / PASSED / FAILED / BLOCKED`。状态应反映实际运行，不在生成计划时预先标绿。

不要把任务书拆成多个互相覆盖的 FINAL_V2/STATUS/HANDOFF。后续会话从同一份记录继续即可。

### 10.2 Coding agent 最终回复格式

按下面结构给项目所有者回复，内容必须来自实际工作：

1. **当前完成状态：**哪些强制项通过；是否仍存在阻塞，不能用“基本完成”掩盖强制失败。
2. **主要变更：**架构、数据/同步、API/客户端、文档四个方面；列关键当前入口。
3. **已删除内容：**旧兼容代码、旧迁移链、重复文档和旧规则；说明有价值知识的去处。
4. **执行证据：**最终 SHA/工作树、命令、退出码、测试数量、安装与离线闭环结果。
5. **未验证事项：**Zepp 实测、Hermes 环境、设备与授权/许可缺失等；与 mock/自动化验收分开。
6. **如何运行：**当前实际可用的最短安装/初始化/API/worker/demo/Skill 调用步骤。

只在上面确有实际证据时说“通过”或“完成”。不称完整安全审计，不称医疗有效性验证。

### 10.3 对本任务书本身的使用

这是一个**实施规格与任务计划**，不是已提交的代码补丁。收到它的 coding agent 应从 T00 开始修改仓库，不能只再生成一份整改建议然后结束。

执行过程中发现实际代码已满足某项要求，运行对应测试证明后复用，不为凑目标目录重复重写。发现与本任务书相悖的旧兼容限制，按用户本轮明确的 pre-release 目标替换。

最终成果应是：**一个清晰、可安装、可测试、由正常软件模块组成的 Vitalis 产品，外加能独立使用它的薄产品 Skill；而不是围绕开发 Agent 构造一套新的软件平台。**

---

## 11. 资料与依据

下列引用只支持可公开核验的项目背景和 Hermes Skill 机制；目录、接口、任务、命令入口和验收矩阵是本任务书指定的目标方案，不表示它们已存在于当前仓库。

- **[R0] 用户明确约束：**项目尚未完成，可破坏性重构，不需要旧版兼容；强化文档主线；任务书交 coding agent 执行；产品供 Hermes 等框架使用。此约束优先于上一版整改总纲中的兼容要求。
- **[R1] Vitalis 公开仓库与 README：**`https://github.com/Robin-Wujw/Vitalis`。本轮可读取，用于确认产品定位和顶层目录，不等于取得完整源码。
- **[R2] Vitalis 当前公开 Zepp 集成文档：**`https://github.com/Robin-Wujw/Vitalis/blob/main/docs/ZEPP_INTEGRATION.md`。本轮可读取，包含当前协议、操作及旧数据库迁移等混合内容。
- **[R3] Hermes 官方 Working with Skills：**`https://hermes-agent.nousresearch.com/docs/guides/work-with-skills`。说明 Skill 主文档、附属文件及按需加载。
- **[R4] Hermes 官方 Skills System：**`https://hermes-agent.nousresearch.com/docs/user-guide/features/skills`。说明 Skill 目录和附属文件的安装/分发。

核验日期：2026-09-26；最终离线回归复核：2026-09-27。执行者以其实际工作区 SHA 为实施基线，外部框架命令以执行时的官方文档和实际安装版本核验，不将此文档的访问日期当项目 commit 日期。

---

## 执行记录（持续更新）

- Baseline commit: `60517a4c0de26035da99134b927e06ec8db8a037`；`origin/main` 与当前工作分支远端头均为此 SHA（2026-09-26 核验）。
- Working tree notes: 初始仅五个未跟踪用户资料：`docs/START_HERE_For_Coding_Agent.md`、本任务书原件、`docs/*.apkm` 和两张 JPG；原件及真实数据库不在改动范围。
- Confirmed stack and commands: Python 3.13.14 / FastAPI / SQLAlchemy / SQLite、PostgreSQL 声明支持；Zepp OS bridge Node 24 / npm 11。`.venv/Scripts/python.exe -m pytest -q -x --basetemp=D:/MyCodes/Vitalis/.pytest-rebuild-60517a4`：68 passed, 1 failed（`test_bilingual_markdown.py` 的固定清单不接受新任务书）；`npm --prefix zepp_os/balance2_bridge test`：6 passed；`npm --prefix zepp_os/balance2_bridge run build`：成功。首次 pytest 在系统 TEMP 清理时 WinError 5，改用独立 basetemp 后得到明确测试失败。
- Source-to-target mapping (final current paths): `src/vitalis/domain/` holds normalized models; `src/vitalis/application/` holds connector/job ports and pure sync values; `src/vitalis/adapters/persistence/`, `adapters/credentials.py`, `adapters/notifications.py`, `adapters/zepp/` implement storage, secrets, delivery and Zepp; `src/vitalis/entrypoints/api/`, `entrypoints/cli.py`, `entrypoints/worker.py` are process entrypoints; `clients/browser_extension/` and `clients/zepp_os/balance2_bridge/` are current client paths; `skills/vitalis/` is a standalone HTTP Skill. One old implementation path is not left as an import shadow package.
- Capability disposition: Zepp 同步、训练明细与动作标签、持久任务账本、数据健康、确定性 Daily/Weekly/固定 28 天 Monthly、早晚简报、反馈、PushPlus、浏览器配对和 Balance 2 上传：保留并重构；Open Health 影子洞察：研究性隔离；旧 Vitalis 的迁移/兼容工具与重复文档：合并独有内容后删除；用户 APK 中尚未核对的新增 code：缺少证据，不能自动升级为 verified。
- Final offline verification (2026-09-27): `ZEPP_MOCK=true python tools/check.py all --ci` passed quick 108, backend 947 with 1 Windows symlink-permission skip, clients 33 plus Node syntax/tests/package/build (6 Node tests), docs 15 plus 23-link case checker/API-reference check, isolated locked package install, and offline E2E. A second package check in a fresh offline `uv sync --locked --no-dev` environment passed. 本轮 `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` 再次退出 0：quick 109、backend 951+1 skipped、clients 33 + Node 6/build、docs 15、package wheel 102/sdist 106、e2e 1；加强发布物文件白名单后 `quick`、`package` 单独重跑通过。wheel 携带第三方声明，sdist 排除本地 APK/JPG/test 材料。`git diff --check` 通过；真实 Zepp/Hermes/设备/PostgreSQL/systemd/第三方审计仍属外部阻塞。
- Documentation merge/delete mapping: `SYSTEM*` 的现行架构/运维与风险分别归 `architecture`/`operations`；`ARCHITECTURE*` 归 `architecture` 和 `data-contracts`；`GETTING_STARTED*` 归 `quickstart`/`operations`；`ZEPP_INTEGRATION*` 归 `zepp`/`data-contracts`；`API*` 的接口语法归 OpenAPI、语义归 `data-contracts`；`RESEARCH_NOTES*` 仅保留有证据的 Zepp 协议限制；`SYSTEM_HISTORY*` 不作为活跃说明。重复文件删除须在内容归属核验后进行。

| Task | State | Changed paths | Test command/result | Remaining blocker |
| --- | --- | --- | --- | --- |
| T00 | PASSED | `docs/plans/rebuild.md` | 初始基线 838 passed、1 条固定文档清单失败；bridge 6 passed/build 成功；APK SHA256 与既有证据相符 | 真实设备与新增 code 无可核验关联 |
| T01 | PASSED | `README*`, `AGENTS`, `CONTRIBUTING`, `docs/{README,quickstart,architecture,data-contracts,zepp,agents,operations}.md`；用户确认的旧文档删除 | `check.py docs --ci` 15 passed、链接/大小写/生成参考通过 | - |
| T02 | PASSED (offline) | `src/vitalis/{domain,application,adapters,entrypoints}/`, `pyproject.toml`, `uv.lock`, config/CLI | 旧 services 入口移除；导入边界、显式 UoW、quick 118 与独立 wheel/sdist 安装通过 | PostgreSQL 部署不在已验证名单 |
| T03 | PASSED (offline) | domain 模型、训练次数类型、同毫秒样本来源键/tests | 缺失/零、纠错优先、同毫秒幂等及 23/25 小时与非整点本地日回归通过 | 真实厂商/设备时间载荷仍未实测 |
| T04 | PASSED (SQLite) | 当前 schema、显式 init/reset、索引/FK、旧迁移链删除/tests | 空库、索引/外键/漂移拒绝、SQLite UnitOfWork 与备份恢复回归通过 | PostgreSQL live 约束及恢复未验证 |
| T05 | PASSED | Zepp JSON 目录/catalog/parser/refresh/ParseResult/tests | 244 项 Zepp parser/coordinator 关联测试；未知非空不落库、合法空明细可追踪；wheel 目录加载通过 | APK 截图没有新增 code 独立证据，真实 Zepp 未测 |
| T06 | PASSED (SQLite) | worker/调度/心跳/systemd/同步租约/tests | 父子租约/接管/撤销、worker 心跳、通知持久 outbox 与重启不确定状态回归通过 | 真实 systemd/PushPlus 不在离线验收内 |
| T07 | PASSED (offline) | 分析任务端口/适配器、最终事务租约栅栏；用户资料/反馈/偏好/来源输入 revision/tests | 纯 `analyze(dataset, request, policy)`、来源/成功空覆盖及配置摘要、事件最终投影和旧 RUNNING 回收回归通过 | 真实数据质量和医学有效性不由离线验收证明 |
| T08 | PASSED (SQLite offline) | 单一 `/api`、Bearer scopes、错误/同步/反馈幂等账本/tests | 配对 origin/限流、用户优先锁序、Bearer 身份与用途令牌、错误合同及任务幂等回归通过 | PostgreSQL 并发与公网部署未实测 |
| T09 | PASSED | `clients/`、独立 Skill/白名单 HTTP 脚本、生成接口参考/tests | bridge 6 passed/build；独立 Skill 33 contracts；客户端目录离仓可用 | 真实 Hermes/浏览器/手表未授权实测 |
| T10 | PASSED | CLI demo/db/backup/restore/doctor、非 root systemd 双服务/tests | 新库 demo/备份恢复、进程 API/worker 重启、heartbeat doctor 通过 | 真实 systemd 与 PostgreSQL 恢复未验证 |
| T11 | PASSED | `tools/check.py`, `check_docs.py`, CI 三 Python 版本矩阵、锁文件；sdist 白名单与 wheel 第三方声明 | 2026-09-27 `all --ci`: quick 108、backend 947+1 skipped、clients 33、docs 15、package、e2e 通过；本轮 package 单独重跑通过，sdist 106 文件、wheel 102 文件含声明 | GitHub CI 本身未运行；npm audit 未执行（权限/网络边界） |
| T12 | PASSED | 唯一文档导航与短 Skill；重复总纲/手册/工作流删除 | 文档布局 15 passed，23 Markdown link/case checks，Skill self-contained | - |
| T13 | IN_PROGRESS (offline checks PASS) | `tests/e2e/test_offline_acceptance.py`、最终矩阵、跨事务/锁序/预算回归 | 新库→API/worker→离仓 Skill→反馈/分析→重启闭环通过；A05/A15/A17/A27 离线定向验证，未使用真实账号或通知 | 外部 A11/A22/A23/A30 BLOCKED；用户现授权推送当前修复分支，未授权合并 main 或发布真实数据 |

### A01–A32 当前离线验收状态（2026-09-28）

PASS 仅表示对应离线证据已运行；IN_PROGRESS 为强制目标尚未闭环；BLOCKED 不等于通过。`all --ci` 绿色只证明本次自动化覆盖范围，不替代真实账号、医学有效性或公共发布验收。

| ID | 状态 | 依据或缺口 |
| --- | --- | --- |
| A01 | PASS | 单一 `/api` 前缀、`src/vitalis` 安装路径与一个 Settings/CLI；旧 `/api/v1` 未暴露 |
| A02 | PASS | 旧身份/schema 迁移、旧 Skill 工具和旧服务入口删除；新库初始基线 |
| A03 | PASS | 初始五份未跟踪 APK/图片/任务书原件保持未跟踪且未改动；真实库未用于验收 |
| A04 | PASS | `check.py package --ci` 构建 wheel，独立锁定 venv 安装并从仓库外导入/运行 CLI 与扩展资源 |
| A05 | PASS (offline) | 纯聚合/分析、显式 UoW 与来源用例位于 domain/application；路由通过应用层调用，导入边界/事务测试通过；旧 services 入口已移除 |
| A06 | PASS | 缺失/零、来源/设备/单位与显式反馈保留区别；规范次数为整数或 null |
| A07 | PASS (SQLite) | 空库、真实唯一索引/FK、同毫秒幂等及事务回滚测试；PostgreSQL 不在已验证支持名单 |
| A08 | PASS | 未知/旧 schema 与索引/谓词漂移在写入前拒绝，文件/数据不变 |
| A09 | PASS (offline) | Zepp parser 合同与实际已有 payload 变体，未知非空载荷不计为空成功 |
| A10 | PASS | 带 provenance 的唯一 JSON 目录、未知 unmapped、wheel 安装后加载 |
| A11 | BLOCKED | APK 摘要与旧目录证据已核对；两张图无新数值 code→动作链，不能编造新增 verified 映射 |
| A12 | PASS (SQLite) | API 无调度；worker 单调度，chunk/attempt 活租约、父代次、接管/撤销/恢复测试 |
| A13 | PASS (offline) | 认证、网络、空与不可用/未识别载荷分流测试 |
| A14 | PASS (offline) | Bridge 与 Zepp 同毫秒来源 ID、空设备、重复批次、训练改日投影测试 |
| A15 | PASS (offline) | 纯 `analyze(dataset, request, policy)` 固定输入确定性；发布事务以最终事件重建周/月建议及晨报，等生成时间快照按完成 run 一致选取，跨事务合成回归通过 |
| A16 | PASS | 已映射目录标签不提升动作确认或处方证据门槛 |
| A17 | PASS (SQLite offline) | 资料/反馈/偏好/来源事实及首次绑定/恢复、成功空覆盖和配置摘要进入当前资格及提交栅栏；无变化重放、详情键序、设备上传 user-first 锁序回归通过 |
| A18 | PASS (offline) | 双用户及 read/analyze/sync/feedback/manage scopes、令牌摘要/撤销和跨用户任务/训练/反馈拒绝 |
| A19 | PASS (SQLite) | source 删除与过期租约回写拒绝；设备上传所有权检查与写入同一事务 |
| A20 | PASS (offline) | 同仓扩展/bridge/独立 Skill 使用当前路径和 Bearer/用途令牌，Node 测试及构建通过 |
| A21 | PASS | 整个 Skill 复制出仓后，mock HTTP Read/Analyze/Act/错误分支测试通过 |
| A22 | BLOCKED | 未提供可授权的 Hermes 运行环境，未安装或发起真实调用 |
| A23 | BLOCKED | 现有本地健康库只读 schema 检查不兼容当前基线；没有在当前新库完成真实 Zepp 授权与手表/浏览器实机验收，mock/离线测试不能替代 |
| A24 | PASS | 文档 canonical owner、链接大小写与四类读者导航检查 |
| A25 | PASS | 根 AGENTS、集成指南、产品 SKILL 职责分开且可脱离源码 |
| A26 | PASS | 用户逐项确认后的重复总纲、旧迁移指南/规则和旧 Skill 附属页删除，Git 留历史 |
| A27 | PASS (SQLite offline) | 报告/job/意图同事务；worker CAS 认领、发送前持租约重指向、user-first 完成事务与仅唤醒已有不可用意图的回归通过；不确定结果不自动重发，备份包含意图 |
| A28 | PASS (SQLite only) | 文档将 SQLite 列为已验证默认；PostgreSQL 连接可解析但未宣传为经测试支持 |
| A29 | PASS (offline) | wheel/sdist 白名单及安装检查确认第三方声明随包、APK/图片/测试资料不进包；令牌摘要/加密与错误脱敏测试；不等于完整安全审计 |
| A30 | BLOCKED | 缺仓库 LICENSE 与可公开分发的全部第三方依据；未擅自替作者选择许可证 |
| A31 | PASS (local) | 2026-09-28 `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` 退出 0：quick 131、backend 1066+1 Windows 权限跳过、clients 33 + Node 测试/构建、docs 17、wheel 114/sdist 118 与独立安装、离线 E2E 2；GitHub CI 未运行 |
| A32 | PASS (offline) | T00 有效同步、训练、反馈、周期报告、通知与客户端能力均有当前实现/对应测试 |

2026-09-27 continuation (worktree only; no commit): T03 missing-value/ordinal/dense-file/correction target tests 107 passed; pairing/identity API target tests 83 passed; source/config invalidation plus outbox target tests 19 passed; Zepp coordinator tests 39 passed; aggregation/API/architecture tests 17 passed; quick 113 passed after timezone coverage. Canonical report aliases were removed and report briefing tests 7 passed. SourceAccount/UnitOfWork and pure analysis extraction are still under implementation; these figures do not replace a final `all --ci` run.

2026-09-27 later offline milestone (still uncommitted): SourceAccount/UoW, worker-owned sync and pure analysis were implemented; an interim `all --ci` exited 0 with quick 114, backend 987+1 skipped, clients 33 + Node build/tests, docs 15, installed wheel/sdist, offline E2E 1. Feedback transaction moved to application and later backend 1002+1 skipped; A27 delivery migration targeted 116 passed after authorized old-file cleanup; targeted OAuth/re-auth/scheduler/5xx safeguards passed. These runs predate final T08 account/bridge/sync route migration and do not constitute final acceptance.

2026-09-27 review-fix candidate (still uncommitted): first independent high-effort review found eight concrete issues in mock encryption, Zepp pagination/request budget/HRV zone/client cleanup and browser HTML links/script; each now has an offline fix or direct verified closure. `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` exited 0 after these fixes: quick 118, backend 1028 passed + 1 Windows symlink-permission skip, clients 33 + Node native tests/build, docs 17 plus 23 Markdown link/API-reference checks, installed wheel 114 and sdist 118, offline E2E 1 passed. `git diff --check HEAD` passed; second independent review, final staged-content audit and local commits/merge are pending. No real messages or vendor/device calls.

Next task (2026-09-28): 重建本地提交 `f2d94d4` 已完成；用户本轮授权提交并推送当前修复分支的 mock 全分块/晨晚报验收修复，不合并 main。旧健康库向全新当前基线的迁移须独立设计、备份和逐项验收，不能在旧库上原地升级或先删源库；仅在新库成功且有可恢复保全时再按用户授权处理旧库。真实账号、设备及许可证阻塞单列。
External-only blockers: Zepp 账号与设备实测、Hermes 运行时、手表/浏览器真实上传、PostgreSQL 部署恢复、GitHub CI 运行和第三方材料分发许可尚无本地可验证依据。

2026-09-28 current-tree verification (no commit): `.venv/Scripts/python.exe -m pytest -q -x tests` with an out-of-repository temporary directory passed 1087, skipped 1 Windows permission case; A05/A15/A17/A27 targeted tests passed 67, skipped 1. `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` exited 0: quick 118, backend 1028+1 skipped, clients 33 plus Node tests/build, docs 17 plus link/API-reference checks, wheel 114/sdist 118 with independent install, offline E2E 1. `git diff --check HEAD` exited 0. The second independent review and final acceptance-status audit are still in progress; these checks do not establish the external-only items above. Initial runs with the global Python installation and an in-repository pytest temporary directory were environment/setup failures, not product test failures.

2026-09-28 second-review closure (working tree only, no commit): confirmed and fixed same-run event projection drift, running outbox intent/new-run race, source-account create/reactivate input fencing, raw-budget silent truncation and unbounded in-memory aggregation. Focused follow-up closed the post-check outbox defer window with user-first finalization and existing-intent rearm (manual analysis creates no new intent), aligned pairing and device-upload lock order with source revocation, and fixed SQLite workout-detail replay with reordered JSON keys. A tied-generation-time cross-run snapshot mix and mixed-timezone metric request were reproduced and fixed; briefing persistence was correctly excluded because briefings are eligible-snapshot projections under a rules digest. Reordered normalized daily fields were confirmed idempotent through the public model path. Final `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` exited 0: quick 118, backend 1052 passed + 1 Windows skip, clients 33 plus Node tests/build, docs 17 and link/API-reference checks, installed wheel 114/sdist 118, offline E2E 1. `git diff --check HEAD` passed. Staged paths contained no APK/JPG/database/log/credential files; original APK/images/taskbook and the pre-existing pytest log remain untracked. The index still has staged rename/delete intermediates and does not represent the final working tree; no staging, commit, merge, push or real message was performed. An optional broader `ruff --select F` check reported 14 pre-existing unused-import/annotation findings outside the required quick lint selection; this is not an all-green claim for that optional command. PostgreSQL live concurrency, real vendor/devices, Hermes, GitHub CI and distributable licensing remain unverified or blocked.

2026-09-28 mock fresh-data acceptance (uncommitted): the existing `vitalis.db` was inspected through SQLite immutable read-only schema metadata only and lacks current source-account/input-revision schema; it was not initialized, migrated, reset, or used for sync. Initial isolated mock sync attempts exposed outdated nonempty HRV and then DailyHealth event payloads; unknown payloads correctly failed instead of being misreported as empty success. The mock client now emits only typed synthetic events in the requested window, preserves explicit empty optional streams, and aligns watch/device/workout identity with current parsers; production parser and real Zepp client are unchanged. A new out-of-repository empty-database E2E starts actual API and worker, requires successful full sync with written records before analysis, then verifies same-day morning/evening HTTP 200 with one run and no delivery intent. An independent loopback smoke for the current Asia/Shanghai day confirmed the same sync/analysis/report result and a current-day sample; both processes were stopped and the generated test directory removed. `PYTHONPATH=src .venv/Scripts/python.exe tools/check.py all --ci` exited 0: quick 131, backend 1066 passed + 1 Windows permission skip, clients 33 plus Node tests/build, docs 17, wheel 114/sdist 118 installed, E2E 2. No real vendor request, real notification, or user health payload entered test output. This does not verify that the incompatible real database can fetch latest Zepp data or deliver real reports.
