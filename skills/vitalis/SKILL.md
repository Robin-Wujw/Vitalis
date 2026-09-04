---
name: vitalis
description: Use Vitalis Health Intelligence APIs for deterministic Chinese health analysis, training response, personal patterns, timelines, and explicit feedback actions.
---

# Vitalis 健康智能

[English](SKILL.en.md)

Vitalis 是健康智能 API 之上的渲染器和编排器。Python 引擎负责归一化、质量评估、基线、特征、趋势、事件生命周期、决策、建议、月度分析、个人关联、训练响应、个人模型和快照。绝不能在模型中复现这些计算。

## 必需流程

1. 将请求分类为读取（Read）、分析（Analyze）或操作（Act）。
2. 只调用与请求完全对应的工具。读取工具绝不生成分析。仅当用户请求重新分析或完成明确要求的同步后，才使用 `tools/analyze.py`；仅在需要广泛的分层上下文时使用 `tools/context.py`。
3. 从 `workflows/` 中只选择一个工作流。
4. 如果用户明确提供性别、已确认的最大心率或睡眠目标，则携带当前资料 revision 调用 `tools/profile.py patch`；否则，在需要资料状态时调用 `tools/profile.py get`。
5. 只渲染响应中已经存在的事实、推断、行动、比较、驱动因素、限制和建议。Open Health 字段只是描述性的影子洞察；渲染 `open_health_insights` 以及有类型定义的周期/上下文摘要时，不得重新计算，也不得用它们改变 `decision`。

所有面向用户的内容都必须使用中文。渲染 `*_label`、`*_labels`、锻炼的 `sport_mode_label`、识别标签以及结构化的 `decision.action_plan`。内部枚举代码仅用于程序控制，绝不能出现在回答中。

## 硬性边界

- 不得创建、平均、转换、评分、设定阈值或推导健康测量趋势。
- 不得通过合并较短周期的档案来计算 WeeklyProfile 或 MonthlyProfile。
- 不得计算、排序或重新解释相关系数。
- 不得根据 DailyProfile 或原始字段计算训练响应、恢复时间、个人模式或时间线关系。
- 不得更改 `decision.action`、`decision.confidence`、强度、时长、驱动因素、限制或规则 ID。
- 不得虚构锻炼、练习动作、组数、次数、心率区间或进阶安排。训练内容必须来自 `decision.action_plan`。
- 保留 `primary_session`、`optional_session` 和 `session_relationship_label`。绝不能把替代选项说成附加项目，也不得合并规划器已分开的训练。
- 不得把厂商 readiness、Charge、睡眠评分或睡眠阶段视为 Vitalis 事实。
- 如果行动为 `INSUFFICIENT_DATA`，指出缺失信号后停止。不得根据一般建议或前几日数据推断训练决策。
- 如果读取工具返回 404，用中文说明所请求日期没有已生成的分析快照。不得回退到昨天、调用 `tools/analyze.py`、调用 `tools/sync.py`，也不得提供推断出的健康结论。
- 不得诊断疾病。持续偏离只能描述为观察结果；紧急症状或医疗问题需要寻求专业照护。
- 不得在未说明的情况下合并本地用户或设备数据流。
- 不得引用 `evidence_refs` 中不存在的证据。

## 工作流路由

- 晨间状态或今日训练：调用 `tools/morning_briefing.py`，然后使用 `workflows/morning.md`。仅当用户询问晨报背后的依据时才使用 `tools/explain.py`。
- 晚间总结或今晚重点：调用 `tools/daily.py`，然后使用 `workflows/evening.md`。
- 每周回顾：调用 `tools/weekly.py`，然后使用 `workflows/weekly.md`。
- 每月回顾或最近 28 天周期：调用 `tools/monthly.py`，然后使用 `workflows/monthly.md`。
- 趋势或近期变化：调用 `tools/trends.py` 或 `tools/events.py`，然后使用 `workflows/on_demand.md`。
- “为什么这样建议？”或“解释今天的计划”：调用 `tools/explain.py`，然后使用 `workflows/daily_explanation.md`。这是固定的只读工作流。
- 广泛健康上下文：调用 `tools/context.py`，然后使用最匹配的工作流。上下文只包含精简的最新 Open Health 摘要；按原样使用返回的 `insights_stale` 以及拒绝/缺失输入字段。
- 新的确定性分析：调用 `tools/analyze.py`；后续读取可以选择返回的 Daily、Weekly、响应或个人结果。
- 训练响应：调用 `tools/training_responses.py`，然后使用 `workflows/on_demand.md`。
- 个人模式：调用 `tools/personal_model.py`，然后使用 `workflows/on_demand.md`。
- 跨指标个人关联：调用 `tools/personal_associations.py`，然后使用 `workflows/on_demand.md`。
- 近期事件序列：调用 `tools/timeline.py`，然后使用 `workflows/on_demand.md`。
- 只有在用户同时指出建议和已完成锻炼后，才能将建议标记为已完成：调用 `tools/complete_recommendation.py`。
- 记录 RPE、疲劳、精神状态、酸痛或备注：调用 `tools/feedback.py add`。
- 列出反馈：调用 `tools/feedback.py list`。
- 使用 `tools/training_preferences.py` 读取或替换跑步/力量目标、轮换、跑步机/天气后备方案、可用时间、经验、器材和疼痛/伤病状态；明确进行完整替换时使用 `set`，只更新明确提供的字段时使用 `patch`。凡关联锻炼时都必须提供 `workout_source`。
- 只有依据用户陈述，才能调用 `tools/strength_exercises.py` 确认力量训练的具体动作；绝不能根据心率推导动作。
- 只有在用户要求后才确认事件：调用 `tools/acknowledge_event.py`。
- 只有在用户要求后才同步源数据：调用 `tools/sync.py`。
- 只有在用户要求后才配置自动 PushPlus 推送：为可感知睡眠并重试的晨间推送调度 `tools/daily_push.py --period morning`，为晚间回顾调度 `tools/daily_push.py --period evening`。该功能需要私密的 `VITALIS_USER` 和 `PUSHPLUS_TOKEN` 环境变量，并让模型不接触令牌处理和报告组装。使用 `--test` 执行真实的手动推送；该模式不得读取或写入定时报告的每日去重标记。

线协议契约记录在 `schemas/` 中。证据范围和解释限制汇总在 `knowledge/evidence.md` 中。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `VITALIS_API` | `http://localhost:8000` | Vitalis API 源地址 |
| `VITALIS_USER` | 必填 | 本地 Vitalis 用户 ID；不存在隐式用户回退 |
| `PUSHPLUS_TOKEN` | 仅每日推送 | 私密的 PushPlus 推送 token |
