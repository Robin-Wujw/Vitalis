# Vitalis Health Intelligence Architecture v1.0

## 1. System Boundary

Vitalis has three layers. Only the Intelligence layer computes health state or training
decisions.

```text
Assistant
  Hermes Skill: Morning / Evening / Weekly / On-demand rendering
                              |
Intelligence                  v
  DailyProfile -> Quality -> Baseline -> Features -> States -> Decision
                              |
Data                          v
  Zepp connector -> normalized records -> SQLite/PostgreSQL
```

Hermes is not an analysis engine. It may select and phrase fields from `DailyProfile`,
but cannot calculate trends, scores, thresholds, confidence, or replacement advice.

## 2. Data Layer

`vitalis/connectors` authenticates, synchronizes, and normalizes vendor payloads. The
analysis layer reads only normalized records:

- Daily records: sleep, activity, and training.
- `metric_samples`: timestamped HR, RMSSD, SDNN, sleep HRV/RHR, readiness components,
  Charge, SpO2, and other supported measurements.
- `daily_metrics`: sparse vendor facts such as readiness, stress, respiratory rate,
  PAI, ODI, and lactate-threshold fields.
- `workouts` and `workout_samples`: normalized workout summaries and detail HR.
- `dense_data_files`: metadata for opaque `SEC_HR` payloads; indexed files are not
  represented as decoded measurements.

Raw measurements and vendor scores remain separate from Vitalis-derived states.
`ahi_readiness` and `afib_readiness` are vendor readiness component scores, not AHI or
AFib diagnoses.

## 3. Intelligence Layer

The implementation lives in `vitalis/intelligence`:

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Versioned Pydantic contracts for facts, quality, baselines, features, states, and decisions |
| `profile.py` | One-local-user loader, provenance, target-day facts, and deterministic quality flags |
| `baseline.py` | Device/metric-specific 7-day and 28-day robust statistics |
| `analyzers.py` | Sleep, HRV/RHR, recovery, and training feature extraction |
| `decision.py` | Explainable training action policy with abstention |
| `service.py` | Assemble the complete `DailyProfile` |

### 3.1 DailyProfile

The wire contract is `schema_version=1.0`, `model_version=vitalis-intelligence-1`:

```text
DailyProfile
|- data_quality: status, missing signals, coverage, flags, device validity
|- facts: target-day normalized observations and provenance
|- baselines: 7d/28d robust statistics per metric and device stream
|- features: sleep, HRV/RHR, recovery, training
|- states: sleep, recovery, training load
|- decision: action, confidence, drivers, limitations, rule IDs
|- evidence_refs
`- metadata: identity and product-policy versions
```

There is no uncalibrated Vitalis 0-100 recovery score. Vendor readiness, Charge, and
sleep scores are labeled as vendor context and do not become the Vitalis result.

### 3.2 Data Quality

Three concepts are intentionally separate:

- `data_quality`: deterministic completeness, coverage, provenance, query-limit, and
  identity flags.
- `device_validity`: evidence metadata, `UNKNOWN` unless device-specific validation is
  attached.
- `decision.confidence`: a rule-based inference-completeness band, not a device
  measurement probability.

Missing target-day sleep or HRV is explicit. The engine never assigns default score
points or reuses an old analysis result.

One vendor identity may map to multiple local Vitalis users. The loader reports
`SOURCE_IDENTITY_SHARED` and continues using only the requested local user; it never
merges history automatically.

### 3.3 Baselines

Baseline keys include local user, metric, source, source scope, device ID, window, and
model version. RMSSD, SDNN, sleep HRV, and RHR never share numerical baselines; device
streams are never averaged together.

Within each stream, high-frequency samples are reduced to one daily median before
history eligibility is counted. The engine computes:

- median and median absolute deviation (MAD);
- 25th and 75th percentiles;
- least-squares trend per day;
- sample count, distinct-day count, and coverage;
- percent deviation and robust z-score for the target day.

RMSSD uses natural-log values for robust statistics and retains an original-ms
`reference_value`. Seven-day baselines require 3 distinct days and 28-day baselines
require 14. These minimums are versioned product policy, not medical truth.

### 3.4 Feature and Decision Policy

The preferred HRV stream must have a target-day observation. Selection ranks usable
28-day coverage first, then metric preference (`RMSSD`, sleep HRV, SDNN). RHR is paired
conservatively and is not substituted into an HRV baseline.

Recovery requires at least two baseline-interpretable signals from HRV, RHR, sleep,
and recent training load. Multi-signal suppression can produce `RECOVERY` or `REST`;
good recovery plus low load can produce `TRAIN_HARD`. Every decision returns rule IDs,
drivers, limitations, and one action:

```text
TRAIN_HARD | TRAIN_NORMAL | TRAIN_LIGHT | RECOVERY | REST | INSUFFICIENT_DATA
```

The engine detects deviations and guides training; it does not diagnose disease.
Sleep stages remain trend-only. The v1 training load is vendor-derived and lacks RPE,
sets/reps/weight, and reliable aerobic-intensity classification.

## 4. API and Assistant

The sole computed-analysis endpoint is:

```text
GET /api/v1/intelligence/daily-profile?day=YYYY-MM-DD
X-User-Id: <local user>
```

`/api/v1/health/*` remains the raw/summarized data and synchronization surface. The
prototype `/api/v1/health/today` and `/api/v1/analyze` paths were removed because the
application is pre-production and no compatibility contract is required.

`skills/vitalis` contains one HTTP tool, a JSON Schema, evidence limits, and four
renderer workflows. Weekly v1 may render only the engine-computed seven-day training
fields; weekly sleep/recovery aggregation is not implemented and Hermes may not derive
it from seven daily calls.

## 5. Scheduled Flow

```text
02:00  sync 7 days
09:30  sync 2 days -> DailyProfile -> Morning renderer -> push
21:30  sync 1 day  -> DailyProfile -> Evening renderer -> push
```

The scheduler pushes an explicit insufficient-data profile when analysis requirements
are not met. It does not delay and then substitute stale health results.

## 6. Evidence Boundaries

- WHO activity guidance supplies population-level weekly context, not readiness rules.
- HRV measurement standards support metric separation and lnRMSSD handling, not a
  proprietary recovery score.
- World Sleep Society recommendations bound consumer sleep-stage interpretation.
- AASM/SRS provides a general adult sleep-duration reference.
- IOC consensus supports integrated load/recovery/health monitoring, not a universal
  acute-to-chronic ratio threshold.

Device-specific validity evidence for Balance 2 and Helio is not currently attached,
so `device_validity.status` remains `UNKNOWN`.

## 7. Current Scope

Implemented: versioned DailyProfile, deterministic quality/provenance, device-isolated
7/28-day baselines, sleep/HRV/recovery/training features, explainable decisions,
Morning/Evening pushes, and thin Hermes workflows.

Not implemented: 60/90-day personal correlations, health anomaly persistence,
minute-level stress load, Energy Dynamics, weekly sleep/recovery review, training RPE,
body composition/BP integration, forecasts, or medical alerts. These require explicit
new data contracts and validation rather than LLM inference.
