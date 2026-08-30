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
| `SYNC_CRON_HOUR` / `SYNC_CRON_MINUTE` | Nightly synchronization time |
| `VITALIS_NO_SCHEDULER` | Disable background jobs when set to `1` |

The deterministic analysis engine does not require an LLM. Hermes or another agent
consumes structured Vitalis results and renders them separately.

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
orchestrator and must not calculate, merge, or fill health observations.

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
activity and stress, a seven-day SDNN trend, and rolling training load through today,
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
| 02:00 | Nightly sync | Synchronize 7 days and persist a fresh analysis run |
| 09:30-21:30 hourly | Morning retry | Send once after today's sleep has a wake time |
| 22:30 | Evening | Synchronize 1 day and send the distinct Evening profile once |

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

See [SYSTEM.md](../SYSTEM.md) for the required plan, test, documentation, commit, and
delivery workflow.
