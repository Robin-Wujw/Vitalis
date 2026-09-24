# 晨间分析

[English](morning.en.md)

使用请求的日期；如果未指定日期，则使用今天。先读取 `tools/morning_briefing.py`，并且只渲染其中的字段。不得根据 DailyProfile 特征重新构建报告。

全程使用普通中文，按 `sections` 顺序展示已返回的昨晚睡眠、今早身体信号、昨天活动与已记录训练、今天截至分析时的活动；有依据的今天安排放在最后。每项数字保留日期、单位和来源，`facts` 与 `interpretation` 各展示一次。`observations`、`key_reasons` 与章节重复时不再复述。当天尚无 workout 不代表数据缺失，不得因此变为 `INSUFFICIENT_DATA`；晨报处方不得混入历史 `observed_sets`。

事实版（`facts_only`）也可展示按日期核对的活动和已记录训练事实，但不能把已记录场次说成完整训练历史；昨天的活动不替代今天的记录。训练历史未查全时只说明一次，不得补写今天的训练动作、强度、重量或从完整日报重建处方。完整报告按以下顺序渲染：

1. `今天做什么`：直接展示 `sections.today_plan` 已有的时长、强度和步骤；用 `action_plan.primary_session`、`optional_session` 与 `session_relationship` 核对可选补充或二选一，不重复列出同一剂量。
2. `为什么`：使用相关章节已有的解释；仅当 `key_reasons` 提供未出现的事实时补充，最多三条。不增加未返回的指标或内部代码。
3. `需要留意`：`sections` 的 `limitations` 和 `cautions` 按内容去重，并在相关数字旁用具体状态说明；真实停止条件单独标为 `停止条件`，不统称为“限制”。
4. 当 `decision_action` 为 `INSUFFICIENT_DATA` 时，只展示 `sections` 已有的睡眠、身体、按日期标注的活动和训练观测，以及 `data_quality`、`key_reasons` 和 `cautions`；不得把昨天的活动说成今天完成的训练，也不得给通用训练建议或补偿性安排。

用户明确询问“为什么”时再调用 `tools/explain.py`；只展示已保存的证据和数据说明，不重新计算或挑选指标。Open Health 是描述性影子洞察，不参与晨报决策。
