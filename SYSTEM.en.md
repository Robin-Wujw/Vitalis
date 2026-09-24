# Vitalis Development System

[中文](SYSTEM.md)

## 1. Purpose

This document is Vitalis's current execution contract. It contains only the rules required for daily work, verified current state, and work that remains unfinished.

Historical tasks, completed TODOs, session records, and per-run verification results are archived in `docs/SYSTEM_HISTORY.en.md`. They are not active context and cannot override this document.

## 2. Required Workflow

1. **Inspect the current state**: first review Git status, relevant code, existing documentation, and tests without overwriting existing user changes.
2. **Form a plan**: before changing state on a non-trivial task, write an executable plan and TODO list and make the scope visible to the user.
3. **Complete locally**: implement locally first, then run focused tests and full verification. The server receives only verified commits and is not a debugging environment.
4. **Synchronize documentation**: update the corresponding Markdown when behavior, APIs, configuration, project structure, platform support, data contracts, or test counts change.
5. **Verify deployment**: after server deployment, perform only health checks, schema checks, and controlled end-to-end verification. Return to local implementation for any behavioral fix and verification.
6. **Deliver**: every logically complete task requires tests, documentation, and a traceable commit. Failures and skipped checks must be recorded accurately.

## 3. Completion Rules

- `[ ]` means pending, partially complete, or not yet verified; `[x]` means implementation, tests, documentation, and focused verification are all complete.
- Active TODOs contain only unfinished work; move completed items immediately to `docs/SYSTEM_HISTORY.en.md` or the commit record.
- Test counts in documentation must come from the latest real run, not a copied historical number.
- Documentation-only work must at least verify Markdown, links, and `git diff --check`; behavioral changes must also run focused and complete tests.
- Existing user changes must be preserved; unrelated cleanup and refactoring are outside the task.

## 4. Data and Health Boundaries

- Missing observations remain explicitly missing and may produce `INSUFFICIENT_DATA`; never fill them with zeroes, stale results, or template content.
- Device streams remain isolated by source, scope, device, and unit; never average HRV across devices or assume devices are interchangeable.
- Devices, vendors, and user feedback are sources of facts; Vitalis generates inference and advice only from explicit data.
- Open Health output is shadow-only and cannot alter training decisions.
- Hermes only routes, explains, and records feedback explicitly provided by the user; it must not recompute measurements, trends, recovery, or training content.
- Training advice must prioritize pain/injury, insufficient recovery, and insufficient-data gates.

## 5. Database and Deployment Boundaries

- The production database schema must match the current code contract. Stop when schemas do not match; do not use an old database to explain new code.
- Before a destructive database operation, confirm the exact target, data value, recovery method, and user authorization.
- Server databases, secrets, Zepp credentials, and PushPlus tokens must not appear in logs, documentation, commits, or conversation.
- The server runs a verified commit. Fix parsers, data contracts, synchronization, morning reports, and tests locally before pushing, deploying, and verifying.
- A PushPlus morning report with training prescriptions requires complete sleep and verified training history for the previous seven days. When sleep is complete but sport sources remain unqueried, the report may still show sleep and body readings, date-labeled activity, and observed workouts, stating the gap once without a training prescription. Date, credential, sleep-completeness, and daily deduplication gates still apply; never generate today's training advice from stale or partially verified training history.

## 6. Cross-Platform Conventions

- Core business logic, data contracts, and tests should be shared between Linux servers and Windows workstations.
- Handle operating-system differences through small platform adapters rather than scattering platform checks through business logic.
- Windows uses PowerShell 5.1-compatible syntax; Git Bash commands use POSIX syntax.
- Verify and record platform differences separately; do not describe a Linux file-permission assertion as passing on Windows.

## 7. Mandatory Bilingual Markdown Policy

