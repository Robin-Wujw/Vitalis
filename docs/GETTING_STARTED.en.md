# Getting Started

[简体中文](GETTING_STARTED.md)

This guide covers local setup, service operation, deployment, scheduling, and
verification. Product positioning belongs in the root [README](../README.en.md); health
intelligence contracts and calculation boundaries belong in
[ARCHITECTURE.en.md](ARCHITECTURE.en.md).

## Requirements

- Python 3.11 or newer
- SQLite for local development, or PostgreSQL for a persistent deployment
- A browser-trusted HTTPS origin for real browser-extension pairing

## Local Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m vitalis.main
```

The default `ZEPP_MOCK=true` mode requires no vendor credentials and supplies
deterministic development data. The API is served at `http://127.0.0.1:8000`; FastAPI
documentation is available at `/docs`.

Important configuration:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLite or PostgreSQL connection string |
| `ZEPP_MOCK` | Use the deterministic development connector when `true` |
| `VITALIS_TIMEZONE` | Local-day boundary used by intelligence calculations |
| `HOST` / `PORT` | Application listener |
| `VITALIS_PUBLIC_URL` | Public HTTPS origin used by pairing pages |
| `ZEPP_PAIRING_PROCESSING_LEASE_SECONDS` | Time before an interrupted pairing submission can be reclaimed |
| `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` | Nightly synchronization enqueue time |
| `SYNC_DISPATCHER_INTERVAL_SECONDS` | Interval between durable-ledger dispatcher passes |
| `SYNC_DISPATCHER_BATCH_CHUNKS` | Maximum chunks processed per fair dispatcher pass |
| `SYNC_LEASE_SECONDS` / `SYNC_ATTEMPT_LEASE_SECONDS` | Chunk and attempt fencing lease durations |
| `VITALIS_NO_SCHEDULER` | Disable background jobs and retry recovery when set to `1` |

The deterministic analysis engine does not require an LLM. Hermes or another agent
consumes structured Vitalis results and renders them separately.

### Existing Database Identity Migration

Current schema requires one local owner for each non-null Zepp vendor identity. A fresh
database receives the unique indexes during `init_db()`. For a database created by an
older release, stop the API, worker, and scheduler and take a tested backup before
starting the new code. Audit the existing mappings without reading token values:

```bash
python -m vitalis.storage.identity_migration audit
```

