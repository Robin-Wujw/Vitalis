# Recent 28-Day Review

[简体中文](monthly.md)

All runtime user-visible output must be Chinese. First call `tools/monthly_briefing.py` and render only its `ReportBriefing 1.0`. Its `sections` are the same as the HTML renderer; do not freely compose from MonthlyProfile or a raw profile. The Monthly renderer is an explicit capability, not a new cron job. The monthly data scope is:

1. Make clear that this is the 28 consecutive local days ending on the target date, not a calendar month.
2. Use `data_quality.status_label` and `confidence_label` to describe analysis confidence.
3. Present facts from `facts.sleep`, `facts.recovery.streams`, `facts.training`, `facts.activity`, and `facts.feedback`. Present recovery streams from different devices separately, never turn missing values into zero, and never describe unknown days as rest days. When activity calories use `role=unspecified`, do not call them total energy expenditure or add them across entries or workout calories; without intake data, do not infer an energy deficit.
4. Use `inferences.key_changes`, `events`, and `personal_associations` to describe changes and personal associations. Association does not imply causation; do not create a new recommendation from it.
5. Present the actions and evidence already provided by Vitalis in `actions.recommendations.priority` order, and show `inferences.limitations`. Do not calculate a monthly conclusion from weekly reports, daily reports, or association coefficients.
6. You may repeat the returned target-day summary and period coverage from `open_health_period_summary` and `open_health_coverage`; do not calculate them or rewrite their status.