- Every Markdown addition, deletion, rename, or semantic update in the repository must update both the Chinese and English versions in the same change. Never merge one language first or allow a translation to lag behind the current contract.
- Each pair stores Simplified Chinese (zh-CN) in the unsuffixed `.md` file and English in the `.en.md` file. The only exception is `docs/README.md`, which maintains complete Chinese and English navigation inline and has no `docs/README.en.md`.
- Every paired file must provide visible reciprocal language-switch links: the Chinese file links to the English file and the English file links back to the Chinese file. Apart from that switch and the inline documentation hub, local Markdown links must stay within the current language.
- Both languages must remain semantically and structurally equivalent. Heading levels, checkboxes, tables, fenced code blocks, links, and all dates, commit hashes, test counts, versions, API paths, commands, file paths, schema/field names, and other technical literals must align. A translation must not omit, condense, or reinterpret content.
- Authoritative license text in license and third-party notices must not be translated or rewritten. The MIT text in `THIRD_PARTY_NOTICES.md` and `THIRD_PARTY_NOTICES.en.md` must be byte-for-byte identical after line-ending normalization.
- `skills/vitalis/SKILL.en.md`, `skills/vitalis/knowledge/evidence.en.md`, and `skills/vitalis/workflows/*.en.md` are English reading sidecars only, not runtime entry points. Skill frontmatter, tool routing, and runtime workflows are always defined by the unsuffixed Chinese files; runtime code must never load an `.en.md` sidecar.
- Before delivering any Markdown change, run `tests/test_bilingual_markdown.py`, the complete local link/anchor checks, the applicable full test suite, and `git diff --check`. Do not deliver when the fixed inventory, reciprocal switches, structural parity, language-local links, Skill routing, or license invariants fail.

## 8. Documentation Responsibilities

- `README.md` / `README.en.md`: product positioning, primary experience, trust boundaries, project status, and documentation entry points.
- `docs/README.md`: the only inline bilingual documentation hub and audience-based navigation.
- `docs/GETTING_STARTED.md` / `docs/GETTING_STARTED.en.md`: local startup, deployment, scheduling, and verification.
- `docs/ZEPP_INTEGRATION.md` / `docs/ZEPP_INTEGRATION.en.md`: Zepp pairing, credential lifecycle, data coverage, and device boundaries.
- `docs/API.md` / `docs/API.en.md`: HTTP API guide; OpenAPI remains the complete interface reference.
- `docs/ARCHITECTURE.md` / `docs/ARCHITECTURE.en.md`: system boundaries, data flow, intelligence policy, and contracts.
- `docs/RESEARCH_NOTES.md` / `docs/RESEARCH_NOTES.en.md`: external evidence, research limitations, and implementation candidates.
- `docs/SYSTEM_HISTORY.md` / `docs/SYSTEM_HISTORY.en.md`: complete, aligned archive of historical work and verification.
- `SYSTEM.md` / `SYSTEM.en.md`: current execution contract and unfinished work.

## 9. Current Status

Date: 2026-09-24

- The 2026-09-24 denser Morning report remains local on `feat/richer-morning-evidence`; it has not been committed, deployed, or resent. All 736 local Python tests and 6 Balance 2 Node tests passed. The server last verified on `165b6e7`; read-only partition inspection showed the generic sport endpoint succeeded while 12 category endpoints were unavailable, so observed workouts cannot prove complete history or support a training dose. The user received the old Morning report today; this round used synthetic data only to preview the new projection.
- The 2026-09-23 local audit closed the conditional cross-user built-in delivery risk `AUD-001`; all 705 Windows Python tests, 47 bilingual checks, and 6 Balance 2 Node tests passed. The user confirmed that the complete API is currently reachable only on loopback/private networks; the live service was not inspected, deployed, or sent real messages. Tracked source directories were not moved; open items are in section 10 and completed evidence is in `docs/SYSTEM_HISTORY.en.md`.
- `2d98418` was deployed, with `674` tests passing on both Windows and server Linux, including `47` bilingual Markdown and local link/anchor checks. The real evening API verified limited exercise display names; the existing scheduler successfully delivered a facts-only morning report, with no action plan and the daily deduplication marker confirmed.
- Current local contracts are `WorkoutDetail 5.1` (shared `WORKOUT_DETAIL_SCHEMA_VERSION`), Daily 14.0, Weekly 6.0, Monthly 3.0, MorningBriefing 4.0, Agent Context 6.0, Intelligence 14.0, StrengthAnalysis 2.0, and Decision Policy 9.0. `GET /intelligence/evening-briefing`, `GET /intelligence/weekly-briefing`, and `GET /intelligence/monthly-briefing` return `ReportBriefing 1.0` and share the same `sections` with HTML.
- Field-availability checks do not expose personal health values. General capabilities may be described, but personal health values, real records, and unverified app-corrected exercises are not written to the repository or claimed as obtained.
- Only when `training_family=strength` and a lap row has exactly 62 columns are the limited weight, repetition, and exercise-code observations read from 0-based positions 21, 22, and 28; their set order is retained in independent `observed_sets`, not used as `explicit_exercises`, muscle coverage, or prescription evidence.
- Weekly/monthly activity aggregation uses local dates; distinct workouts on the same day have their energy summed, while repeated daily summaries retain median reduction without mixing sources. Reports preserve metric units and partial-coverage limitations; unknown energy units are not filled in as kilocalories.
- Missing activity remains `None`; a legacy `ActivityRecord` default zero without observation evidence is not a measurement. Generic calories remain `role=unspecified`, are not treated as total energy expenditure, and are not added across duplicate entries or to workout calories; no intake record means no energy-deficit conclusion. Source/scope/device/unit streams remain separate.
- ProfileLoader training-history coverage spans 56 local days; synchronization ledgers and chunks remain subject to their respective runtime limits, and insufficient range degrades with limitations. Unknown monthly days are never described as rest. The Monthly renderer is an explicit capability, not a new cron job; the retrospective path remains safely retained.

