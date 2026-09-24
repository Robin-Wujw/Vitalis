# Recent 28-Day Review

[简体中文](monthly.md)

All runtime user-visible output must be Chinese. First call `tools/monthly_briefing.py` and render only its `ReportBriefing 1.0`. Its `sections` share the HTML projection; do not reconstruct a report from MonthlyProfile or raw records. Monthly reporting is an explicit query capability, not a new cron job:

1. Use `period_start` and `period_end` to identify 28 consecutive local days, not a calendar month.
2. `coverage`: Show verified training-history days separately from valid sleep, HRV, and activity days. `data_quality` is not a substitute for per-metric coverage.
3. `sleep_recovery` and `training_activity`: Lead with 1–2 representative returned `facts` while preserving units, valid-day counts, and prior-period denominators; expand detailed values only when needed. A recorded subtotal is not a full 28-day total. Separate conflicting device streams, never turn missing values into zero or unknown days into rest days, and never add unlike energy scopes or infer an energy deficit. Vendor scores are not period conclusions.
4. `associations`: Repeat only returned paired-day counts and association summaries. Association does not imply causation or justify a new recommendation.
5. `actions`: Show returned period changes and recommendations once, without echoing the same change in earlier sections. Do not calculate a monthly conclusion from weekly/daily reports or association coefficients.
6. Render `limitations` once as specific notes beside the relevant section, not as repeated “限制：” prefixes. Keep real gaps, source mismatches, and partial coverage visible rather than hiding them behind vague wording.
