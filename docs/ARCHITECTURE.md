# Vitalis Health Intelligence Architecture v7.0

## 1. System Boundary

Vitalis has three layers. Only the Intelligence layer computes health state or training
decisions.

```text
Assistant
  Hermes Skill: Read / Analyze / Act + Chinese rendering
                              |
Intelligence                  v
  Sync -> Analyze command -> immutable AnalysisRun / snapshots
                              |
  Quality -> Baseline -> Features -> Trends -> Event Lifecycle -> Monthly
                              |
  Concurrent Planner -> Recommendation -> Workout -> Response -> Association -> Personal Model
                              |
Data                          v
  Zepp connector -> normalized records -> SQLite/PostgreSQL
```

Hermes is not an analysis engine. It may select and phrase fields from structured
Vitalis responses, but cannot calculate trends, weekly aggregates, scores, thresholds,
training responses, recovery time, monthly aggregates, correlations, personal patterns,
confidence, or replacement advice.

## 2. Data Layer

`vitalis/connectors` authenticates, synchronizes, and normalizes vendor payloads. The
analysis layer reads only normalized records:

- Daily records: sleep, activity, and training.
- `metric_samples`: timestamped HR, RMSSD, SDNN, sleep HRV/RHR, readiness components,
  Charge, SpO2, and other supported measurements.
- `daily_metrics`: sparse vendor facts such as readiness, stress, respiratory rate,
  PAI, ODI, and lactate-threshold fields.
- `workouts` and `workout_metric_samples`: normalized workout summaries, versioned
  detail metadata, device zone boundaries, and typed workout HR, speed, equivalent
  pace, cadence, stride length, distance, altitude, running power, ground-contact time, vertical
  oscillation, and vertical-stride-ratio observations.
- `dense_data_files`: per-device `SEC_HR` file indexes, decode state, and sample count.
  Decoded second-level values are stored as ordinary device-scoped `heart_rate`
  metric samples.

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

`workouts` is the canonical training fact table. A Zepp sport page only upserts canonical
workout identities; every affected local day is then rebuilt from all stored workouts in
that configured-timezone interval. `training_records` is therefore a derived daily
summary, never a page-level accumulator. Because it fuses every canonical workout source,
its analysis provenance is `canonical_workouts`, not a vendor name. A corrected workout
timestamp rebuilds both its old and new local dates.

Normalized stream identity includes provenance. Timestamped metrics use `(user, source,
metric, timestamp, source_scope, device_id)`; sparse daily metrics use `(user, source,
date, metric, source_scope, device_id)`. Workout-detail samples also include canonical
workout source in their identity, so equal vendor workout IDs from different connectors
cannot delete or merge one another's measurements. The same `(source, workout_id)` key is
used by detail APIs, ProfileLoader, feedback, recommendations, strength confirmation,
training response, and timeline references. A missing device is stored internally
as an empty non-null identity so SQLite and PostgreSQL apply the same uniqueness
semantics, but API and intelligence boundaries expose it as absent rather than as a
device name.

Zepp failures carry a machine-readable kind. Only an explicit `not_available` response
may be treated as an unsupported optional capability; authentication, network, service,
and vendor-response failures remain failures. Empty successful cloud responses and
non-empty unrecognized payloads keep distinct fetch/parse/write states.

Workout detail is current-contract-only (`schema_version=4.0`). Rows from older detail
contracts are fetched and replaced in bounded batches during subsequent synchronization
windows so a historical upgrade cannot consume the whole health-sync budget. Zepp delta/time series
are decoded into typed metric samples. Laps retain only verified index, duration, and
distance semantics; pauses retain start and duration. Running analysis derives moving
time and per-kilometre pace, heart rate, and elevation from these normalized streams.
Explicit vendor strength sets may
carry exercise identity, repetitions, weight, work duration, and rest. Empty or
undocumented vendor fields remain absent, and strength assessment payloads are not
reinterpreted as exercise names.

## 3. Intelligence Layer

