# 近 28 天回顾

[English](monthly.en.md)

全程使用中文，先调用 `tools/monthly_briefing.py`，只渲染返回的 `ReportBriefing 1.0`。
其 `sections` 与 HTML 报告同源；不得从 MonthlyProfile 或原始记录另行拼接。月报是显式查询能力，不是新的 cron：

1. 使用 `period_start` 和 `period_end` 说明连续 28 个本地日，不称为自然月。
2. `coverage`：展示已核实的训练历史天数，以及睡眠、HRV、活动各自的有效天数；`data_quality` 不能替代每项指标的实际覆盖范围。
3. `sleep_recovery` 和 `training_activity`：逐条展示 `facts` 的原始单位、有效天数和前期对比分母；“已记录小计”不是完整 28 日总量。不同设备流分别显示，缺失不写成零，未知日不写成休息日。不同口径热量不相加，也不推断热量赤字。
4. `associations`：只呈现已返回的配对天数和关联描述，关联不代表因果，不据此生成建议。
5. `actions`：只展示已返回的周期变化和阶段建议，同一句变化不在前面章节重复；不得从周报、日报或关联系数自行计算月度结论。
6. `limitations` 放在相关章节作一次具体数据说明，不逐条加“限制：”；保留真实缺失、来源不一致和部分覆盖，不用模糊措辞掩盖它们。
