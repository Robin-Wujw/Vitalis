# Evening Summary

[简体中文](evening.md)

All runtime user-visible output must be Chinese. Call `tools/evening_briefing.py` first and render only its `ReportBriefing 1.0`. Its `sections` share the HTML report projection; do not reconstruct from raw `features` or repeat the morning narrative:

1. `training`: Lead with returned workout summaries and confirmed or observed movements in order. Group only adjacent same-name observed sets; never merge A–B–A or treat observed sets as user-confirmed. Show repetitions, weights, and actual units from the original `facts` only when detail is requested; say when a value or name is unrecorded instead of inventing it.
2. Show each workout's summary and specialist measurements once. No recorded formal workout is not proof of a rest day and does not warrant compensatory training.
3. `activity`: Keep steps, activity, and each device energy scope separate, with its actual unit. Do not add energy entries or call unknown-scope energy total expenditure.
4. `intraday`: Device stress scores, heart-rate samples, and first/last observation times describe recorded intervals only. Explain incomplete coverage near the metric; unobserved intervals are not zero.
5. `recovery`: Distinguish last night's recovery readings from today's training load; do not call pre-training readings post-exercise recovery.
6. Lead each section with 1–2 returned key facts and conclusions. Keep vendor scores, raw sampling detail, and repeated source labels out of the main narrative; explain them when asked or when sources conflict. Repeat only returned `facts`, `interpretation`, and actual safety information; do not reschedule today's workout, calculate new values, or forecast tomorrow's recovery.
7. Render `limitations` once as data notes in the related sections rather than prefixing every item with “限制：” or collecting them at the end. Put missing/partial scopes beside the corresponding number.
8. Do not proactively request RPE, physical fatigue, mental state, or muscle soreness. Only when the user explicitly provides feedback, record it verbatim using `tools/feedback.py add`; do not infer a score.
