# Getting Started

This guide covers local setup, service operation, deployment, scheduling, and
verification. Product positioning belongs in the root [README](../README.md); health
intelligence contracts and calculation boundaries belong in
[ARCHITECTURE.md](ARCHITECTURE.md).

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
[ZEPP_INTEGRATION.md](ZEPP_INTEGRATION.md#source-identity-ownership). After every
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

Keep the checked-in Skill as the single source of truth by linking it into Hermes'
local discovery tree from the repository root:

```bash
mkdir -p "$HOME/.hermes/skills/health"
ln -s "$PWD/skills/vitalis" "$HOME/.hermes/skills/health/vitalis"
```

Do not copy the Skill or commit a local user identity. Configure the loopback API and
the explicit local Vitalis user in Hermes' private `~/.hermes/.env` instead:

```dotenv
VITALIS_API=http://127.0.0.1:8000
VITALIS_USER=<local-user-id>
NO_PROXY=127.0.0.1,localhost
```

Start Vitalis, then verify discovery and fresh-session loading:

```bash
hermes skills list --source local --enabled-only
hermes --skills vitalis prompt-size --json
```

The list must show `vitalis` as a local enabled Skill, and the prompt-size breakdown
must resolve `vitalis` from the linked path. `VITALIS_USER` has no fallback: every tool
passes that exact identity as `X-User-Id`. Hermes remains a Read / Analyze / Act
orchestrator and must not calculate, merge, or fill health observations. Daily
explanations use only the persisted `/intelligence/explain` projection; a missing
snapshot is reported without automatic synchronization or analysis. Keep this API
loopback/private because `X-User-Id` selects identity but does not authenticate a caller.

### Daily PushPlus Report

The local production setup uses Hermes Cron as the only daily dispatcher. Vitalis runs
as a loopback system service with its embedded scheduler disabled. The morning job runs
hourly from 09:30 through 21:30 `Asia/Shanghai`, synchronizes two days for the explicit
`VITALIS_USER`, and analyzes only the current local day. It sends nothing while today's
sleep status is unavailable or `wake_time` is absent. The next hourly run synchronizes
and checks again; once sleep is complete it sends exactly one Morning report and later
runs skip the date using private atomic state under `~/.hermes/vitalis_push/`. It never
substitutes yesterday's profile.

A separate job runs at 22:30, synchronizes one day, and sends one Evening report for the
current date. The Morning report interprets recovery and presents the health-first
concurrent action plan: fused overnight health, recent running and strength balance,
one primary session, and an optional addition or alternative with a concrete dose and
plain-language reasons. The Evening report instead reviews completed workouts, daily
activity and stress, a seven-night sleep-HRV trend, and rolling training load through today,
then gives a practical recovery action and leaves tomorrow's intensity to the next
complete overnight assessment. Both reports are sent with PushPlus' HTML template using
portable inline styling; report
values are escaped before HTML generation. The HTML root has its own high-contrast light
background so PushPlus dark mode cannot place dark report text directly on black. The
Evening report does not show the sleep-only RMSSD curve or infer continuous daytime
HRV, stress, or emotion from sparse samples.

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

Running `hermes cron run <job-id>` is an official scheduled invocation: a successful
delivery writes the daily marker and prevents that period from being sent twice.

The tool exits before synchronization when either `VITALIS_USER` or `PUSHPLUS_TOKEN` is
missing. The token is never passed to the model, included in a URL, or written to
repository files and logs.

## Public Deployment

Keep the application listener on a private or loopback interface and put a
browser-trusted HTTPS reverse proxy or tunnel in front of it:

```bash
HOST=127.0.0.1
VITALIS_PUBLIC_URL=https://health.example.com
```

A Cloudflare Quick Tunnel can be used for temporary integration testing:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Quick Tunnel addresses are temporary. Persistent deployments should use a stable
domain and automatically renewed TLS certificates.

## Scheduled Jobs

Synchronization, analysis, and push rendering are separate stages:

| Local time | Job | Behavior |
| --- | --- | --- |
| 02:00 | Nightly sync | Enqueue 7 days; analyze after the durable attempt succeeds |
| 09:30-21:30 hourly | Morning retry | Enqueue 2 days; analyze and send once after successful sync and complete sleep |
| 22:30 | Evening | Enqueue 1 day; analyze and send the distinct Evening profile after successful sync |

FastAPI lifespan owns scheduler startup and shutdown, so both `python -m vitalis.main` and
direct `uvicorn vitalis.api.app:app` launches recover persisted work. Each dispatcher pass
processes at most `SYNC_DISPATCHER_BATCH_CHUNKS` due chunks and rotates attempts by their
last update. Network work runs outside database transactions; renewable attempt/chunk
leases prevent a stale process from claiming or finalizing additional work after takeover.
Set `VITALIS_NO_SCHEDULER=1` only when a separate process owns dispatch and recovery.

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

The repository is pre-production and current-contract-only. It does not maintain
legacy endpoints, migrations, backfills, dual reads, or old-data adapters. Re-ingest
disposable local data after a contract change. Missing observations remain missing and
must never be replaced with zero or fabricated measurements.

The current canonical-data constraints require a fresh database/schema when upgrading
from an earlier checkout: stop every API, scheduler, and synchronization worker; retain a
cold copy only for whole-version rollback; create a new empty SQLite database or
PostgreSQL application schema; start the current code so `init_db()` creates its tables;
then reconnect Zepp, synchronize the desired history, and run a new analysis. Do not run
old and new versions against the same database. The application intentionally contains
no online ALTER, legacy-row conversion, or compatibility reader for this change.

After re-ingestion, verify that each daily table has one row per `(user_id, date)`, that
same-time metrics from different sources/scopes/devices remain separate, that source-qualified
workout details and user links resolve independently, and that each daily training summary
matches all canonical workouts grouped by `VITALIS_TIMEZONE`.

Open Health Insights also requires the current workout-detail schema, including
`workout_metric_samples.source`. Before enabling it on a real installation, rebuild a fresh
schema, re-synchronize at least the desired 42-day load window (180 days recommended), then
PATCH the user-confirmed profile fields such as `sex` and `confirmed_hrmax_bpm`. Do not let
workout observations or device-zone candidates silently populate confirmed profile values.

See [SYSTEM.md](../SYSTEM.md) for the required plan, test, documentation, commit, and
delivery workflow.
