# 每周回顾

[English](weekly.en.md)

全程使用中文，只渲染 WeeklyProfile。周期是截至请求日期的滚动 7 个本地日期；如果
`report_context.target_day_complete` 为 `false`，必须明示目标日尚未结束，不能把它
当成完整的一天。

1. 用 `data_quality.status_label` 和 `confidence_label` 说明分析可信度。
2. 从 `facts.sleep`、`facts.recovery`、`facts.training`、`facts.activity` 和
   `facts.feedback` 分别展示可用事实；缺失值不写成零，`UNKNOWN` 天数不写成休息日。
3. 展示 `facts.training.record_days`、`unknown_days`、`coverage_status` 和
   `totals_are_partial`；训练总时长、厂商负荷、有氧分钟、力量场次和休息日为 null 时
   保持缺失，不做补算。
4. 用 `inferences.key_changes` 和 `inferences.events` 说明本周变化与持续事件。
5. 按 `actions.recommendations.priority` 顺序完整展示建议、行动和依据。
6. 展示 `inferences.limitations`，不得根据七份日报自行计算周汇总。
7. 可复述 `open_health_period_summary` 与 `open_health_coverage` 中已返回的目标日摘要和周期覆盖，不得从日报或其他 bundle 自行计算。
