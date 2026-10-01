# Vitalis

[中文](README.md) | [Documentation](docs/README.md)

Vitalis turns wearable sleep, activity, and workout records into traceable personal analysis and training guidance. Its deterministic engine computes the results; agent clients may explain persisted evidence but must not invent measurements or prescriptions. It is a pre-release product, not a medical device.

## Current Entry

The package lives in `src/vitalis/` and exposes `vitalis` / `python -m vitalis` with `demo`, `serve`, `worker`, `user create`, `token issue/revoke`, `db init/reset`, and `doctor`. The demo creates a new SQLite file with synthetic data. The current API uses `/api`; only the worker starts scheduled jobs. User-scoped calls require a user-bound Bearer token, with `read`, `analyze`, `sync`, `feedback`, and `manage` scopes. `X-User-Id` alone is not authentication. The standalone product Skill uses a thin Bearer HTTP client; real Hermes runtime integration remains unverified.

Reports default to the previous complete local calendar week or month; explicit rolling windows are labeled as near 7 or near 28 days. [PushPlus](docs/reports.md) is a one-way, read-only delivery channel and never asks for a reply. Hermes is an optional interactive client for explanations and explicitly authorized feedback.

Current guides are maintained in Chinese: [quickstart](docs/quickstart.md), [reports](docs/reports.md), [data contracts](docs/data-contracts.md), [Zepp](docs/zepp.md), [agent integration](docs/agents.md), and [operations](docs/operations.md). See the [current architecture](docs/architecture.md) and [documentation by role](docs/README.md). Contributors start with [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