If the report is not clean, use the explicit `resolve`, `resolve-local`,
`assign-missing`, `resolve-projection`, or `clear-projection` command documented in
[ZEPP_INTEGRATION.en.md](ZEPP_INTEGRATION.en.md#source-identity-ownership). After every
conflict has an operator-verified resolution, apply the migration:

```bash
python -m vitalis.storage.identity_migration migrate --apply
```

Startup fails with `SourceIdentityMigrationRequired` while duplicate mappings prevent
the unique indexes from being created. Do not bypass that check or delete a database to
hide the conflict. The migration preserves both users' historical health data and only
releases the non-canonical credential and browser link.

### Existing SQLite Schema Migration

`create_all()` creates a fresh current schema but does not alter existing SQLite tables.
Before starting newer code against a long-lived database, stop Vitalis processes and
audit columns, unique constraints, check constraints, and indexes:

```bash
python -m vitalis.storage.schema_migration audit
```

For the known pre-current layouts, run the explicit migration with the historical data
source and a required backup path:

```bash
python -m vitalis.storage.schema_migration migrate \
  --legacy-source zepp \
  --backup backups/vitalis-before-current-schema.db \
  --apply
```

The command creates and verifies the SQLite backup before changing tables. It rebuilds
drifted tables from the current SQLAlchemy metadata, maps known added columns, preserves
row counts, normalizes legacy null device IDs, recreates current indexes, and finishes
with schema and foreign-key checks. It refuses to reinterpret a non-empty legacy
`sync_attempts` or `sync_chunks` ledger. Unknown schema differences require a separate
review rather than a forced migration.

Run the audit again and start the API/worker only when it reports `clean=true`.

## Hermes Runtime

Keep the checked-in Skill as the single source of truth. On Linux, link it from the
repository root into Hermes' local discovery tree:

```bash
mkdir -p "$HOME/.hermes/skills/health"
ln -s "$PWD/skills/vitalis" "$HOME/.hermes/skills/health/vitalis"
```

For native Windows Hermes, use the active `HERMES_HOME` (typically
`%LOCALAPPDATA%\\hermes`) and add the repository's **parent** skills directory to
`config.yaml`, merging with any existing `skills` entries:

```yaml
skills:
  external_dirs:
    - D:/MyCodes/Vitalis/skills
```

This points at the source in the repository; do not copy the Skill or overwrite the
rest of the private Hermes configuration. Set the loopback API and the **chosen**
local Vitalis user in the same profile's private `.env` (not in git):

```dotenv
VITALIS_API=http://127.0.0.1:8000
VITALIS_USER=<local-user-id>
NO_PROXY=127.0.0.1,localhost
```

Start Vitalis with a schema-audited database and a loopback listener. Then use a new
Hermes session to verify skill discovery and exercise a read-only tool:

```bash
hermes skills list --source local --enabled-only
hermes --skills vitalis prompt-size --json
python skills/vitalis/tools/daily.py --user YOUR_LOCAL_USER_ID --date YYYY-MM-DD
hermes chat --skills vitalis -q "昨天的睡眠有哪些已记录的事实？"
```

A standalone shell does not load Hermes' private `.env`: replace `YOUR_LOCAL_USER_ID`
with the selected local user ID, or set `VITALIS_USER` and `VITALIS_API` in the current
shell. Run the tool command from the repository root with its Python environment active;
on native Windows, `D:/MyCodes/Vitalis/.venv/Scripts/python.exe` is an explicit
interpreter. A missing snapshot is reported as `status=snapshot_missing`; it must
not cause automatic sync, analysis, or a fallback to a different date. For today's
training advice use the persisted morning briefing; for its reasons use `explain`.
Hermes only routes and renders Vitalis' structured evidence; it must not calculate,
merge, diagnose, or invent a training plan. Keep `VITALIS_API` on loopback and do
not expose unauthenticated intelligence routes through a public reverse proxy:
`X-User-Id` selects an identity but does not authenticate the caller, including
other processes on the same machine. With `VITALIS_USER` set, tools reject a
model-supplied alternative `--user`. Hermes may retain conversations and send
necessary structured health summaries to its configured model provider; review
that provider before asking personal questions.

### Daily PushPlus Report

The local production setup may use Hermes Cron as the daily scheduling entry point, or
Vitalis' built-in scheduler; they are alternative entry points and must not both own delivery.
With Hermes, Vitalis runs as a loopback system service with its embedded scheduler disabled.
The Hermes Morning job runs hourly from 09:30 through 21:30 `Asia/Shanghai`, synchronizes
two days for the explicit `VITALIS_USER`, and analyzes only the current local day. It sends
nothing while today's sleep status is unavailable or `wake_time` is absent. The next hourly
run synchronizes and checks again; once sleep is complete, private state markers skip a
Morning delivery already recorded for that date. The Morning report shows returned sleep,
body-state, and available same-day running/strength context; the absence of a workout so far
today is not a missing item. It never substitutes yesterday's profile.

When sleep is complete but training history for the previous seven days is unverified, the
Morning report is facts-only: it shows only sleep and body state, explicitly discloses the
history gap, and contains no exercise, intensity, or weight prescription. Incomplete sleep,
expired dates, and invalid credentials still block delivery. A successful facts-only delivery
also writes the daily Morning deduplication marker, so subsequent hourly retries do not send
it again; test mode leaves the scheduled marker unchanged.

A separate Hermes job runs at 22:30, synchronizes one day, and sends an Evening report for
the current date. The Evening report reviews actual workout details in `started_at` order;
when returned, it shows running metrics, confirmed strength records, and ordered `observed_sets`,
giving confirmed records precedence and preserving the `source` literal, code, missing fields, and
unknown units without inventing `kg` or exercise names. It also reviews daily activity and stress,
a seven-night sleep-HRV trend, and rolling training load through today, then gives a practical
recovery action and leaves tomorrow's intensity to the next complete overnight assessment. Both
reports are sent with PushPlus' HTML template using portable inline styling; report
values are escaped before HTML generation. The HTML root has its own high-contrast light
background so PushPlus dark mode cannot place dark report text directly on black. The
Evening report does not show the sleep-only RMSSD curve or infer continuous daytime
HRV, stress, or emotion from sparse samples.

### Reading the Four Reports and Feedback

`GET /api/v1/intelligence/evening-briefing`, `GET /api/v1/intelligence/weekly-briefing`, and `GET /api/v1/intelligence/monthly-briefing` return `ReportBriefing 1.0` and use the same `sections` as the HTML renderer. They are explicit read capabilities, do not freely compose raw profiles, and do not add a cron job; the Monthly renderer runs only when explicitly called. Reports do not automatically request subjective feedback or prompt for RPE. When the user provides feedback explicitly, `tools/feedback.py add` remains available and existing feedback analysis is retained.

Add the PushPlus token to Hermes' private `~/.hermes/.env`:

```dotenv
PUSHPLUS_TOKEN=<private-pushplus-token>
```

The cron tool reads that private file at execution time, so adding or rotating the token
does not require a Gateway restart. Verify the persistent runtime and inspect delivery
history with:

```bash
systemctl status vitalis.service hermes-gateway.service
hermes cron status
hermes cron list
hermes cron runs <job-id>
```

To send a real manual test without reading or writing the scheduled delivery marker,
run the report tool with `--test`:

```bash
/root/Vitalis/.venv/bin/python /root/Vitalis/skills/vitalis/tools/daily_push.py \
  --period evening --test
```

For a report resent after the date has changed, add `--date YYYY-MM-DD` to
`--period evening --test`. Only the most recent 7 days are supported, and the tool
expands the synchronization window to cover that date. A resent report contains
historical facts only: it omits expired training prescriptions, tonight's recovery
advice, and tomorrow's transition advice, and leaves scheduled delivery markers
unchanged. Morning reports, non-test delivery, and future dates cannot use this mode.

Running `hermes cron run <job-id>` is an official scheduled invocation: a successful
delivery writes the daily marker and prevents that period from being sent twice.

The tool exits before synchronization if `VITALIS_USER` or `PUSHPLUS_TOKEN` is missing,
a user and token are not paired in the same configuration source, `--user` differs
from the configured identity, or the process and Hermes private configurations
conflict on user or token. The configured token must belong to that user; it is never
passed to the model, included in a URL, or written to repository files and logs.

Built-in scheduler delivery is configured separately from private Hermes Cron: set both
`VITALIS_PUSH_USER` (the local user ID belonging to the recipient) and `PUSHPLUS_TOKEN`
in the built-in scheduler process's private environment. One global token is bound to
one local user; other users are still synchronized and analyzed but are not sent to
that token. If either setting is missing, built-in Morning and Evening delivery is
disabled and no success marker is written. The Hermes tool still uses its explicit
`VITALIS_USER` and private token, independently of `VITALIS_PUSH_USER`.

## Public Deployment

The complete API is currently suitable only for trusted loopback or private-network
access. `X-User-Id` is not authentication; even if the upstream listens on `127.0.0.1`,
connecting it directly to a Cloudflare Quick Tunnel or public HTTPS reverse proxy
also exposes health data reads, writes, and device-token issuance. Do not publish the
complete application directly or rely on TLS, a temporary tunnel address, or CORS for
protection. Leave `VITALIS_PUBLIC_URL` empty unless a trusted identity check and
local-user authorization binding protect every sensitive route. Browser pairing needs
a separate end-to-end design compatible with that gateway.

## Scheduled Jobs

Synchronization, analysis, and push rendering are separate stages. The table shows the
actual built-in scheduler times; Hermes' 09:30-21:30 hourly Morning job and 22:30 Evening
job are alternative entry points, so enable only one delivery entry point:

| Local time | Job | Behavior |
| --- | --- | --- |
| 02:00 | Nightly sync | Enqueue 7 days; analyze after the durable attempt succeeds |
| 09:30 | Morning (once) | Enqueue 2 days; defer on incomplete sleep, without an automatic same-day built-in retry |
| 21:30 | Evening (once) | Enqueue 1 day; analyze and attempt Evening delivery after successful sync |

Unless `VITALIS_NO_SCHEDULER=1` is set, FastAPI lifespan starts the built-in scheduler for
both `python -m vitalis.main` and direct `uvicorn vitalis.api.app:app` launches. Each
dispatcher pass processes at most `SYNC_DISPATCHER_BATCH_CHUNKS` due chunks and rotates
attempts by their last update. Network work runs outside database transactions; renewable
attempt/chunk leases prevent a stale process from claiming or finalizing additional work
after takeover. The supplied `vitalis-api.service` explicitly disables its embedded
scheduler and `vitalis-worker.service` only drains due sync chunks. Installing just these
two units does not enqueue scheduled reports; a separate entry point must own enqueue
and delivery, such as a configured Hermes Cron job.

An insufficient profile remains insufficient. The scheduler does not replace it with
an older result, a default score, or a generic training template.

## Repository Layout

```text
vitalis/
|-- connectors/          Source authentication, fetch, and normalization
|-- models/              Current normalized health contracts
|-- storage/             SQLAlchemy persistence
|-- intelligence/        Deterministic health intelligence pipeline
|-- services/            Synchronization, aggregation, and push services
|-- api/                 FastAPI routes
`-- scheduler/           Independent sync, analysis, and push jobs
skills/vitalis/          Hermes Read / Analyze / Act integration
tests/                   Unit and API coverage
browser_extension/       Official-page Zepp browser pairing
zepp_os/balance2_bridge/ Balance 2 device-side heart-rate bridge
```

## Verification

`schema_export.py` produces seven deterministic exports from the Pydantic contracts and uses standard local `$ref` references; callers must not assume that only an explanation export exists. The exports describe the current contracts and contain no personal health values or real records.

Run the complete suite:

```bash
.venv/bin/python -m pytest -q
```

The current verified result is recorded in `SYSTEM.md`. The suite covers connector parsing and
synchronization, browser pairing, health-data APIs, device isolation, baselines,
Daily/Weekly/Monthly intelligence, health-event lifecycle, training response,
personal associations, immutable snapshots, bounded Context, Timeline, push rendering,
and Hermes Skill contracts.

Additional checks used before delivery:

```bash
.venv/bin/python -m compileall -q vitalis skills/vitalis/tools
.venv/bin/python /root/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/vitalis
git diff --check
```

## Development Contract

The repository is pre-production and current-contract-only. It does not maintain legacy
endpoints, backfills, dual reads, or old-data adapters. Re-ingest disposable local data after
a contract change; known legacy SQLite layouts must first use the explicit schema-migration
commands above for audit and migration. Missing observations remain missing and must never be
replaced with zero or fabricated measurements.

When upgrading from an earlier checkout, stop every API, scheduler, and synchronization worker,
retain a verified backup, and run `vitalis.storage.schema_migration audit`. For a known layout,
run `vitalis.storage.schema_migration migrate` with the historical source and `--backup`, then
audit again and start current code only when it reports `clean=true`. Migratable historical data
is preserved; unknown differences require separate review rather than forced migration. Do not
let old and new versions write the same database concurrently. Create a new empty SQLite database
or PostgreSQL application schema only for a contract change that cannot be migrated, let
`init_db()` create its tables, then reconnect Zepp, synchronize the desired history, and run a
new analysis.

After re-ingestion, verify that each daily table has one row per `(user_id, date)`, that
same-time metrics from different sources/scopes/devices remain separate, that source-qualified
workout details and user links resolve independently, and that each daily training summary
matches all canonical workouts grouped by `VITALIS_TIMEZONE`.

Open Health Insights also requires the current workout-detail schema, including
`workout_metric_samples.source`. Before enabling it on a real installation, rebuild a fresh
schema, re-synchronize at least the desired 42-day load window (180 days recommended), then
PATCH the user-confirmed profile fields such as `sex` and `confirmed_hrmax_bpm`. Do not let
workout observations or device-zone candidates silently populate confirmed profile values.

See [SYSTEM.en.md](../SYSTEM.en.md) for the required plan, test, documentation, commit, and
delivery workflow.
