# Recent 28-Day Review

[简体中文](monthly.md)

All runtime user-visible output must be Chinese. Render only MonthlyProfile:

1. Make clear that this is the 28 consecutive local days ending on the target date, not a calendar month.
2. Use `data_quality.status_label` and `confidence_label` to describe analysis confidence.
3. Present facts from `facts.sleep`, `facts.recovery.streams`, `facts.training`, `facts.activity`, and `facts.feedback`. Present recovery streams from different devices separately, and never turn missing values into zero.
4. Use `inferences.key_changes`, `events`, and `personal_associations` to describe changes and personal associations. Association does not imply causation; do not create a new recommendation from it.
5. Present the actions and evidence already provided by Vitalis in `actions.recommendations.priority` order, and show `inferences.limitations`. Do not calculate a monthly conclusion from weekly reports, daily reports, or association coefficients.
6. You may repeat the returned target-day summary and period coverage from `open_health_period_summary` and `open_health_coverage`; do not calculate them or rewrite their status.
