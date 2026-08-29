# Vitalis API

All paths below are prefixed with `/api/v1`. User-scoped endpoints require an explicit
`X-User-Id` header; there is no implicit default user. Intelligence GET requests are
side-effect free and return `404` when the requested snapshot has not been generated.

## Connect and Import

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/connect/zepp/scan?user=...` | Open the browser-extension pairing page |
| POST | `/connect/zepp/pair` | Create a one-time browser pairing session |
| POST | `/connect/zepp/link/credentials` | Update credentials through an authenticated browser link |
| POST | `/connect/zepp/link/validate` | Validate a saved credential when browser cookies are temporarily unavailable |
| POST | `/connect/zepp/link/disconnected` | Report an official browser logout |
| POST | `/connect/zepp/device-link` | Create a Balance 2 upload token |
| POST | `/connect/zepp/device-link/heart-rate` | Receive a device-side heart-rate batch |
| GET | `/connect/zepp/token` | Read connection, renewal, and re-login state |
| POST | `/connect/zepp` | Connect and synchronize up to 730 days |

See [ZEPP_INTEGRATION.md](ZEPP_INTEGRATION.md) for authentication, security, and source
semantics.

## Health Intelligence

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/intelligence/analyze?day=YYYY-MM-DD` | Run deterministic analysis and persist immutable snapshots |
| GET | `/intelligence/daily?day=YYYY-MM-DD` | Read DailyProfile facts, baselines, states, and decision |
| GET | `/intelligence/weekly?day=YYYY-MM-DD` | Read the 7-day profile and prior-week comparison |
| GET | `/intelligence/monthly?day=YYYY-MM-DD` | Read the directly computed 28-day profile |
| GET | `/intelligence/trends?day=YYYY-MM-DD` | Read device-isolated 7/28/90-day trends |
| GET | `/intelligence/events?start=&end=&event_type=` | Read health-event lifecycle state |
| GET | `/intelligence/explain?day=YYYY-MM-DD` | Read fact → inference → action decision evidence |
| GET | `/intelligence/context?day=YYYY-MM-DD` | Read bounded Current/Recent/Trend/Personal agent context |
| GET | `/intelligence/training-responses?day=YYYY-MM-DD` | Read T+1/T+2/T+3 post-workout responses |
| GET | `/intelligence/personal-model?day=YYYY-MM-DD` | Read baselines, response distributions, and supported associations |
| GET | `/intelligence/personal-associations?day=YYYY-MM-DD` | Read 60/90-day association evaluations |
| GET | `/intelligence/timeline?start=&end=&limit=` | Read typed health timeline summaries |
| GET | `/intelligence/recommendations/{id}` | Read one recommendation instance |
| POST | `/intelligence/recommendations/{id}/complete` | Explicitly link a recommendation to a completed workout |
| POST | `/intelligence/feedback` | Record RPE, fatigue, mental state, soreness, or notes |
| GET | `/intelligence/feedback?start=&end=` | Read subjective feedback |
| GET | `/intelligence/training-preferences` | Read health-first running/strength targets and constraints |
| PUT | `/intelligence/training-preferences` | Replace weekly availability, experience, equipment, and pain/injury state |
| POST | `/intelligence/workouts/{workout_id}/strength-exercises` | Replace confirmed exercises and session focus for a user-owned strength workout |
| POST | `/intelligence/events/{id}/acknowledge` | Acknowledge a user-scoped event |

## Health Data and Synchronization

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/health/sync?days=7` | Run an incremental source synchronization |
| GET | `/health/token-status` | Read credential state and next synchronization time |
| GET | `/health/range?from=&to=&granularity=` | Read 180d/90d/30d/7d/1d aggregate blocks |
| GET | `/health/workouts?from=&to=` | List workout summaries and detail availability |
| GET | `/health/workouts/{workout_id}` | Read current v2 workout detail and ordered typed metric samples |
| GET | `/health/metrics/{metric}?from=&to=` | Read timestamped measurements and provenance |
| GET | `/health/daily-metrics?metric=&from=&to=` | Read sparse daily metrics |
| GET | `/health/dense-files/second_heart_rate?from=&to=` | Read high-frequency file coverage without file IDs |

## Examples

Synchronize and then explicitly analyze:

```bash
curl -X POST 'http://localhost:8000/api/v1/health/sync?days=7' \
  -H 'X-User-Id: <local-user-id>'

curl -X POST 'http://localhost:8000/api/v1/intelligence/analyze?day=2026-08-28' \
  -H 'X-User-Id: <local-user-id>'
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
    "workout_id": "<workout-id>",
    "session_rpe": 7,
    "physical_fatigue": 3
  }'
```

Confirm the exercises in one strength workout. This replaces that workout's current
exercise list and does not infer old records:

```bash
curl -X POST 'http://localhost:8000/api/v1/intelligence/workouts/<workout-id>/strength-exercises' \
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

- `POST /intelligence/analyze` is the calculation command; GET endpoints do not run or
  mutate analysis.
- Daily, Weekly, Monthly, Training Response, Personal Association, and Personal Model
  snapshots share one AnalysisRun identity.
- Facts, inferences, and actions remain distinct in period profiles.
- Missing measurements remain null or produce explicit insufficient-data state.
- Workout-detail samples use `metric`, `value`, and `unit`; the removed heart-rate-only
  sample shape is not retained as an alias.
- Exact strength exercises come only from explicit vendor sets or user confirmation.
  Heart rate can estimate work/rest structure but not exercise identity or load.
- `decision.action_plan` contains one primary session and at most one optional addition
  or alternative. It includes 7/28-day balance, safety state, conflict checks, evidence,
  dose, stop conditions, missing-input gates, and local-day expiry. Removed generic
  prescription-list fields are not retained.
- Internal enum codes are for program control; Chinese `*_label` fields are the
  presentation contract.
- Association responses are observational and always carry `association_only=true`.

Full Pydantic models live in `vitalis/intelligence/contracts.py`. Hermes-facing wire
schemas live in `skills/vitalis/schemas/`. Calculation policies are documented in
[ARCHITECTURE.md](ARCHITECTURE.md).
