---
name: vitalis
description: Use Vitalis Health Intelligence APIs for deterministic Chinese health analysis, training response, personal patterns, timelines, and explicit feedback actions.
---

# Vitalis Health Intelligence

Vitalis is a renderer and orchestrator over the Health Intelligence API. The Python
engine owns normalization, quality, baselines, features, trends, event lifecycles,
decisions, recommendations, monthly analysis, personal associations, training responses,
personal models, and snapshots. Never reproduce those calculations in the model.

## Required Flow

1. Classify the request as Read, Analyze, or Act.
2. Call exactly the relevant tool. Read tools never generate analysis. Use
   `tools/analyze.py` only when the user requests a fresh analysis or after an explicit
   synchronization; use `tools/context.py` only for broad, layered context.
3. Select exactly one workflow from `workflows/`.
4. If the user explicitly provides sex, confirmed maximum heart rate, or sleep target,
   call `tools/profile.py patch` with the current profile revision; otherwise call
   `tools/profile.py get` when profile state is needed.
5. Render only facts, inferences, actions, comparisons, drivers, limitations, and
   recommendations already present in the response. Open Health fields are descriptive
   shadow insights only; render `open_health_insights` and typed period/context summaries
   without recomputing or using them to alter `decision`.

All user-visible content must be Chinese. Render `*_label`, `*_labels`, workout
`sport_mode_label`, recognition labels, and the structured `decision.action_plan`.
Internal enum codes exist only for program control and must never appear in the answer.

## Hard Boundaries

- Do not create, average, transform, score, threshold, or trend health measurements.
- Do not calculate a WeeklyProfile or MonthlyProfile by combining shorter profiles.
- Do not calculate, rank, or reinterpret correlation coefficients.
- Do not calculate training response, recovery time, personal patterns, or timeline
  relationships from DailyProfiles or raw fields.
- Do not change `decision.action`, `decision.confidence`, intensity, duration, drivers,
  limitations, or rule IDs.
- Do not invent a workout, exercise, set, repetition, heart-rate zone, or progression.
  Training content must come from `decision.action_plan`.
- Preserve `primary_session`, `optional_session`, and `session_relationship_label`.
  Never present an alternative as an addition or combine sessions the planner separated.
- Do not treat vendor readiness, Charge, sleep score, or sleep stages as Vitalis truth.
- If action is `INSUFFICIENT_DATA`, name the missing signals and stop. Do not infer a
  training decision from general advice or prior days.
- Do not diagnose disease. Persistent deviations may be described only as observations;
  urgent symptoms or medical questions require professional care.
- Do not silently merge local users or device streams.
- Do not cite evidence that is absent from `evidence_refs`.

## Workflow Routing

- Morning status or today's training: call `tools/daily.py`, then `workflows/morning.md`.
- Evening summary or tonight's focus: call `tools/daily.py`, then `workflows/evening.md`.
- Weekly review: call `tools/weekly.py`, then `workflows/weekly.md`.
- Monthly review or recent 28-day cycle: call `tools/monthly.py`, then
  `workflows/monthly.md`.
- Trends or recent changes: call `tools/trends.py` or `tools/events.py`, then
  `workflows/on_demand.md`.
- "Why this recommendation?": call `tools/explain.py`, then `workflows/on_demand.md`.
- Broad health context: call `tools/context.py`, then the closest matching workflow. Context contains only
  the compact latest Open Health summary; use `insights_stale` and refusal/missing-input fields as returned.
- Fresh deterministic analysis: call `tools/analyze.py`; subsequent reads may select
  the returned Daily, Weekly, response, or personal result.
- Training response: call `tools/training_responses.py`, then `workflows/on_demand.md`.
- Personal patterns: call `tools/personal_model.py`, then `workflows/on_demand.md`.
- Cross-metric personal associations: call `tools/personal_associations.py`, then
  `workflows/on_demand.md`.
- Recent sequence of events: call `tools/timeline.py`, then `workflows/on_demand.md`.
- Mark a recommendation completed only after the user identifies both the recommendation
  and completed workout: call `tools/complete_recommendation.py`.
- Record RPE, fatigue, mental state, soreness, or notes: call `tools/feedback.py add`.
- List feedback: call `tools/feedback.py list`.
- Read or replace running/strength targets, rotation, treadmill/weather fallback,
  availability, experience, equipment, and pain/injury state with
  `tools/training_preferences.py`; use `set` for explicit full replacement or `patch` to update
  only explicitly supplied fields. `workout_source` is required whenever a workout is linked.
- Confirm exact exercises for a strength workout only from the user's statement by
  calling `tools/strength_exercises.py`; never derive an exercise from heart rate.
- Acknowledge an event only after the user asks: call `tools/acknowledge_event.py`.
- Synchronize source data only after the user asks: call `tools/sync.py`.
- Configure automated PushPlus delivery only after the user asks: schedule
  `tools/daily_push.py --period morning` for sleep-aware morning retries and
  `tools/daily_push.py --period evening` for the evening recap. It requires private
  `VITALIS_USER` and `PUSHPLUS_TOKEN` environment variables and keeps the model out of
  token handling and report assembly. Use `--test` for a real manual delivery that must
  not read or write the scheduled report's daily deduplication marker.

Wire contracts are documented in `schemas/`. Evidence scope and interpretation limits
are summarized in `knowledge/evidence.md`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `VITALIS_API` | `http://localhost:8000` | Vitalis API origin |
| `VITALIS_USER` | required | Local Vitalis user ID; there is no implicit user fallback |
| `PUSHPLUS_TOKEN` | daily push only | Private PushPlus delivery token |
