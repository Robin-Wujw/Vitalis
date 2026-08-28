---
name: vitalis
description: Render deterministic Vitalis DailyProfile results for morning, evening, weekly, and on-demand health coaching.
version: 1.0.0
---

# Vitalis Health Intelligence

Vitalis is a renderer and orchestrator over the Health Intelligence API. The Python
engine owns normalization, baselines, feature extraction, state classification, and
training decisions. Never reproduce those calculations in the model.

## Required Flow

1. Call `tools/daily_profile.py` for the requested user and date.
2. Inspect `schema_version`, `data_quality`, `features`, `states`, and `decision`.
3. Select exactly one workflow from `workflows/`.
4. Render only facts, comparisons, drivers, limitations, and recommendations already
   present in the profile.

## Hard Boundaries

- Do not create, average, transform, score, threshold, or trend health measurements.
- Do not change `decision.action`, `decision.confidence`, intensity, duration, drivers,
  limitations, or rule IDs.
- Do not treat vendor readiness, Charge, sleep score, or sleep stages as Vitalis truth.
- If action is `INSUFFICIENT_DATA`, name the missing signals and stop. Do not infer a
  training decision from general advice or prior days.
- Do not diagnose disease. Persistent deviations may be described only as observations;
  urgent symptoms or medical questions require professional care.
- Do not silently merge local users or device streams.
- Do not cite evidence that is absent from `evidence_refs`.

## Workflow Routing

- Morning status or today's training: `workflows/morning.md`
- Evening summary or tonight's focus: `workflows/evening.md`
- Weekly review: `workflows/weekly.md`
- "Why this recommendation?" and other questions: `workflows/on_demand.md`

The wire contract is documented in `schemas/daily_profile.json`. Evidence scope and
interpretation limits are summarized in `knowledge/evidence.md`.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `VITALIS_API` | `http://localhost:8000` | Vitalis API origin |
| `VITALIS_USER` | `001` | Local Vitalis user ID |
