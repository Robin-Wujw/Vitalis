# Morning Analysis

[简体中文](morning.md)

Use the requested date, or today's date when none is given. Read `tools/morning_briefing.py` first and render only its fields. Do not reconstruct a report from DailyProfile features. All runtime user-visible output must be ordinary Chinese, phrased like a coach explaining what to do today.

Render returned last-night sleep, morning body signals, yesterday's activity and recorded workouts, then any activity observed today through the analysis cutoff in `sections` order. Put a supported training plan last. Preserve each fact's date, unit, and source, showing each `facts` and `interpretation` item once. Do not repeat `observations` or `key_reasons` already present in a section. No workout yet today is not missing data and must not change the report to `INSUFFICIENT_DATA`. Do not mix historical `observed_sets` into morning prescriptions.

A facts-only report (`facts_only`) may include date-checked activity and recorded workouts, but never claim those workouts prove complete training history or substitute yesterday's activity for today's. State an unverified history gap once. Do not add today's exercises, intensity, or weights, or reconstruct a prescription from the full daily profile. Render a complete report in this order:

1. `今天做什么`: Show the returned `sections.today_plan` duration, intensity, and steps. Use `action_plan.primary_session`, `optional_session`, and `session_relationship` only to check whether sessions are alternatives or additions; do not repeat the same dose.
2. `为什么`: Use explanations already in the relevant sections; add only distinct returned `key_reasons`, up to three. Do not add unreturned measurements or internal codes.
3. `需要留意`: Deduplicate the `limitations` in `sections` and the `cautions`, describing each missing or partial datum beside its number. Show actual stop conditions separately as `停止条件`, not as generic limitations.
4. When `decision_action` is `INSUFFICIENT_DATA`, show only returned sleep/body facts and dated activity/workout observations in `sections`, plus `data_quality`, `key_reasons`, and `cautions` in Chinese. Do not present yesterday's activity as today's workout, or add general training advice or a compensating arrangement.

Call `tools/explain.py` only when the user asks why; present persisted evidence and data notes without recomputing or choosing measurements. Open Health remains a descriptive shadow insight, not a morning decision input.
