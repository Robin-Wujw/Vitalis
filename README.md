# Vitalis

[English](README.en.md) | [文档中心](docs/README.md)

> 让穿戴设备数据变成每天可执行、可解释的健康与训练决策。

Vitalis 是面向个人长期数据的健康智能引擎。它连接穿戴设备，持续同步睡眠、HRV、静息心率、活动、训练和主观反馈，建立个人基线，并生成 Daily、Weekly、Monthly 以及训练响应分析。

Vitalis 不是让大模型读取原始数据后自由发挥。健康状态、趋势、事件和训练决策都由确定性引擎计算；Hermes 或其他 Agent 只负责调用结构化结果、解释证据和记录用户明确提供的反馈。

## 从数据到行动

```text
Zepp / Balance 2 / Helio Strap
              |
              v
  持久同步与规范化数据存储
              |
              v
数据质量 -> 个人基线 -> 趋势与事件 -> 训练决策
              |
              v
 Morning / Evening / Weekly / Monthly
              |
              v
   API、Hermes Skill、PushPlus
```

典型使用流程：

1. 通过官方 Zepp 登录页面和浏览器配对导入凭据，不向 Vitalis 提交账号密码。
2. API 与独立 worker 使用持久同步账本获取并规范化健康数据。
3. 确定性智能管线分析当天状态、近期趋势、训练负荷和历史响应。
4. API、Hermes 或 PushPlus 展示同一份结构化结论，而不是各自重新计算。

## 当前能力

| 范围 | 已实现能力 |
| --- | --- |
| 数据接入 | Zepp 云端历史同步、官方浏览器配对、Balance 2 Zepp OS 补充上传 |
| 个人基线 | 睡眠、HRV、静息心率、活动和训练负荷的个人正常范围与偏离 |
| 训练决策 | 健康门控优先的跑步、力量、恢复或休息建议，以及具体训练剂量 |
| 周期分析 | Daily、Weekly、固定 28 天 Monthly、7/28/90 天趋势和健康事件生命周期 |
| 训练闭环 | 建议、实际训练、主观反馈、T+1/T+2/T+3 响应和个人历史模式 |
| 开放洞察 | 可审计的 readiness、睡眠规律性、TRIMP、ATL/CTL/TSB，保持 shadow-only |
| Agent 集成 | Hermes Read / Analyze / Act 工具、解释工作流和 PushPlus 每日推送 |
| 运维 | SQLite/PostgreSQL、持久同步尝试、worker、systemd 单元和数据健康状态 |

详细数据合同、算法边界和拒绝条件以[架构文档](docs/ARCHITECTURE.md)为准。

## 信任边界

- **事实、推断和建议分离。** 设备观测、系统判断和行动建议保留不同语义与来源。
- **缺失数据保持缺失。** Vitalis 不用零值、旧快照、厂商分数或模板内容补齐关键观测。
- **设备与身份隔离。** 指标按来源、scope、设备和单位保存；一个 Zepp 厂商身份只能属于一个本地用户。
- **个人基线优先。** 系统关注相对个人历史的变化，不把单一人群阈值当作个人结论。
- **Agent 不重新计算健康事实。** Agent 只能使用版本化结构化结果，不能自行生成趋势、分数或训练处方。
- **不是医疗设备。** Vitalis 用于个人趋势观察和运动决策支持，不诊断疾病，也不替代医生判断。

## 项目状态

Vitalis 当前是可运行、持续开发中的 pre-production 项目，首个完整数据生态为 Zepp、Balance 2 与 Helio Strap。当前重点是数据正确性、同步恢复、透明决策、训练反馈闭环和可验证部署；在这些基础稳定前，不引入不可解释的综合健康分或自动替代现有决策策略的预测模型。

生产或长期数据环境必须先阅读部署、备份、身份迁移和恢复要求。不要直接把开发默认配置暴露到公网。

## 开始使用

- [文档中心](docs/README.md)：按使用者、集成开发者和维护者导航
- [开始使用](docs/GETTING_STARTED.md)：安装、本地运行、服务部署、调度与验证
- [Zepp 数据接入](docs/ZEPP_INTEGRATION.md)：官方登录、凭据生命周期、身份所有权与数据覆盖
- [API 导读](docs/API.md)：当前 HTTP 接口和身份要求
- [系统架构](docs/ARCHITECTURE.md)：数据流、智能管线、决策边界与版本化合同
- [Hermes Skill](skills/vitalis/SKILL.md)：Agent 的 Read / Analyze / Act 调用边界