The implementation lives in `vitalis/intelligence`:

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Versioned analysis, recommendation, response, personal-model, timeline, and context contracts |
| `profile.py` | One-local-user loader, provenance, target-day facts, and deterministic quality flags |
| `baseline.py` | Device/metric-specific 7-day and 28-day robust statistics |
| `analyzers.py` | Sleep, HRV/RHR, recovery, and training feature extraction |
| `running.py` | Device/threshold zones, cadence, power, running dynamics, comparable-run baselines, pace/HR drift, segments, session type, and 7/28-day structure |
| `strength.py` | Confirmed exercises, movement and muscle knowledge, work/rest structure, split state, and muscle recovery context |
| `decision.py` | Health-first running/strength planner, safety gates, scheduling conflicts, and abstention |
| `trend.py` | Device-isolated 7/28/90-day trends and variability |
| `events.py` | Persistent deviations and period-change event detection |
| `lifecycle.py` | Event observations and DETECTED/PERSISTING/IMPROVING/RESOLVED transitions |
| `weekly.py` | Weekly facts, inferences, and deterministic actions |
| `monthly.py` | Direct 28-day facts, inferences, comparison, and actions |
| `association.py` | Device-isolated 60/90-day Spearman personal associations |
| `training_response.py` | Device-isolated pre-workout baseline and T+1/T+2/T+3 response analysis |
| `personal.py` | Robust per-family and per-mode personal response distributions |
| `context.py` | Bounded Current/Recent/Trend/Personal agent context |
| `timeline.py` | Typed summary timeline without raw samples |
| `service.py` | Explicit commands, read-only queries, user actions, and immutable snapshots |

### 3.1 DailyProfile

The wire contract is `schema_version=10.0`. Every result carries `analysis_run_id`,
`intelligence_version`, `decision_policy_version`, and `evidence_version` separately:

```text
DailyProfile
|- data_quality: status, missing signals, coverage, flags, device validity
|- facts: target-day normalized observations and provenance
|- baselines: 7d/28d robust statistics per metric and device stream
|- features: sleep, device-baseline HRV fusion, dense-HR coverage, RHR, recovery, training
|- trends: device-isolated 7/28/90-day period features
|- events: persistent or period-change observations
|- states: sleep, recovery, training load, Chinese labels
|- decision: state action, evidence, and a dated concurrent ActionPlan
|- evidence_refs
`- metadata: identity and product-policy versions
```

There is no uncalibrated Vitalis 0-100 recovery score. Vendor readiness, Charge, and
sleep scores are labeled as vendor context and do not become the Vitalis result.

#### 3.1.1 Open Health shadow insights

DailyProfile 10.0, WeeklyProfile 4.0, MonthlyProfile 2.0, and Agent Context 5.0 expose a versioned `open_health_insights` block. It contains transparent personal-baseline readiness, robust multi-signal anomaly screening, sleep efficiency/regularity, user-target sleep gaps, Banister TRIMP, and descriptive ATL/CTL/TSB.

These outputs are strictly `shadow_only=true`. They never enter `RecoveryFeatures.state`, `DecisionEngine`, decision confidence, rule IDs, or ActionPlan. The current decision policy remains 7.0. A future policy must explicitly version and test any use of these signals.

User-confirmed physiology is stored in a revisioned `UserProfile`. `sex` and confirmed HRmax are never inferred from age, workout maximum heart rate, vendor readiness, lactate threshold, or device-zone boundaries. Missing fields become typed Agent Context questions. Current Zepp apptoken synchronization does not call an unverified cloud profile endpoint.

Open load uses one source/device workout-heart-rate stream, same-day canonical RHR, and user-confirmed HRmax. Calendar days without verified upstream sync coverage remain a lower-bound estimate; unknown workout days are never converted to zero-load rest days. TSB is presented as descriptive load balance, not recovery, form, overtraining, or injury risk.

HRV handling never averages raw milliseconds across devices. Vitalis selects one
canonical same-metric stream using an interpretable personal baseline, baseline-day
coverage, current observation density, and continuity; measurement site is only a final
tie-breaker for nocturnal HRV. The selected stream is compared only with its own robust
28-day baseline. Secondary streams remain audit evidence: agreement is silent and does
not inflate confidence, while comparable disagreement lowers confidence without
replacing the canonical conclusion. This differs from exercise heart rate, where an
explicitly attributed and decoded upper-arm stream has stronger form-factor evidence.

`second_heart_rate` uses Zepp's verified index-to-file contract. Vitalis resolves each
new file ID to a signed HTTPS URL, downloads the small ZIP without forwarding the Zepp
token, decodes the `DailySecondHeartBeat` protobuf, maps archive entries to indexed
devices by a global one-to-one interval-overlap assignment, and stores valid values as
second-level heart rate. A decoded file/device/time interval is skipped on later syncs.
Nightly analysis reads only recorded sleep windows and reduces each device to one
median value per minute before feature extraction; it does not retain millions of raw
points in an analysis profile.

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
result. Stable event identities advance through `DETECTED`, `PERSISTING`, `IMPROVING`,
and `RESOLVED`. Every analysis writes an immutable EventObservation. Acknowledgement is
an independent interaction timestamp and never changes physiological lifecycle state.

### 3.5 WeeklyProfile and Feedback

WeeklyProfile covers the seven local days ending on the requested date and compares
them with the preceding seven days. Its contract separates wearable/aggregate `facts`,
Vitalis `inferences`, and deterministic `actions`. Recovery events take priority over
generic volume targets. Subjective RPE, physical fatigue, mental state, soreness, and
notes remain distinct from device facts and are never inferred when absent.

An explicit analysis command persists one immutable AnalysisRun and immutable Daily,
Weekly, Monthly, Training Response, Personal Association, and Personal Model snapshots.
Repeated analysis creates new runs rather than overwriting prior output. GET requests
only read the most recent persisted result for the requested date and return 404 when
none exists.

### 3.6 MonthlyProfile and Personal Associations

MonthlyProfile covers exactly 28 local days ending on the requested date and compares
them with the immediately preceding 28 days. It is recomputed from normalized sleep,
activity, training, workout, metric-stream, feedback, trend, and event data. It is not
assembled from WeeklyProfile snapshots. Facts, inferences, and actions remain separate.

The Personal Association Engine evaluates fixed, documented candidate pairs across
60- and 90-day windows. Sleep duration and training load are paired with next-day HRV,
RHR, and sleep; steps are paired with same-day sleep. Each measurement stream retains
metric, source, source scope, device ID, and unit. Missing days are excluded pairwise,
never zero-filled. Spearman ranks use deterministic average ranks for ties.

A 60-day association requires at least 30 paired days; a 90-day association requires
45. Both require at least 50% coverage and meaningful variation in predictor and
outcome. Training on an outcome day is counted as a potential confounder and can lower
confidence. Strength bands (`<0.2`, `0.2-0.4`, `0.4-0.6`, `>=0.6`) are product display
policy. Every result has `association_only=true`; it is descriptive and never causal.

### 3.7 Recommendation, Training Response, and Personal Model

Every Daily decision has a RecommendationInstance tied to its AnalysisRun. Completion
requires an explicit user-scoped link to one stored workout; Vitalis never infers it
from timestamps, text, or workout type. Session RPE requires a workout identity, and a
recommendation-aware feedback record must match the completed link.

Training Response v1 compares each eligible workout with device-isolated 28-day
pre-workout baselines and T+1/T+2/T+3 HRV, RHR, sleep, and linked subjective facts.
Future/missing windows stay explicit. Any other workout in the response window marks
the result confounded. Recovery hours are emitted only when HRV has returned near/above
baseline, RHR near/below baseline, available sleep is not below baseline, and the window
is not confounded. This is deterministic product policy, not a medical recovery model.

Personal Model v2 groups responses by training family and exact sport mode. It reports
median, MAD, sample count, eligible count, coverage, and coverage-derived confidence for
each device-specific response stream. It also includes only medium/high-confidence
personal associations. It contains no ML, synthetic stress score, or causal claim.

### 3.8 Timeline and Agent Context

Health Timeline projects typed summaries for analysis, recommendation, workout,
feedback, event transition, training response, monthly summary, and supported personal
association. It never copies raw sensor or workout samples. Agent Context 5.0 contains
only bounded Current, Recent, Trend, and Personal layers, with hard item caps and no
embedded Daily/Weekly/Monthly payloads.

### 3.9 Feature and Decision Policy

The preferred HRV stream must have a target-day observation. Selection ranks usable
28-day coverage first, then recovery-metric preference (nightly sleep HRV, sleep-oriented
SDNN, then all-day RMSSD only when the overnight metrics are absent). Every current
device stream for the selected metric remains visible with its own baseline deviation;
deterministic selection does not claim that one device is more accurate. RHR is paired
conservatively and is not substituted into an HRV baseline. All-day visualization has
its own RMSSD-only source policy and never changes the recovery metric.

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

Training content is deterministic engine output, not model-generated advice. Decision
Policy 7.0 returns an `ActionPlan` with one primary session and at most one optional
compatible addition or alternative. Every session includes dose, evidence, progression,
stop conditions, and a local-day expiry. The current session library includes:

- Zone 2 running: warm-up, talk-test-controlled main work, cool-down, progression, and
  stop conditions. Until an individual heart-rate zone is validated, no numeric Zone 2
  range is invented.
- Full-body resistance: squat, push, pull, hip extension, and core patterns with sets,
  repetitions, rest, repetitions-in-reserve, substitutions, and load progression.
- Recovery activity and full rest with explicit intensity constraints.

The planner's fixed product goal is health first with both running and strength
required. User constraints specify weekly targets, running/strength selection policy,
treadmill availability, bad-weather running fallback, available weekdays, session
time, experience, equipment, and pain/injury state. Missing constraints remain
explicit and reduce prescription specificity; recorded pain or injury blocks planned
training.

Running and strength completion are compared against both 7-day and 28-day targets.
When both are due, the larger relative deficit becomes primary; ties alternate from the
latest training family. Easy running and upper-body strength may be separate same-day
additions with at least six hours between them. Quality running and lower-body strength
are alternatives, and a quality/long run or leg session in the previous 48 hours blocks
another conflicting high-load lower-body session.

Users may instead select explicit running/strength alternation. In that mode, the most
recent recognized aerobic session selects strength next, and the most recent strength
session selects running next, even when weekly target deficits differ. Recovery,
availability, injury, and load-conflict gates still take precedence. Weather fallback
preferences are stored deterministically, but are not applied until a weather source is
configured; Vitalis never invents weather conditions.

### 3.10 Running Analysis

DailyProfile 10.0 embeds `TrainingFeatures.running` with Running Analysis v2. Each
session preserves distance, duration, derived and equivalent pace, median speed and cadence,
cadence variability, power, ground-contact time, vertical oscillation, vertical stride
ratio, HR-zone duration, cardiac drift, detected work/recovery segments, classification
evidence, confidence, and limitations.

Each workout's six valid `heart_range` boundaries take precedence. Without them, five
zones are calculated only when a personal vendor lactate-threshold HR is available,
using the verified Zepp threshold boundaries. Vitalis does not estimate maximum HR from
age. Cardiac drift requires at
least 20 minutes of overlapping speed/HR detail and is withheld when first/second-half
speed differs by more than 15%. Cadence is a personal observation; no universal
180-spm target is applied. Session types are recovery, easy, steady, tempo, interval,
long, or unclassified. These thresholds are versioned product policy rather than
medical or coaching truth.

When at least three earlier runs in the prior 180 days are within 20% of the target
distance, the session carries a baseline from up to ten recent comparable runs. Pace,
average HR, and power comparisons remain descriptive personal facts; they are not
automatically labeled fitness improvement or deterioration.

### 3.10.1 Synchronization Data Health

`sync_stream_states` records the latest fetch, parse, and write status independently
for each user-owned source stream, together with the latest stored sample timestamp.
An empty cloud response, an unrecognized non-empty payload, and a storage write are
different states. A synchronization is complete only when every stream is successful or
explicitly unavailable as a whole. Mixed successful/unavailable chunks are partial, and
optional network, service, parse, or authentication failures block overall success while
preserving earlier successful writes. `GET /health/data-health` exposes this diagnostic
contract without measurement values or private file identifiers.

The running prescription consumes those session facts. It chooses among recovery,
easy, steady, threshold, and long-easy sessions using recent hard-session timing,
lower-body conflicts, the latest interpretable cardiac drift, personal threshold HR,
and the median duration of recent completed runs. Duration is bounded around personal
history and the configured time limit. Recent cadence is repeated only as the runner's
natural observation, never as a universal cadence target.

### 3.11 Strength Analysis

DailyProfile 10.0 embeds `TrainingFeatures.strength` with Strength Analysis v1. A user
can confirm exercise name, set count, repetitions, load, RPE/RIR, rest, and session
focus against a user-owned strength workout. Vitalis normalizes known Chinese or
English exercise names to movement patterns and muscle groups while preserving the
original name as the auditable fact.

Explicit vendor sets and user-confirmed exercises are the only sources of exact
exercise identity. When they are absent, workout heart rate or verified zero-distance
laps may estimate work-bout count and work/rest duration, but cannot identify a squat,
bench press, or any target muscle. Strength heart-rate zones describe cardiovascular
context only and never represent load intensity.

The last 28 days support confidence-bounded full-body, upper/lower, push-pull-legs, and
five-day split detection. A next focus is returned only after a recognizable rotation
exists. Per-muscle recency and confirmed soreness/RPE remain separate observations;
missing exercise coverage leaves muscle volume and recovery incomplete.

The strength prescription first looks for the most recent explicit session matching
the next recognized focus. When found, it reuses exercise names, sets, repetitions,
load, and rest and lets recorded RPE/RIR decide whether to hold or allow a small
progression. Without explicit exercises it uses concrete movement-pattern choices;
heart-rate work/rest structure can size and explain the session but never supplies an
exercise identity. Planned sessions carry the exact evidence-reference IDs used by the
rule.

### 3.12 Nocturnal Recovery Context

DailyProfile 10.0 keeps timestamped ordinary heart rate separate from daily metric
series. For each sleep interval, the engine isolates device streams and requires at
least 120 covered minutes and 50% interval coverage. It derives the nightly median, a
rolling five-minute median low point, first- and second-half medians, and coverage.
The selected nightly stream is compared only with its own preceding 28-night history.

The canonical HRV stream also reports its latest seven-day median against the preceding
seven days. Minute and decoded second-level heart rate are never treated as
beat-to-beat HRV. When current-night coverage is sufficient, the nightly heart-rate
stream prefers the identified upper-arm device and compares it only with that device's
own history.

## 4. API and Assistant

The Health Intelligence API is:

```text
POST /api/v1/intelligence/analyze
GET  /api/v1/intelligence/daily
GET  /api/v1/intelligence/weekly
GET  /api/v1/intelligence/monthly
GET  /api/v1/intelligence/trends
GET  /api/v1/intelligence/events
GET  /api/v1/intelligence/explain
GET  /api/v1/intelligence/context
GET  /api/v1/intelligence/training-responses
GET  /api/v1/intelligence/personal-model
GET  /api/v1/intelligence/personal-associations
GET  /api/v1/intelligence/timeline
GET  /api/v1/intelligence/recommendations/{recommendation_id}
POST /api/v1/intelligence/recommendations/{recommendation_id}/complete
POST /api/v1/intelligence/feedback
GET  /api/v1/intelligence/feedback
GET  /api/v1/intelligence/training-preferences
PUT  /api/v1/intelligence/training-preferences
POST /api/v1/intelligence/workouts/{workout_id}/strength-exercises?source=
POST /api/v1/intelligence/events/{event_id}/acknowledge
```

Every endpoint requires `X-User-Id`. The old `/intelligence/daily-profile` route was
removed rather than retained as a compatibility alias because the system is still
pre-production.

`/api/v1/health/*` remains the raw/summarized data and synchronization surface. The
prototype `/api/v1/health/today` and `/api/v1/analyze` paths were removed because the
application is pre-production and no compatibility contract is required.

`skills/vitalis` exposes Read tools for persisted Daily, Weekly, Monthly, trends, events,
training responses, associations, Personal Model, timeline, and context. Analyze is an explicit POST
tool. Act covers synchronization, recommendation completion, subjective feedback, and
event acknowledgement. Every user-facing value comes from Chinese labels or structured
engine fields. Hermes never derives one intelligence contract from another.

## 5. Scheduled Flow

```text
02:00  sync 7 days -> analyze -> immutable snapshots
09:30-21:30 hourly  sync 2 days -> analyze today's DailyProfile
                     -> incomplete sleep: defer
                     -> complete sleep and unsent: Morning renderer -> PushPlus -> mark sent
22:30                sync 1 day -> analyze today's DailyProfile
                     -> Evening renderer -> PushPlus -> mark sent
```

The morning scheduler defers while today's sleep record has no wake time and never
substitutes stale health results. Morning and evening use separate per-user, per-date
delivery markers, so retries and overlapping invocations do not duplicate a successful
PushPlus delivery. The evening report is not blocked by the morning sleep gate.

The morning renderer is a deterministic presentation-selection layer over the complete
DailyProfile. It emits one conclusion, concrete primary/optional actions, short reasons,
at most one actionable event, and only consequential cautions. Per-device streams,
raw trend windows, empty signal groups, unknown safety inputs, passed checks, planning
gates, and generic limitations stay in the structured profile instead of being copied
into the daily push.

The evening renderer is a separate deterministic view. It reports today's actual
workout metrics when present, summarizes fused daily activity and device-recorded
stress, explains the recent completed-day training load, and closes with recovery and
next-day continuity actions. It does not turn a day without formal training into a
problem, infer readiness from load alone, or print low-confidence workout type and
cardiac-drift claims.

Both daily renderers produce sanitized HTML for PushPlus. The health content remains a
deterministic text contract; a presentation pass escapes raw HTML, converts the report
dialect, and adds conservative inline styles that remain readable when a delivery
channel strips styling. Daytime HRV interpretation is intentionally not part of the
daily contract until the activity, posture, respiration, circadian-reference, and
coverage gates in `RESEARCH_NOTES.md` can be satisfied.

`HrvFeatures` keeps Zepp's algorithms separate. The recovery value prefers the nightly
`sleepHRV` summary, then sleep-oriented SDNN when available. The evening PushPlus report
renders seven nightly `sleepHRV` values from the single device stream with the strongest
personal baseline; it does not merge device values. On the current account this selects
Balance 2, which has complete seven-night coverage and a 28-day baseline. Timestamped
`HRVRMSSD/real_data` remains available in the structured profile for inspection, but its
sleep-only timeline is not included in the evening summary because it adds no useful
all-day information. Neither sparse daytime RMSSD observations nor vendor stress are
relabeled as a continuous all-day HRV curve. Sparse SDNN remains stored but is not used
for the weekly display trend.

Training `load_7d`, duration, and comparison windows are rolling local-day windows ending
on the analysis date. The evening report therefore includes today's completed training;
the three comparison weeks are the immediately preceding non-overlapping seven-day
windows.

## 6. Evidence Boundaries

- WHO activity guidance supplies population-level weekly context, not readiness rules.
- HRV measurement standards support metric separation and lnRMSSD handling, not a
  proprietary recovery score.
- World Sleep Society recommendations bound consumer sleep-stage interpretation.
- AASM/SRS provides a general adult sleep-duration reference.
- IOC consensus supports integrated load/recovery/health monitoring, not a universal
  acute-to-chronic ratio threshold.
- ACSM's 2026 resistance-training position stand supports progressive
  major-muscle-group resistance training and adjustable prescription variables, but
  not automatic absolute weights without individual strength history.
- Concurrent-training evidence supports cautious aerobic/strength scheduling, while
  exact same-day spacing and 48-hour lower-body conflict windows remain Vitalis
  product policy.
- RPE/RIR evidence supports guided subjective effort feedback with experience-related
  limitations; it is not a precise replacement for load, sets, repetitions, or device
  observations.

No independent model-specific validation was found for Balance 2 or Helio Strap, so
their `device_validity.status` remains evidence-limited. Upper-arm exercise studies do
not establish Helio's nocturnal-HRV accuracy, and every-second manufacturer claims do
not become accuracy weights.

## 7. Current Scope

Implemented: immutable AnalysisRun snapshots, command/query separation, versioned
Daily/Weekly/Monthly, recommendation/workout/feedback identity, device-isolated Training
Response v1, Personal Model v2 robust distributions and supported personal associations, event lifecycle observations,
typed Timeline, bounded layered Context, deterministic quality/provenance, 7/28-day
baselines, 7/28/90-day trends, explainable decisions, 122 public workout IDs, Chinese
presentation contracts, running and strength workout analysis, structured action plans, scheduled analysis,
and thin Hermes Read/Analyze/Act tools.

Not implemented: unrestricted exploratory correlation discovery, minute-level stress load,
Energy Dynamics, body composition/BP intelligence, forecasts,
or medical alerts. These require explicit new contracts and validation rather than LLM
inference.
