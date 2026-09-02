# 每周回顾

全程使用中文，只渲染 WeeklyProfile：

1. 用 `data_quality.status_label` 和 `confidence_label` 说明分析可信度。
2. 从 `facts.sleep`、`facts.recovery`、`facts.training`、`facts.activity` 和
   `facts.feedback` 分别展示可用事实；缺失值不写成零。
3. 用 `inferences.key_changes` 和 `inferences.events` 说明本周变化与持续事件。
4. 按 `actions.recommendations.priority` 顺序完整展示建议、行动和依据。
5. 展示 `inferences.limitations`，不得根据七份日报自行计算周汇总。
6. 可复述 `open_health_period_summary` 与 `open_health_coverage` 中已返回的目标日摘要和周期覆盖，不得从日报或其他 bundle 自行计算。
