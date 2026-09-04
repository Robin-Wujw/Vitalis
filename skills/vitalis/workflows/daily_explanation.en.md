# Daily Recommendation Explanation

[简体中文](daily_explanation.md)

This workflow handles only same-day explanation requests such as “Why is today's training recommended this way?” and “Explain today's plan.” Call `tools/explain.py` first and render only fields from its `decision_explanation.json` contract. All runtime user-visible output must remain Chinese.

1. If it returns `status=snapshot_missing`, explain in Chinese that no Vitalis analysis snapshot has been generated for that date. Do not substitute yesterday's data or general advice, and do not use `tools/analyze.py` or `tools/sync.py`.
2. `今日结论`: Repeat only `action.action_label` and the primary session, optional session, relationship, and validity period in `action.action_plan`. Do not rewrite intensity, duration, exercises, stop conditions, or safety status.
3. `为什么`: Give at most four items, using only persisted triggering facts and gate results from `facts` and `gates`. Use `inferences` and `action.driver_labels` only for the corresponding Chinese labels. Do not calculate new metrics, fill in missing observations, or present supporting signals as triggers.
4. `数据限制`: State the Chinese status from `snapshot.data_quality`, missing required signals, and `limitations`. When `action.action` is `INSUFFICIENT_DATA`, stop here and do not provide a training recommendation.
5. `依据`: Cite only returned `evidence_refs` that are relevant to the current plan. Do not introduce external web pages, medical conclusions, or evidence that was not returned.
6. Traceability: When needed, explain in Chinese that `snapshot.analysis_run_id` and the version fields identify this persisted analysis rather than a new real-time calculation.

Use Chinese labels throughout all runtime output. Do not expose internal enums, diagnose disease, claim causality, or merge records from different devices or sources.
