# Vitalis

[English](README.en.md) | [文档中心](docs/README.md)

Vitalis 将可穿戴设备的睡眠、活动和训练记录变成可追溯的个人健康分析与训练建议。确定性引擎计算事实之上的推断；Hermes 等客户端只能读取结构化结果、解释证据和记录用户明确提供的反馈，而不能自行生成健康结论。

## 产品主线

Zepp 和设备补充数据经过同步、规范化和来源核对，形成个人基线与不可变分析快照，再由 API 和客户端读取同一份结果。Zepp 账号密码只输入官方页面，缺失观测不会被伪造。

## 当前状态

项目仍处于预发布阶段。源码位于 `src/vitalis/`；CLI 提供 `serve`、`worker`、`user create`、`token issue/revoke`、`db init/reset`、`doctor` 和不需要真实账号的 `demo`。当前 HTTP 操作统一使用 `/api` 前缀，调度器只由独立 worker 启动。健康智能提供 Daily、Weekly、Monthly、训练响应和只用于描述的 `open_health_insights`，后者不改变现行训练决策。数据语义与适用边界见[数据合同](docs/data-contracts.md)。

用户范围的 HTTP 调用使用绑定本地用户的 Bearer 令牌及 `read`、`analyze`、`sync`、`feedback`、`manage` 权限；`X-User-Id` 不能单独认证。产品 Skill 使用独立的 Bearer HTTP 薄客户端；离仓 mock 验收与真实 Hermes 环境调用需分开核验，详见[智能体集成](docs/agents.md)。

## 信任边界

- **事实、推断和建议分离。** 设备观测、系统判断和行动建议保留不同语义与来源。
- **缺失数据保持缺失。** Vitalis 不用零值、旧快照、厂商分数或模板内容补齐关键观测。
- **设备与身份隔离。** 指标按来源、scope、设备和单位保存；一个 Zepp 厂商身份只能属于一个本地用户。
- **个人基线优先。** 系统关注相对个人历史的变化，不把单一人群阈值当作个人结论。
- **Agent 不重新计算健康事实。** Agent 只能使用版本化结构化结果，不能自行生成趋势、分数或训练处方。
- **不是医疗设备。** Vitalis 用于个人趋势观察和运动决策支持，不诊断疾病，也不替代医生判断。

## 最短入口

在 Python 3.11-3.13 环境安装后，运行 `vitalis demo --database demo.db --day 2026-09-26` 可向**新建的** SQLite 文件写入合成数据和一次分析；`python -m vitalis` 提供同一命令。已有文件不会被覆盖。安装、令牌签发和首份报告见[快速开始](docs/quickstart.md)。生产或长期数据需要先确认[运维](docs/operations.md)与[安全](SECURITY.md)边界，不能直接公开本地默认服务。

## 按角色阅读

- 首次使用：[快速开始](docs/quickstart.md)；部署或同步排障：[运维](docs/operations.md)。
- 接口或字段开发：[当前架构](docs/architecture.md) → 服务的 `/docs` → [数据合同](docs/data-contracts.md)。
- Zepp 连接：[Zepp](docs/zepp.md)；Hermes/其他客户端：[智能体集成](docs/agents.md)。
- 仓库开发：[AGENTS.md](AGENTS.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)；完整索引见[文档中心](docs/README.md)。
