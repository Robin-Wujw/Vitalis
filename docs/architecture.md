# 当前架构

[文档中心](README.md) | [数据合同](data-contracts.md) | [运维](operations.md)

Vitalis 是单仓库 Python 服务。FastAPI API 和调度 worker 分进程；应用层组织用例，适配器负责 Zepp、SQLAlchemy 和 PushPlus。客户端读取持久化结果，不能在路由或 Skill 中重算健康事实。本页描述当前实现，不描述已删除计划中的目标。

## 模块职责

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

Bearer 令牌在库中只保存摘要，绑定本地用户和 `read`、`analyze`、`sync`、`feedback`、`manage` scope；`X-User-Id` 不能单独认证。厂商凭据与令牌加密密钥分离保存。API 默认回环监听，公网部署需要 TLS、网关和来源配置。配对码、浏览器链接令牌、同步 attempt 和通知状态都不是健康观测。

来源、scope、设备、单位和观测时间随事实保存。写入事实、显式反馈和纠错时，在同一个事务记录带日期、信号域和输入 revision 的持久分析任务；依赖窗口内的已有目标与合资格的当前目标会重新排队。分析用例将 detached 数据集交给确定性引擎，发布时在用户写锁下检查相关范围的输入游标和配置摘要，避免旧计算覆盖新结果。历史重算只保存历史快照，不回退当前事件状态。

报告读取保留同日期 last-good，并附带新鲜度、任务和失败状态；通知资格仍只接受当前快照。`source_mode` 从数据集进入同步请求和快照，不能随进程配置改变；真实与合成数据不能写入同一用户数据集。`open_health_insights` 是 `shadow_only`，不会改变当前训练决策。

## 修改入口与测试

修改路由或鉴权时从 `entrypoints/api/`、`deps.py` 和当前 API 合同测试开始；修改字段、单位或缺失资格时从 `domain/`、`intelligence/contracts.py`、数据管道测试和[数据合同](data-contracts.md)开始；修改同步时查看 Zepp adapter、worker 测试和[Zepp](zepp.md)。修改 Skill API 文案先改 `tools/generate_api_reference.py` 的生成逻辑或源码合同，再运行生成器，不手改生成产物。

先运行受影响测试，再运行 `python tools/check.py quick` 或 `backend`，最后检查 `git diff --check`。不要在路由复制算法、在 Skill 侧重算趋势、或把投递受理写成最终送达。
