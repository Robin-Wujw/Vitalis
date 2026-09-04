# Evening Summary

[简体中文](evening.md)

All runtime user-visible output must be Chinese. If `open_health_insights.training_load` is returned, repeat only that day's TRIMP and the descriptive ATL/CTL/TSB values. Describe TSB only as a training-load difference, not as recovery or status. For a refusal or missing input, briefly state the returned missing input in Chinese.

Do not repeat the morning narrative. Begin with what actually happened today:

1. From `recent_workouts`, use `sport_mode_label` to show every workout that day, its duration, vendor load, and `recognition_confidence_label`. Do not show only the broad `type`.
2. Show today's load, 7-day duration, 7-day load, and `load_state_label`.
3. Use Chinese status labels, `positive_signal_labels`, `negative_signal_labels`, and the action label to describe recovery status and whether to continue activity tonight.
4. Present recent context with each available trend's `metric_label`, `direction_label`, and `confidence_label`, plus unresolved events' `summary`, severity label, and lifecycle label.
5. Do not reschedule today's training in the evening. You may review returned discipline-specific analysis in `features.training.running` and `features.training.strength`, but must not calculate or add actions.
6. Explain the judgment with Chinese evidence labels and always put data limitations in the final section.
7. After training, you may briefly ask once for RPE, physical fatigue, mental state, or muscle soreness. After receiving an answer, record it verbatim with `tools/feedback.py add`; never infer a rating for the user.

The current model has no recovery forecasting capability. Do not predict tomorrow's recovery.
