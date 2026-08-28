---
name: vitalis
description: Use Vitalis Health Intelligence APIs for deterministic Chinese daily and weekly analysis, trends, health events, training explanations, and subjective feedback.
---

# Vitalis Health Intelligence

Vitalis is a renderer and orchestrator over the Health Intelligence API. The Python
engine owns normalization, quality, baselines, features, trends, events, states,
decisions, recommendations, and snapshots. Never reproduce those calculations in the model.

## Required Flow

1. Classify the request as Read, Analyze, or Act.
2. Call exactly the relevant tool; use `tools/context.py` only when one request needs
   daily, weekly, event, and feedback context together.
3. Select exactly one workflow from `workflows/`.
4. Render only facts, inferences, actions, comparisons, drivers, limitations, and
   recommendations already present in the response.

All user-visible content must be Chinese. Render `*_label`, `*_labels`, workout
`sport_mode_label`, recognition labels, and structured `prescriptions`. Internal enum
codes exist only for program control and must never appear in the answer.

## Hard Boundaries

- Do not create, average, transform, score, threshold, or trend health measurements.
- Do not calculate a WeeklyProfile by combining DailyProfiles.
- Do not change `decision.action`, `decision.confidence`, intensity, duration, drivers,
  limitations, or rule IDs.
- Do not invent a workout, exercise, set, repetition, heart-rate zone, or progression.
  Training content must come from `decision.prescriptions`.
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
- Trends or recent changes: call `tools/trends.py` or `tools/events.py`, then
  `workflows/on_demand.md`.
- "Why this recommendation?": call `tools/explain.py`, then `workflows/on_demand.md`.
- Broad health context: call `tools/context.py`, then the closest matching workflow.
- Record RPE, fatigue, mental state, soreness, or notes: call `tools/feedback.py add`.
- List feedback: call `tools/feedback.py list`.
- Acknowledge an event only after the user asks: call `tools/acknowledge_event.py`.
- Synchronize source data only after the user asks: call `tools/sync.py`.

Wire contracts are documented in `schemas/`. Evidence scope and interpretation limits
are summarized in `knowledge/evidence.md`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `VITALIS_API` | `http://localhost:8000` | Vitalis API origin |
| `VITALIS_USER` | required | Local Vitalis user ID; there is no implicit user fallback |
