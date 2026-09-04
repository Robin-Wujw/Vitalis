# Weekly Review

[简体中文](weekly.md)

All runtime user-visible output must be Chinese. Render only WeeklyProfile:

1. Use `data_quality.status_label` and `confidence_label` to describe analysis confidence.
2. Present available facts separately from `facts.sleep`, `facts.recovery`, `facts.training`, `facts.activity`, and `facts.feedback`. Never turn missing values into zero.
3. Use `inferences.key_changes` and `inferences.events` to describe changes and continuing events during the week.
4. Present every recommendation, action, and supporting evidence in `actions.recommendations.priority` order.
5. Show `inferences.limitations`. Do not calculate a weekly summary from seven daily reports.
6. You may repeat the returned target-day summary and period coverage from `open_health_period_summary` and `open_health_coverage`; do not calculate them from daily reports or another bundle.
