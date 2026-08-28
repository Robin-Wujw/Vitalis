# Vitalis Health Intelligence Architecture v2.0

## 1. System Boundary

Vitalis has three layers. Only the Intelligence layer computes health state or training
decisions.

```text
Assistant
  Hermes Skill: Read / Analyze / Act + Chinese rendering
                              |
Intelligence                  v
  Daily/Weekly -> Quality -> Baseline -> Features -> Trends -> Events
                              |                         |
                              `----> States -> Decision -> Actions
                              |
Data                          v
  Zepp connector -> normalized records -> SQLite/PostgreSQL
```

Hermes is not an analysis engine. It may select and phrase fields from structured
Vitalis responses, but cannot calculate trends, weekly aggregates, scores, thresholds,
confidence, or replacement advice.

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

Workout summaries retain the original numeric vendor type ID. `sport_types.py` maps
all 120 currently public Zepp OS modes plus the two additional public legacy Huami
cloud-history modes to a stable code, exact Chinese label, broad category, and training
family. Known numeric definitions carry high recognition confidence. Missing or
unknown IDs remain explicit and auditable rather than inheriting an endpoint label or
being guessed into a known activity.

Timestamped measurements remain stored in UTC. Daily intelligence groups them by the
configured application timezone (`VITALIS_TIMEZONE`, currently `Asia/Shanghai`). Sleep
clock times use the vendor-provided offset, and workout summaries are assigned to the
local date on which the session started.

## 3. Intelligence Layer

The implementation lives in `vitalis/intelligence`:

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Versioned Daily/Weekly, trend, event, feedback, explanation, and context contracts |
| `profile.py` | One-local-user loader, provenance, target-day facts, and deterministic quality flags |
| `baseline.py` | Device/metric-specific 7-day and 28-day robust statistics |
| `analyzers.py` | Sleep, HRV/RHR, recovery, and training feature extraction |
| `decision.py` | Explainable training action policy with abstention |
| `trend.py` | Device-isolated 7/28/90-day trends and variability |
| `events.py` | Persistent deviations and period-change event detection |
| `weekly.py` | Weekly facts, inferences, and deterministic actions |
| `service.py` | Intelligence pipeline, snapshots, feedback, and query composition |

### 3.1 DailyProfile

The wire contract is `schema_version=2.0`, `model_version=vitalis-intelligence-2`:

```text
DailyProfile
|- data_quality: status, missing signals, coverage, flags, device validity
|- facts: target-day normalized observations and provenance
|- baselines: 7d/28d robust statistics per metric and device stream
|- features: sleep, HRV/RHR, recovery, exact workout modes, training
|- trends: device-isolated 7/28/90-day period features
|- events: persistent or period-change observations
|- states: sleep, recovery, training load, Chinese labels
|- decision: action, confidence, drivers, limitations, prescriptions, Chinese labels
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

### 3.4 Trends and Events

Trend streams retain metric, source, source scope, device ID, and unit. Each supported
7/28/90-day window reports the current and previous medians, percentage change when
available, daily slope, MAD variability, coverage, direction, and confidence. Missing
days are absent observations, never zeros. The profile loader reads 180 local days so
the current and preceding 90-day periods can both be evaluated.

Health events are deterministic observations, not diagnoses. HRV, RHR, and sleep
deviation events require persistence; training-load and activity events require an
explicit period change; recovery suppression requires the state engine's multi-signal
result. Stable event IDs support idempotent persistence and user-scoped acknowledgement.

### 3.5 WeeklyProfile and Feedback

WeeklyProfile covers the seven local days ending on the requested date and compares
them with the preceding seven days. Its contract separates wearable/aggregate `facts`,
Vitalis `inferences`, and deterministic `actions`. Recovery events take priority over
generic volume targets. Subjective RPE, physical fatigue, mental state, soreness, and
notes remain distinct from device facts and are never inferred when absent.

Daily and weekly generation persist versioned JSON snapshots idempotently by user,
profile type, period, and model version.

### 3.6 Feature and Decision Policy

The preferred HRV stream must have a target-day observation. Selection ranks usable
28-day coverage first, then metric preference (`RMSSD`, sleep HRV, SDNN). Every current
device stream for the selected metric remains visible with its own baseline deviation;
deterministic selection does not claim that one device is more accurate. RHR is paired
conservatively and is not substituted into an HRV baseline.

Recovery requires at least two baseline-interpretable signals from HRV, RHR, sleep,
and recent training load. Multi-signal suppression can produce `RECOVERY` or `REST`;
good recovery plus low load can produce `TRAIN_HARD`. Every decision returns rule IDs,
drivers, limitations, and one action:

```text
TRAIN_HARD | TRAIN_NORMAL | TRAIN_LIGHT | RECOVERY | REST | INSUFFICIENT_DATA
```

The engine detects deviations and guides training; it does not diagnose disease.
Sleep stages remain trend-only. Training load is vendor-derived; recorded RPE augments
weekly context but does not replace load, completed sets/reps/weight, or reliable
individualized aerobic-intensity classification.

Training content is deterministic engine output, not model-generated advice. The
current prescription library includes:

- Zone 2 running: warm-up, talk-test-controlled main work, cool-down, progression, and
  stop conditions. Until an individual heart-rate zone is validated, no numeric Zone 2
  range is invented.
- Full-body resistance: squat, push, pull, hip extension, and core patterns with sets,
  repetitions, rest, repetitions-in-reserve, substitutions, and load progression.
- Recovery activity and full rest with explicit intensity constraints.

When both aerobic and strength targets are due, the decision marks the returned
prescriptions as alternatives rather than a combined session. Strength is not selected
when a recorded strength workout occurred within the previous two local days.

## 4. API and Assistant

The Health Intelligence API is:

```text
GET  /api/v1/intelligence/daily
GET  /api/v1/intelligence/weekly
GET  /api/v1/intelligence/trends
GET  /api/v1/intelligence/events
GET  /api/v1/intelligence/explain
GET  /api/v1/intelligence/context
POST /api/v1/intelligence/feedback
GET  /api/v1/intelligence/feedback
POST /api/v1/intelligence/events/{event_id}/acknowledge
```

Every endpoint requires `X-User-Id`. The old `/intelligence/daily-profile` route was
removed rather than retained as a compatibility alias because the system is still
pre-production.

`/api/v1/health/*` remains the raw/summarized data and synchronization surface. The
prototype `/api/v1/health/today` and `/api/v1/analyze` paths were removed because the
application is pre-production and no compatibility contract is required.

`skills/vitalis` exposes Read tools for daily, weekly, trends, events, and context;
Analyze uses the explanation endpoint; Act covers synchronization, subjective feedback,
and event acknowledgement. Every user-facing value comes from Chinese labels or
structured prescription fields. Hermes never derives weekly results from daily calls.

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

Implemented: versioned DailyProfile and WeeklyProfile, deterministic quality/provenance,
device-isolated 7/28-day baselines, 7/28/90-day trends, persistent health events,
analysis snapshots, subjective feedback, sleep/HRV/recovery/training features, explainable decisions,
local-day handling, 122 public workout IDs with explicit unknown handling, Chinese
presentation contracts, structured running/strength prescriptions, Morning/Evening
pushes, and thin Hermes workflows. User-scoped APIs and Hermes tools require an
explicit identity.

Not implemented: 60/90-day personal correlations, training-response modeling,
minute-level stress load, Energy Dynamics, body composition/BP intelligence, forecasts,
or medical alerts. These require explicit new data contracts and validation rather
than LLM inference.
