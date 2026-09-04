# 每日建议解释

[English](daily_explanation.en.md)

仅处理“为什么今天这样建议训练”“解释今天的计划”等当天解释请求。先调用
`tools/explain.py`，只渲染其 `decision_explanation.json` 契约中的字段。

1. 若返回 `status=snapshot_missing`，说明该日期尚未生成 Vitalis 分析快照；
   不得改用昨天的数据、通用建议、`tools/analyze.py` 或 `tools/sync.py`。
2. `今日结论`：只复述 `action.action_label` 和 `action.action_plan` 中的主训练、可选训练、
   二者关系及有效期。不得改写强度、时长、动作、停止条件或安全状态。
3. `为什么`：最多四条，只使用 `facts` 与 `gates` 中已持久化的触发事实和门控结果；
   `inferences` 与 `action.driver_labels` 仅用于对应中文标签。不得计算新指标、补全缺失观测
   或把支持性信号写成触发原因。
4. `数据限制`：说明 `snapshot.data_quality` 的中文状态、缺失必需信号和 `limitations`。当
   `action.action` 为 `INSUFFICIENT_DATA` 时，到此为止，不得给出训练建议。
5. `依据`：仅在返回的 `evidence_refs` 与当前计划相关时引用；不得引入外部网页、医学结论
   或未返回的证据。
6. 可追溯性：必要时说明 `snapshot.analysis_run_id` 和版本字段代表该次已持久化分析，
   不是新的实时计算。

全程使用中文标签。不得输出内部枚举、诊断疾病、声称因果关系，或合并不同设备/来源的记录。
