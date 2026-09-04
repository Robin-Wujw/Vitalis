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
- Send a PushPlus morning report only when current synchronization and analysis satisfy the contract; never generate today's training advice from stale or partially verified training history.

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

Date: 2026-09-04

- The current working branch is `fix/zepp-identity-ownership`, based on deployed commit `aef653c`. This documentation work brings all 39 project Markdown files into 19 Chinese/English pairs plus the sole inline-bilingual hub `docs/README.md`.
- The complete post-change Python suite passed 473 tests; all 47 bilingual-contract tests in `tests/test_bilingual_markdown.py` and all 6 Balance 2 Node tests passed. Zepp identity uniqueness and migration hardening, Chinese and English documentation entry points, current SQLite schema migration, and the `all_day_stress` local-day time series have all been verified.
- Comparison of Zepp data and UI for the same account, device, and matching date confirmed that the daily stress summary comes from the `all_day_stress` fields and its chart comes from the explicitly timestamped `data` array. The `Charge/stress_data` protobuf and `Charge/insight_data` still have no provable semantics and remain unrequested.
- Local and server SQLite schema/identity audits are clean. A real `zepp-sync-v4` stress stream completed fetch/parse/write successfully and wrote 234 local-day points for 2026-09-03 with a range of 5-65. The complete attempt was `partial` only because optional capabilities were unavailable, with 0 failed chunks; deterministic analysis returned `SUFFICIENT` / `TRAIN_NORMAL`, and both Morning and Evening projections succeeded.
- The server has deployed `fix/zepp-identity-ownership@aef653c`; API/worker are active, `healthz=ok`, and error-log count is 0. Morning `PushPlus --test` returned `test_sent` without changing the formal schedule marker.

## 10. Current Unfinished Work

- [ ] Establish a production backup/recovery drill and long-term data-retention policy for the durable synchronization ledger.
