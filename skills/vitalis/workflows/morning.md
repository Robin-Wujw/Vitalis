# 晨间分析

[English](morning.en.md)

使用请求的日期；如果未指定日期，则使用今天。先读取 `tools/morning_briefing.py`，并且只渲染其中的字段。不得根据 DailyProfile 特征重新构建报告。

全程使用普通中文，按 `sections` 顺序展示已返回的睡眠、恢复和今天安排；每项数字保留对应单位，`facts` 与 `interpretation` 各展示一次。`observations`、`key_reasons` 与章节重复时不再复述。当天尚无 workout 不代表数据缺失，不得因此变为 `INSUFFICIENT_DATA`；晨报处方不得混入历史 `observed_sets`。

事实版（`facts_only`）只展示返回的睡眠与身体状态、一次训练历史说明和必要安全信息；不得补写训练动作、强度、重量或从完整日报重建处方。完整报告按以下顺序渲染：

1. `今天做什么`：直接展示 `sections.today_plan` 已有的时长、强度和步骤；用 `action_plan.primary_session`、`optional_session` 与 `session_relationship` 核对可选补充或二选一，不重复列出同一剂量。
2. `为什么`：使用相关章节已有的解释；仅当 `key_reasons` 提供未出现的事实时补充，最多三条。不增加未返回的指标或内部代码。
3. `需要留意`：`sections` 的 `limitations` 和 `cautions` 按内容去重，并在相关数字旁用具体状态说明；真实停止条件单独标为 `停止条件`，不统称为“限制”。
4. 当 `decision_action` 为 `INSUFFICIENT_DATA` 时，只展示已有的睡眠与身体事实、`data_quality`、`key_reasons` 和 `cautions`，不得改用昨天数据、通用训练建议或补偿性安排。

用户明确询问“为什么”时再调用 `tools/explain.py`；只展示已保存的证据和数据说明，不重新计算或挑选指标。Open Health 是描述性影子洞察，不参与晨报决策。
