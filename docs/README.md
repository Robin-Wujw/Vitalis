# 文档中心

这是 Vitalis 当前文档入口。先按读者和任务选择一份主责指南；历史计划、一次性审计和已删除的旧文档不作为日常入口。所有命令默认从仓库根目录执行，除非页面另有说明。

## 先走哪条路

- 想得到第一份不发送通知的合成报告：读[快速开始](quickstart.md)。
- 想理解报告周期和 PushPlus：读[报告与渠道](reports.md)。
- 想改代码或接口：先读[当前架构](architecture.md)，再看[数据合同](data-contracts.md)和测试。
- 想连接 Zepp：读[Zepp](zepp.md)；想接入 Skill 或 Hermes：读[智能体集成](agents.md)。
- 想执行整改：读[预发布整改计划](plan.md)。
- 想发布到服务器：读[发布与部署 SOP](deployment.md)；想运行、备份或排障：读[运维](operations.md)。

## 主题归属

| 主题 | 唯一当前指南 |
| --- | --- |
| 产品、数据和报告整改计划 | [预发布整改计划](plan.md) |
| 第一次运行与 API 读取 | [快速开始](quickstart.md) |
| 报告周期与 PushPlus | [报告与渠道](reports.md) |
| 模块职责与数据流 | [当前架构](architecture.md) |
| 字段、单位与缺失资格 | [数据合同](data-contracts.md) |
| Zepp 登录、同步与协议 | [Zepp](zepp.md) |
| Skill、Hermes 与 Bearer 客户端 | [智能体集成](agents.md) |
| 配置、worker、备份与诊断 | [运维](operations.md) |
| 本地合并、服务器发布与破坏性换库 | [发布与部署 SOP](deployment.md) |
| 仓库开发规则 | [AGENTS.md](../AGENTS.md) |
| 依赖、测试与变更流程 | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| 漏洞、凭据与隐私边界 | [SECURITY.md](../SECURITY.md) |
| 浏览器扩展配对 | [扩展指南](../clients/browser_extension/README.md) |
| 第三方来源与许可原文 | [第三方声明](../THIRD_PARTY_NOTICES.md) |

## 如何使用这些页面

用户指南按“目标、前提、步骤、成功结果、常见失败”组织；开发指南按“职责、入口、输入输出、修改与测试”组织。字段和版本以当前源码、运行服务的 `/openapi.json` 及生成参考为准。遇到文档与实际行为不一致时，先运行目标测试并修正唯一主责文档，不在多个页面维护不同默认值。
