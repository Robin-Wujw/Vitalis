# Vitalis API

[简体中文](API.md)

All paths below are prefixed with `/api/v1`. User-scoped endpoints require an explicit
`X-User-Id` header; there is no implicit default user. Intelligence GET requests are
side-effect free and return `404` when the requested snapshot has not been generated.

`report_context` in DailyProfile 12.0 and WeeklyProfile 5.0 retains `as_of` (ISO UTC),
`timezone`, `target_date`, `target_day_complete`, `training_history`, and
`latest_observations`. `training_history` contains `status` (`COMPLETE`/`PARTIAL`/`UNKNOWN`),
`verified_days`, `last_synced_at`, and `prior_7d_verified`; it explains data boundaries and
never turns unknown training days into zero or rest.

## Connect and Import

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/connect/zepp/scan?user=...` | Open the browser-extension pairing page |
| POST | `/connect/zepp/pair` | Create a one-time browser pairing session |
| POST | `/connect/zepp/link/credentials` | Update credentials through an authenticated browser link |
| POST | `/connect/zepp/link/validate` | Validate a saved credential when browser cookies are temporarily unavailable |
| POST | `/connect/zepp/link/disconnected` | Report an official browser logout |
| POST | `/connect/zepp/token` | Import a real Zepp `userid` plus `apptoken` |
| POST | `/connect/zepp/device-link` | Create a Balance 2 upload token |
| POST | `/connect/zepp/device-link/heart-rate` | Settle a device heart-rate batch per sample ID |
| GET | `/connect/zepp/token` | Read connection, renewal, and re-login state |
| POST | `/connect/zepp` | Connect and synchronize up to 730 days |

See [ZEPP_INTEGRATION.en.md](ZEPP_INTEGRATION.en.md) for authentication, security, and source
semantics. Manual import, initial pairing, and browser-link renewal return HTTP `409`
when the Zepp vendor identity already belongs to another local user; the existing
credential and both users' historical records remain unchanged.

## Health Intelligence

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/intelligence/analyze?day=YYYY-MM-DD` | Run deterministic analysis and persist immutable snapshots |
| GET | `/intelligence/profile` | Read the revisioned user-confirmed physiology and sleep profile |
| PATCH | `/intelligence/profile` | Patch explicit profile fields with `expected_revision` conflict protection |
| GET | `/intelligence/daily?day=YYYY-MM-DD` | Read DailyProfile 12.0 facts, persisted decision evidence, and shadow-only open health insights |
| GET | `/intelligence/morning-briefing?day=YYYY-MM-DD` | Read the MorningBriefing schema_version=2.0 morning presentation projection |
| GET | `/intelligence/weekly?day=YYYY-MM-DD` | Read the rolling 7-day WeeklyProfile 5.0 and prior-week comparison |
| GET | `/intelligence/monthly?day=YYYY-MM-DD` | Read the directly computed 28-day profile |
| GET | `/intelligence/trends?day=YYYY-MM-DD` | Read device-isolated 7/28/90-day trends |
| GET | `/intelligence/events?start=&end=&event_type=` | Read health-event lifecycle state |
| GET | `/intelligence/explain?day=YYYY-MM-DD` | Read one persisted decision explanation, including snapshot provenance and data quality |
| GET | `/intelligence/context?day=YYYY-MM-DD` | Read bounded Agent Context 6.0 Current/Recent/Trend/Personal context |
| GET | `/intelligence/training-responses?day=YYYY-MM-DD` | Read T+1/T+2/T+3 post-workout responses |
| GET | `/intelligence/personal-model?day=YYYY-MM-DD` | Read baselines, response distributions, and supported associations |
| GET | `/intelligence/personal-associations?day=YYYY-MM-DD` | Read 60/90-day association evaluations |
| GET | `/intelligence/timeline?start=&end=&limit=` | Read typed health timeline summaries |
| GET | `/intelligence/recommendations/{id}` | Read one recommendation instance |
| POST | `/intelligence/recommendations/{id}/complete` | Link a recommendation using `workout_source` plus `workout_id` |
| POST | `/intelligence/feedback` | Record feedback; workout-linked input requires `workout_source` plus `workout_id` |
| GET | `/intelligence/feedback?start=&end=` | Read subjective feedback |
| GET | `/intelligence/training-preferences` | Read health-first running/strength targets, rotation, weather fallback, and constraints |
| PUT | `/intelligence/training-preferences` | Replace the complete training-preference document |
| PATCH | `/intelligence/training-preferences` | Update only explicitly provided training-preference fields |
| POST | `/intelligence/workouts/{workout_id}/strength-exercises?source=` | Replace confirmed exercises for one source-qualified strength workout |
| POST | `/intelligence/events/{id}/acknowledge` | Acknowledge a user-scoped event |

