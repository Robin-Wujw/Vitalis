# Vitalis Documentation / 文档中心

[中文项目介绍](../README.md) | [English project overview](../README.en.md)

本文档中心按读者和任务导航。每类信息只有一个主要维护位置：根 README 负责产品定位，专题文档负责运行、接入、API、架构和研究细节，`SYSTEM.md` 负责当前开发规则与未完成事项。

This hub organizes documentation by audience and task. Each subject has one primary maintenance location: the root READMEs describe the product, topic documents own operational and technical detail, and `SYSTEM.md` owns current development rules and unfinished work.

## 中文导航

### 使用与运维

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [开始使用](GETTING_STARTED.md) | 本地使用者、部署维护者 | 安装、配置、本地运行、API/worker 服务、调度、Hermes 与验证 |
| [Zepp 数据接入](ZEPP_INTEGRATION.md) | Zepp 用户、集成维护者 | 官方浏览器配对、凭据生命周期、身份唯一性迁移、数据覆盖和设备边界 |
| [浏览器扩展](../browser_extension/README.md) | 需要完成 Zepp 配对的用户 | Chrome/Edge 加载、官方登录交接、安全限制和自动续期 |
| [Balance 2 Bridge](../zepp_os/balance2_bridge/README.md) | Balance 2 开发者 | Zepp OS 采集队列、上传协议、构建、预览和设备安装 |

### API 与 Agent 集成

| 文档 | 内容 |
| --- | --- |
| [API 导读](API.md) | 当前 HTTP 路径、用户身份要求、同步与智能查询入口 |
| [Hermes Skill](../skills/vitalis/SKILL.md) | Read / Analyze / Act 工具、工作流选择和 Agent 边界 |
| [证据知识库](../skills/vitalis/knowledge/evidence.md) | Hermes 可引用的证据范围与禁止扩展的结论 |

### 架构与研究

| 文档 | 内容 |
| --- | --- |
| [系统架构](ARCHITECTURE.md) | 数据层、同步账本、智能管线、版本化合同、决策策略和安全边界 |
| [调研笔记](RESEARCH_NOTES.md) | 外部证据、研究限制、候选算法和未来实现条件；不是当前产品规范 |

### 项目维护

| 文档 | 内容 |
| --- | --- |
| [当前开发系统](../SYSTEM.md) | 当前有效的工作流程、数据边界、文档职责、项目状态和未完成事项 |
| [历史归档](SYSTEM_HISTORY.md) | 已完成任务、旧计划和过去验证记录；不能覆盖当前开发系统 |
| [systemd 单元](../deploy/systemd/) | Linux API 与 worker 服务定义；部署步骤仍以“开始使用”为准 |

## English Navigation

### Usage and Operations

| Document | Audience | Scope |
| --- | --- | --- |
| [Getting Started](GETTING_STARTED.md) | Local users and operators | Installation, configuration, local runtime, API/worker services, scheduling, Hermes, and verification |
| [Zepp Integration](ZEPP_INTEGRATION.md) | Zepp users and integration maintainers | Official browser pairing, credential lifecycle, identity migration, data coverage, and device boundaries |
| [Browser Extension](../browser_extension/README.md) | Users completing Zepp pairing | Chrome/Edge loading, official-login handoff, security constraints, and credential renewal |
| [Balance 2 Bridge](../zepp_os/balance2_bridge/README.md) | Balance 2 developers | Zepp OS collection journal, upload protocol, build, preview, and device setup |

### API and Agent Integration

| Document | Scope |
| --- | --- |
| [API Guide](API.md) | Current HTTP paths, user identity requirements, synchronization, and intelligence queries |
| [Hermes Skill](../skills/vitalis/SKILL.md) | Read / Analyze / Act tools, workflow routing, and agent boundaries |
| [Evidence Knowledge](../skills/vitalis/knowledge/evidence.md) | Evidence Hermes may cite and conclusions it must not extend |

### Architecture and Research

| Document | Scope |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Data layer, durable synchronization, intelligence pipeline, versioned contracts, decision policy, and safety boundaries |
| [Research Notes](RESEARCH_NOTES.md) | External evidence, research limitations, candidate algorithms, and implementation prerequisites; not a current product contract |

### Project Maintenance

| Document | Scope |
| --- | --- |
| [Current Development System](../SYSTEM.md) | Active workflow, data boundaries, documentation ownership, project status, and unfinished work |
| [Historical Archive](SYSTEM_HISTORY.md) | Completed tasks, superseded plans, and past verification records; never overrides the current system |
| [systemd Units](../deploy/systemd/) | Linux API and worker service definitions; deployment procedure remains in Getting Started |

## Documentation Ownership

- Product purpose, primary experience, trust boundaries, current status: `README.md` and `README.en.md`.
- Installation, operation, deployment, scheduling, backup, migration, and verification: `GETTING_STARTED.md`.
- Zepp authentication, credential lifecycle, source identity, data coverage, and device behavior: `ZEPP_INTEGRATION.md`.
- HTTP endpoint guide: `API.md`; generated OpenAPI remains the complete endpoint reference.
- Data contracts, architecture, intelligence policy, and algorithm boundaries: `ARCHITECTURE.md`.
- External evidence and future candidates: `RESEARCH_NOTES.md`.
- Current engineering rules and open work: `../SYSTEM.md`; completed history: `SYSTEM_HISTORY.md`.