The following records current verification and retained historical baselines:

- The current working branch is `fix/zepp-identity-ownership`; this documentation sync does not change the runtime role of English sidecars.
- Morning scheduling was verified to keep running but return `stored_data_incomplete`: the general history endpoint succeeds while other sport-specific endpoints are unavailable, so the previous seven days lack complete training-history proof. Facts-only delivery does not reinterpret unavailable endpoints as empty records or change history-coverage markers. Server backup and schema audits passed without a structural migration.
- Verified Zepp semantics remain unchanged: daily stress summaries come from `all_day_stress` fields and curves from explicit timestamped `data`; `Charge/stress_data` protobuf and `Charge/insight_data` still have no provable semantics and remain unrequested.
- The 2026-09-07 deployment acceptance performed backup, deployment, service restart, and report checks only within that round's explicit authorization. Evening tests did not write scheduled markers; a successful facts-only Morning report used normal daily deduplication. That one-time push authorization is not reused; the 2026-09-23 local audit did not operate the server or send real messages.

## 10. Current Unfinished Work

These open items come from the 2026-09-23 local code audit; severity describes impact under the stated conditions, not an incident confirmed in production. The complete API is currently reachable only on loopback/private networks; server secrets and personal health values were not inspected. The fixed conditional P0 is archived in `docs/SYSTEM_HISTORY.en.md`; only unfinished work appears here.

