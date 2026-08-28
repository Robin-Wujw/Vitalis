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
| 09:30 | Morning | Synchronize 2 days, analyze, and render the Morning profile |
| 21:30 | Evening | Synchronize 1 day, analyze, and render the Evening profile |

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

Current verified result: 167 tests passed. The suite covers connector parsing and
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
