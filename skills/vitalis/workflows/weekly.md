# Weekly

Version 1 supports only engine-computed seven-day training fields from the profile:
`duration_7d`, `load_7d`, `aerobic_minutes_7d`, `strength_sessions_7d`,
`workout_type_counts_7d`, and `recent_workouts`.

Render those values, the current load state, and the current decision. Explicitly say
that weekly sleep and recovery aggregates are unavailable in this model version. Do
not fetch seven daily profiles and calculate aggregates in the model.
