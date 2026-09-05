# Evening Summary

[简体中文](evening.md)

All runtime user-visible output must be Chinese. If `open_health_insights.training_load` is returned, repeat only that day's TRIMP and the descriptive ATL/CTL/TSB values. Describe TSB only as a training-load difference, not as recovery or status. For a refusal or missing input, briefly state the returned missing input in Chinese.

Do not repeat the morning narrative. Begin with what actually happened today:

1. From `recent_workouts`, use `sport_mode_label` to show every workout that day, `started_at`, its duration, vendor load, and `recognition_confidence_label`. Do not show only the broad `type`. Describe real workout details in start-time order; state plainly when there was no formal workout and do not treat that as an anomaly.
2. When returned by `features.training.running` or `features.training.strength`, show the available discipline-specific running metrics, strength sets, and confirmed exercises. Preserve nulls, unknown units, and missing fields; do not invent `kg`, exercise names, or weights.
3. Show today's load, 7-day duration, 7-day load, and `load_state_label`; when 7-day totals are affected by coverage, state `history_coverage` and `totals_are_partial`.
4. Use Chinese status labels, `positive_signal_labels`, `negative_signal_labels`, and the action label to describe recovery status and whether to continue activity tonight.
5. Present recent context with each available trend's `metric_label`, `direction_label`, and `confidence_label`, plus unresolved events' `summary`, severity label, and lifecycle label.
6. Do not reschedule today's training in the evening. You may review returned discipline-specific analysis, but must not calculate or add actions.
7. Explain the judgment with Chinese evidence labels and always put data limitations in the final section.
8. After training, you may briefly ask once for RPE, physical fatigue, mental state, or muscle soreness. After receiving an answer, record it verbatim with `tools/feedback.py add`; never infer a rating for the user.

The current model has no recovery forecasting capability. Do not predict tomorrow's recovery.