- [ ] `AUD-002` **P1 / conditional P0: public authentication boundary**. `X-User-Id` in `vitalis/api/deps.py` only selects an identity; directly publishing the full API permits cross-user reads, writes, and device-token issuance. The whole-app Quick Tunnel example has been removed from the bilingual guide, but application authentication is still absent. Done when every sensitive route has tested authentication and local-user authorization binding, with compatible browser pairing, before any public entry point is enabled.
- [ ] `AUD-003` **P1 / deployment**. `deploy/systemd/vitalis-worker.service` requires `vitalis.service`, but the supplied unit is `vitalis-api.service`; the API unit disables embedded scheduling and the worker only drains due chunks, with no scheduled enqueuer. Done when unit names and scheduler ownership agree and a fresh install verifies enqueue, restart, and recovery.
- [ ] `AUD-004` **P1 / delivery recovery**. `vitalis/services/zepp_sync_coordinator.py` finalizes sync before analysis/delivery; exceptions are only logged and terminal attempts do not enter recovery. Done when independent downstream work and outcomes are persisted, with tests for crash after terminal commit, send failure, and ambiguous duplicate external delivery.
- [ ] `AUD-005` **P1 / privacy logging**. `vitalis/services/push_service.py::_log_handler` writes the complete health-report body to INFO logs. Done when only non-sensitive delivery metadata are logged and tests exclude personal report content from logs.
- [ ] `AUD-006` **P1 / browser re-pairing**. `browser_extension/background.js` retains the old `browserLinkToken` during new pairing and prefers the old-link credential branch, potentially updating the wrong account or sending the old token to a new server. Done when account switches revoke the old link and executable re-pair/server-switch tests pass.
- [ ] `AUD-007` **P1 / built-in Morning retry**. Built-in Morning runs only at 09:30 in `vitalis/scheduler/jobs.py`; incomplete sleep has no automatic same-day retry. Hermes' hourly job is a separate entry point. Done when the intended built-in retry policy is agreed and deferral plus daily deduplication is tested.
- [ ] `AUD-008` **P1 / backup and retention**. The durable sync ledger still needs a production backup/restore drill and long-term retention policy; markers in `vitalis/services/daily_push.py` live outside the database. Done when isolated restore, schema, delivery markers, and coverage evidence are verified before defining cleanup that preserves historical proof.
- [ ] `AUD-009` **P1 / training-history coverage**. Unavailable real sport-specific endpoints leave the prior 7 days unverified. Morning may show recorded facts but must not generate a training dose or treat unavailable sources as no training. Done when verifiable vendor responses or an alternative complete source establish coverage without weakening the gate.
- [ ] `AUD-010` **P2 / manual import page**. The fallback page in `vitalis/api/routes/connect.py` contains double-brace JavaScript and hardcodes `X-User-Id: '001'`. Done when actual page interaction verifies parsing, submission, and identity selection; the preferred pairing path remains separate.
- [ ] `AUD-011` **P2 / health and observability**. `vitalis/api/app.py` keeps serving a fixed `healthz=ok` if scheduler startup fails, without backlog or worker-readiness signals. Done when liveness and readiness are separated and database, scheduling ownership, backlog, and startup-failure alerts are covered.
- [ ] `AUD-012` **P2 / verification gap**. There is no checked-in CI; `tests/conftest.py` disables scheduling and uses in-memory SQLite, while Node device tests and real process restarts are outside the Python suite. Done when reproducible cross-platform Python/Node and process-level recovery checks are automated.
- [ ] `AUD-013` **P2 / PostgreSQL path**. `.env.example` selects PostgreSQL without provisioning in local instructions; `vitalis/storage/schema_migration.py` only supports SQLite audit/migration. Done when local defaults and PostgreSQL installation, upgrades, concurrency, and restore are demonstrably verified.
- [ ] `AUD-014` **P2 / full heart-rate page boundary**. `vitalis/connectors/zepp/fetcher.py` and `vitalis/services/zepp_sync_coordinator.py` misclassify a full-page cursor exactly at the exclusive window end as `partial`. Done when the end is distinguished from a stalled cursor and both fetch paths have a 1000-row boundary regression.
- [ ] `AUD-015` **P2 / timezone-selected days**. `vitalis/services/zepp_sync_coordinator.py::_window` builds `create_attempt(days=1, timezone_name=...)` in the app timezone before requesting dates in the selected timezone. Done when today and the window use the same zone and local-date/DST transitions are tested.
- [ ] `AUD-016` **P3 / regression isolation**. The mixed seconds/milliseconds cursor example in `tests/test_fetcher.py` does not put the millisecond sample earlier, and the explicit-user test in `tests/test_vitalis_skill.py` depends on ambient `VITALIS_USER` being unset. Done with mixed-order/successor-cursor and explicitly isolated environment tests.
- [ ] `AUD-017` **P2 / strength exercise evidence**. Expand the Zepp exercise dictionary and source of app-corrected sets; only a limited, verified display-name reference exists, unknown codes must not be generalized, and `unit` and unmapped exercise `name` remain unknown. Done with redacted same-device/same-workout comparisons validating mapping and source precedence.
- [ ] `AUD-018` **P3 / awaiting user feedback**. Adjust the new-version real test Evening report after content feedback; additional test pushes require fresh explicit authorization. Done when feedback yields corresponding report assertions, without unsolicited real messages.
- [ ] `AUD-019` **P2 / stale dependency tree**. Ignored `.codex_pydeps/` contains an old Vitalis installation snapshot; historical `PYTHONPATH` references could load stale code. Done when external launchers have been checked to no longer reference it before retiring the directory; this local environment is preserved for now.