## Health Data and Synchronization

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/health/sync?days=7` | Create or reuse a durable incremental synchronization attempt |
| GET | `/health/sync/{attempt_id}` | Read user-scoped attempt, aggregate progress, retries, and chunk outcomes |
| POST | `/health/sync/{attempt_id}/cancel` | Persist a cancellation request; queued or expired work is finalized immediately |
| GET | `/health/data-health` | Read latest attempt plus per-stream fetch, parse, write, error, and sample freshness state |
| GET | `/health/token-status` | Read credential state and next synchronization time |
| GET | `/health/range?from=&to=&granularity=` | Read 180d/90d/30d/7d/1d aggregate blocks |
| GET | `/health/workouts?from=&to=` | List workout summaries and detail availability |
| GET | `/health/workouts/{workout_id}?source=` | Read current v4.0 workout detail and source-isolated typed samples |
| GET | `/health/metrics/{metric}?from=&to=&resolution=` | Read timestamped measurements; raw/hour/day points retain source, scope, device, and unit |
| GET | `/health/daily-metrics?metric=&from=&to=` | Read sparse daily metrics with source provenance |
| GET | `/health/dense-files/second_heart_rate?from=&to=` | Read high-frequency file coverage without file IDs |
| POST | `/health/sync?days=&decode_dense_files=false` | Sync health data; dense archives are index-only unless one-file decoding is explicitly enabled |

`GET /health/metrics/stress?resolution=raw` returns the vendor's timestamped
`all_day_stress.data` observations with source, scope, device, unit, and explicit gaps.
Daily average, min/max, and category proportions remain available separately through
`GET /health/daily-metrics`; the API does not derive category thresholds or fill missing
stress intervals.

Attempts use local-date windows, so repeated equivalent requests reuse an active ledger
entry instead of differing by request-time seconds. Public status omits lease tokens,
internal stages, and raw vendor errors. `retry_wait` retains completed chunks and the next
retry timestamp; timeouts and 5xx responses are retryable; `needs_reauth` is reserved for
classified authentication rejection and blocks further delivery; `partial` is determined per
data-stream domain and includes mixed success/unavailable coverage or an explicit caller deadline.
CLI and built-in delivery entry points share the user/date lock marker. Cancellation is idempotent
and survives process restart. A manual request advances at
most `SYNC_DISPATCHER_BATCH_CHUNKS` chunks before returning; a `queued` response is normal
for larger windows, and the lifespan-owned dispatcher continues the same attempt.

## Examples

Synchronize and then explicitly analyze:

```bash
curl -X POST 'http://localhost:8000/api/v1/health/sync?days=7' \
  -H 'X-User-Id: <local-user-id>'

curl -X POST 'http://localhost:8000/api/v1/intelligence/analyze?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'
```

Store user-confirmed physiology inputs. These values take precedence over workout observations or device-zone candidates:

```bash
curl -X PATCH 'http://localhost:8000/api/v1/intelligence/profile' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "expected_revision": 0,
    "sex": "MALE",
    "confirmed_hrmax_bpm": 190
  }'
```

Read daily, weekly, and monthly intelligence:

```bash
curl 'http://localhost:8000/api/v1/intelligence/daily?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'

