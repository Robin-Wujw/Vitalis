# 当前架构

[文档中心](README.md) | [数据合同](data-contracts.md) | [运维](operations.md)

Vitalis 是单仓库 Python 服务。FastAPI API 和调度 worker 分进程；应用层组织用例，适配器负责 Zepp、SQLAlchemy 和 PushPlus。客户端读取持久化结果，不能在路由或 Skill 中重算健康事实。本页描述当前实现，不描述已删除计划中的目标。

## 模块职责

用户业务操作统一经过 HTTP API：报告读取与导出、Zepp 配对、手动同步、显式重算、反馈、纠错和偏好修改。`vitalis api ...`、MCP 和 Hermes 使用同一套 Bearer/scope、幂等键及任务状态；客户端不接触数据库或厂商凭据。数据库初始化、令牌签发、诊断、部署和隔离合成回放保留为本地管理命令，worker 通过应用用例执行后台工作。

`src/vitalis/api_client.py` 是标准库 HTTP 客户端的唯一源码；`skills/vitalis/scripts/vitalis_api.py` 由 `tools/generate_api_reference.py` 原样生成。`request METHOD relative-path --param NAME=VALUE` 覆盖当前 JSON 业务接口，写入 JSON 来自 stdin；鉴权由服务端负责。原先直接访问数据库的 report/connect/sync/wake-check/shadow 业务命令已删除。

| 模块 | 职责 |
| --- | --- |
| `src/vitalis/entrypoints/api/` | FastAPI 路由、Bearer scope、错误合同和当前 `/api` OpenAPI。 |
| `src/vitalis/entrypoints/worker.py`、`scheduler/` | 唯一调度所有者；启动任务、分析和通知处理。API 不启动 scheduler。 |
| `src/vitalis/domain/` | 用户、设备、观测、训练和值对象；不依赖 HTTP、ORM 或厂商。 |
| `src/vitalis/application/` | 同步、分析任务、健康查询、范围聚合、投递资格和智能用例端口。 |
| `src/vitalis/intelligence/` | 数据资格、个人基线、趋势、事件、训练决策和报告投影。 |
| `src/vitalis/adapters/zepp/` | Zepp 网络、区域主机、协议识别、解析、同步分块和覆盖状态。 |
| `src/vitalis/adapters/persistence/` | SQLAlchemy 模型、当前 schema 检查、仓储、任务租约和报告快照。 |
| `src/vitalis/adapters/notifications.py`、`daily_push.py` | PushPlus transport、直接手动投递和状态映射；不承载健康资格计算。 |
| `skills/vitalis/` | 可离仓安装的 Bearer HTTP 薄客户端和固定 API 白名单；不实现分析算法。 |

## 数据流

```text
Zepp / mock
    -> 同步分块、租约和覆盖账本
    -> 规范观测、训练和用户输入
    -> 确定性分析与 AnalysisRun
    -> Daily / Morning / Weekly / Monthly 快照
    -> API、导出、PushPlus 和 Skill
```

`bootstrap.py` 把应用端口与 SQL/Zepp 适配器组装起来。demo 使用显式 mock 同步写入新库；生产同步由 worker 持有持久 attempt、幂等键、分块和租约。读取报告只从数据库读快照，不查 Zepp、不启动同步、不重新计算。显式分析请求入队，worker 执行并保存新的 `AnalysisRun` 及关联报告。

报告路径是 `/api/reports/{kind}`，当前支持 daily、morning、evening、weekly、monthly 以及 briefing 读取变体。`/api/data-status` 是用户范围的数据覆盖和任务状态；`/live` 和 `/ready` 只是进程/schema 探针。`/api/deliveries` 返回清洗后的通知意图状态。完整参数以运行服务 `/openapi.json` 为准。

## 信任边界

`intelligence/recorded_context.py` 是补充观测汇总的唯一计算入口，结果随日报和周报保存。`report_recorded_context.py` 只组织这些已保存字段的文字；API 的公开 view 与完整报告保留同一来源、日期和资格，Hermes 不再自行计算目标达成或补充指标趋势。

Bearer 令牌在库中只保存摘要，绑定本地用户和 `read`、`analyze`、`sync`、`feedback`、`manage`、`coach`、`source_raw` scope；`X-User-Id` 不能单独认证。`source_raw` 单独保护原始厂商 GET 任务和原文读取，不隐含于 read/sync/coach。厂商凭据与令牌加密密钥分离保存。API 默认回环监听，公网部署需要 TLS、网关和来源配置。配对码、浏览器链接令牌、同步 attempt 和通知状态都不是健康观测。

`HealthAnalytics` 与 `WorkoutStatistics` 读取 `HealthReader` 的 detached 值，将描述统计交给 intelligence；HTTP/Skill 不计算个人趋势。运动列表和统计共用范围、过滤与读取上限；默认详情不包含 GPS 或大段原始流，显式 sections 才读取。来源/设备/单位/source_field/qualification 分组不互相抵消。`source_snapshots` 保存已清洗 Zepp profile/device 元信息，独立于本人确认的 profile；原始 GET 使用现有 SyncAttempt 单 chunk、worker 租约和加密 journal，没有第二任务引擎。查询不触发同步，raw 内容也不参与报告或训练决策。

来源、scope、设备、单位和观测时间随事实保存。写入事实、显式反馈和纠错时，在同一个事务记录带日期、信号域和输入 revision 的持久分析任务；依赖窗口内的已有目标与合资格的当前目标会重新排队；同步写入的数据只重排今天和之前 7 天的目标（`ANALYSIS_REFRESH_DAYS`），180 天首次回填不会把半年的历史分析逐块重算一遍，用户自己的反馈和纠错仍重算对应日期。分析用例将 detached 数据集交给确定性引擎，发布时在用户写锁下检查相关范围的输入游标和配置摘要，避免旧计算覆盖新结果。历史重算只保存历史快照，不回退当前事件状态。

报告读取保留同日期上一份成功结果（`last-good`），并附带新鲜度、任务和失败状态；通知资格仍只接受当前快照。`source_mode` 从数据集进入同步请求和快照，不能随进程配置改变；真实与合成数据不能写入同一用户数据集。日常分析只读日级特征和近两天原始样本。

注意区分当前参与决策的分析与仅用于验证的候选结果：

- **准备度和身体信号预警**已在日常分析中计算，并参与恢复状态和训练强度调整；离峰批处理也记录它们，用于后续核对。
- **开放健康洞察和个人反应模型**在离峰批处理中计算、单独保存，不改变当前训练决策。
- **候选跑步能力和单次训练总分**仍未通过正式验证，不作为日常强度安排的依据。

计算边界见[数据合同](data-contracts.md)，离峰任务的运行说明见[运维](operations.md#日常分析路径与离峰任务)。

## 修改入口与测试

修改路由或鉴权时从 `entrypoints/api/`、`deps.py` 和当前 API 合同测试开始；修改字段、单位或缺失资格时从 `domain/`、`intelligence/contracts.py`、数据管道测试和[数据合同](data-contracts.md)开始；修改同步时查看 Zepp adapter、worker 测试和[Zepp](zepp.md)。修改 Skill API 文案先改 `tools/generate_api_reference.py` 的生成逻辑或源码合同，再运行生成器，不手改生成产物。

先运行受影响测试，再运行 `python tools/check.py quick` 或 `backend`，最后检查 `git diff --check`。不要在路由复制算法、在 Skill 侧重算趋势、或把投递受理写成最终送达。
