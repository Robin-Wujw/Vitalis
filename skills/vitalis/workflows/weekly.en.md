# Weekly Review

[简体中文](weekly.md)

All runtime user-visible output must be Chinese. Render only WeeklyProfile. The period is the rolling seven local days ending on the requested date. When
`report_context.target_day_complete` is `false`, state that the target day is not yet
complete; do not treat it as a complete day.

1. Use `data_quality.status_label` and `confidence_label` to describe analysis confidence.
2. Present available facts separately from `facts.sleep`, `facts.recovery`, `facts.training`, `facts.activity`, and `facts.feedback`. Never turn missing values into zero, and never write `UNKNOWN` days as rest days.
3. Show `facts.training.record_days`, `unknown_days`, `coverage_status`, and
   `totals_are_partial`. Keep training duration, vendor load, aerobic minutes, strength
   sessions, and rest days missing when they are null; do not recalculate them.
4. Use `inferences.key_changes` and `inferences.events` to describe changes and continuing events during the week.
5. Present every recommendation, action, and supporting evidence in `actions.recommendations.priority` order.
6. Show `inferences.limitations`. Do not calculate a weekly summary from seven daily reports.
7. You may repeat the returned target-day summary and period coverage from `open_health_period_summary` and `open_health_coverage`; do not calculate them from daily reports or another bundle.