curl 'http://localhost:8000/api/v1/intelligence/weekly?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'

curl 'http://localhost:8000/api/v1/intelligence/monthly?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'
```

Record post-workout feedback. Session RPE requires a real workout ID:

```bash
curl -X POST 'http://localhost:8000/api/v1/intelligence/feedback' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "date": "2026-08-28",
    "workout_source": "zepp",
    "workout_id": "<workout-id>",
    "session_rpe": 7,
    "physical_fatigue": 3
  }'
```

Confirm the exercises in one strength workout. This replaces that workout's current
exercise list and does not infer old records:

```bash
curl -X POST 'http://localhost:8000/api/v1/intelligence/workouts/<workout-id>/strength-exercises?source=zepp' \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: <local-user-id>' \
  -d '{
    "session_focus": "PUSH",
    "exercises": [{
      "exercise_name": "卧推",
      "sets": 4,
      "repetitions": "8",
      "weight_kg": 60,
      "rir": 2,
      "rest_seconds": 120
    }]
  }'
```

## Contract Boundaries

- `GET /intelligence/morning-briefing` returns the `MorningBriefing schema=2.0`
  `observations`, `key_reasons`, `cautions`, and `report_context`; having no workout today
  is not a missing item.
- `POST /intelligence/analyze` is the calculation command; GET endpoints do not run or
  mutate analysis. `GET /intelligence/explain` returns 404 when no snapshot exists; a
  Hermes explanation must report that state without synchronizing or analyzing.
- Daily, Weekly, Monthly, Training Response, Personal Association, and Personal Model
  snapshots share one AnalysisRun identity.
- Facts, inferences, and actions remain distinct in period profiles.
- `open_health_insights` is shadow-only. It may explain readiness, sleep, TRIMP, ATL, CTL, and TSB, but it does not change Decision Policy 8.0, recovery state, action, rule IDs, or ActionPlan.
- User-confirmed profile values have revisioned provenance. Age formulas, workout maximum heart rate, Zepp scores, and device-zone boundaries never silently populate confirmed HRmax.
- Missing measurements remain null or produce explicit insufficient-data/refusal state.
- Canonical workout identity is `(source, workout_id)`. Detail reads, recommendation
  completion, feedback, strength confirmation, timeline references, and analysis outputs
  retain both fields.
- Workout-detail samples use `metric`, `value`, and `unit`; the removed heart-rate-only
  sample shape is not retained as an alias.
- Metric `1h` and `1d` aggregation never combines different source, source scope, device,
  or unit streams. Daily buckets use `VITALIS_TIMEZONE`, not UTC calendar dates, and
  aggregate queries stream the complete range rather than applying the raw 50,000-row cap.
- Training-response overlap identities use `source:workout_id`; comparable-run baselines
  return parallel workout source and ID arrays.
- Exact strength exercises come only from explicit vendor sets or user confirmation.
  Heart rate can estimate work/rest structure but not exercise identity or load. `strengthSets`
  accepts a string or list, and cross-layer handling converts integer `reps` to a string;
  same-dose sets with different weight/repetition values are preserved. Unknown units remain
  unknown and `kg` is not added. Local whole-session confirmation takes precedence; recent
  28-day detail is refreshed within a bounded budget of at most 4, with refresh time in
  `fetched_at`, and one pass is not guaranteed to cover all. Unverified real cloud detail is
  not claimed as obtained.
- `decision.action_plan` contains one primary session and at most one optional addition
  or alternative. It includes 7/28-day balance, safety state, conflict checks, evidence,
  dose, stop conditions, missing-input gates, and local-day expiry. Removed generic
  prescription-list fields are not retained.
- Internal enum codes are for program control; Chinese `*_label` fields are the
  presentation contract.
- Association responses are observational and always carry `association_only=true`.

Full Pydantic models live in `vitalis/intelligence/contracts.py`. Hermes-facing wire
schemas live in `skills/vitalis/schemas/`. Calculation policies are documented in
[ARCHITECTURE.en.md](ARCHITECTURE.en.md).
