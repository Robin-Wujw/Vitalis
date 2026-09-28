# Vitalis 文档

[项目介绍](../README.md) | [English overview](../README.en.md)

## 按任务阅读

- 首次使用：[快速开始](quickstart.md) → [运维与恢复](operations.md)。
- 接入 Hermes 或其他客户端：[Agent 与客户端](agents.md) → 运行服务的 `/openapi.json`。
- 了解系统或新增字段：[当前架构](architecture.md) → [数据合同](data-contracts.md) → 运行服务的 `/docs`。
- 接入 Zepp：[Zepp 协议与证据](zepp.md)；扩展构建见[浏览器扩展](../clients/browser_extension/README.md)，设备构建见[Balance 2 Bridge](../clients/zepp_os/balance2_bridge/README.md)。
- 修改仓库：[AGENTS.md](../AGENTS.md) → [CONTRIBUTING.md](../CONTRIBUTING.md) → 相关源码与测试。安全边界见 [SECURITY.md](../SECURITY.md)。

## 主题归属

| 主题 | 唯一主责 |
| --- | --- |
| 产品定位与状态 | [README](../README.md) |
| 安装和第一份合成报告 | [quickstart](quickstart.md) |
| 模块、数据流和进程边界 | [architecture](architecture.md) |
| 单位、时间、缺失与分析资格 | [data-contracts](data-contracts.md) |
| Zepp 协议变体与目录证据 | [zepp](zepp.md) |
| 框架中立的调用与 Skill 安装 | [agents](agents.md) |
| 配置、部署、诊断、备份 | [operations](operations.md) |
| 开发命令和贡献流程 | [CONTRIBUTING](../CONTRIBUTING.md) |
| 仓库 Agent 规则 | [AGENTS](../AGENTS.md) |
| 安全与隐私报告 | [SECURITY](../SECURITY.md) |
| 当前 HTTP 字段、状态和路径 | 服务运行时的 `/openapi.json`；Skill 子集由 `tools/generate_api_reference.py` 生成 |

[重构执行记录](plans/rebuild.md)只记录未完成的实施任务，不作为当前产品规格。Git 保存历史说明，不设第二套 API/旧版本迁移手册。
