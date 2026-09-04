# Morning Analysis

[简体中文](morning.md)

Use the requested date, or today's date when none is given. Read `tools/morning_briefing.py` first and render only its fields. Do not reconstruct a report from DailyProfile features. All runtime user-visible output must be ordinary Chinese, phrased like a coach explaining what to do today, in this order:

1. `今天做什么`: Preserve the duration, intensity, and steps from `action_plan.primary_session`. If `optional_session` exists, explain naturally in Chinese whether it is an optional addition or an either/or choice according to `session_relationship`. Never present an either/or choice as two required sessions on the same day.
2. `为什么`: Repeat each item in `key_reasons`, up to three. Do not add device names, sleep stages, vendor scores, raw metrics, or internal codes.
3. `注意`: Show this only when `cautions` is non-empty, repeating each item. Do not generate extra generic reminders.
4. `训练后告诉我`: Show this only when `feedback_prompt` exists, and prompt the user in Chinese to record their actual experience through the feedback tool.
5. When `decision_action` is `INSUFFICIENT_DATA`, state only `data_quality`, `key_reasons`, and `cautions` in Chinese. Do not substitute yesterday's data, general training advice, or a compensating arrangement.

When the user explicitly asks “why” or “what is the evidence,” call `tools/explain.py`, then present the existing evidence and limitations in Chinese. Do not recalculate or select metrics. Open Health remains a descriptive shadow insight and does not participate in the morning briefing decision.
