# 近 28 天回顾

[English](monthly.en.md)

全程使用中文，先调用 `tools/monthly_briefing.py`，只渲染返回的 `ReportBriefing 1.0`。
其 `sections` 与 HTML renderer 相同，不得从 MonthlyProfile 或 raw profile 自由拼接。Monthly renderer 是显式能力，不是新的 cron。月度数据范围如下：

1. 明确这是截至目标日期的连续 28 个本地日，不称为自然月。
2. 用 `data_quality.status_label` 和 `confidence_label` 说明分析可信度。
3. 从 `facts.sleep`、`facts.recovery.streams`、`facts.training`、`facts.activity`
   和 `facts.feedback` 展示事实；不同设备恢复流分别呈现，缺失值不写成零，未知日不解释为休息日。活动热量若为 `role=unspecified`，不得称为总能耗或与其他入口、训练热量相加；没有摄入数据时不判断热量赤字。
4. 用 `inferences.key_changes`、`events` 和 `personal_associations` 说明变化与个人
   关联。关联不代表因果，不得据此新建建议。
5. 按 `actions.recommendations.priority` 展示 Vitalis 已给出的行动和依据，并展示
   `inferences.limitations`。不得从周报、日报或关联系数自行计算月度结论。
6. 可复述 `open_health_period_summary` 与 `open_health_coverage` 中已返回的目标日摘要和周期覆盖，不得自行计算或改写状态。
