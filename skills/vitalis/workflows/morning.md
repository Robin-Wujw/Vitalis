# 晨间分析

[English](morning.en.md)

使用请求的日期；如果未指定日期，则使用今天。先读取 `tools/morning_briefing.py`，并且只渲染其中的字段。不得根据 DailyProfile 特征重新构建报告。

全程使用普通中文，像教练说明今天怎么做。先展示 `observations` 中已返回的睡眠和身体状态；如果有当天跑步或力量训练上下文，也一并展示。当天尚未有 workout 不是缺项，不得因此把晨报改成 `INSUFFICIENT_DATA`。

按以下顺序渲染：

1. `今天做什么`：保留 `action_plan.primary_session` 的时长、强度和步骤。若有 `optional_session`，按 `session_relationship` 用自然语言说明是可选补充还是二选一；不得把二选一写成同日必做。
2. `为什么`：逐条转述 `key_reasons`，最多三条。不得补充设备名、睡眠分期、厂商评分、原始指标或内部代码。
3. `注意`：仅在 `cautions` 非空时展示，逐条转述；不要额外生成通用提醒。
4. `训练后告诉我`：仅在 `feedback_prompt` 存在时展示，并提示用户通过反馈工具记录实际感受。
5. 当 `decision_action` 为 `INSUFFICIENT_DATA` 时，只说明 `data_quality`、`key_reasons` 和 `cautions`，不得改用昨天数据、通用训练建议或补偿性安排。

当用户明确询问“为什么”或“依据是什么”时，调用 `tools/explain.py`，再展示已有的证据与限制；不得重新计算或挑选指标。Open Health 仍是描述性影子洞察，不参与晨报决策。
