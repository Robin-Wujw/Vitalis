# Vitalis Development System

## 1. Purpose

This document is the execution contract for repository changes. It keeps planning,
implementation, tests, and project documentation synchronized so unfinished work is
visible and reproducible.

## 2. Required Workflow

Every implementation session must follow this order:

1. Inspect the repository state, existing documentation, and relevant tests without
   changing business code.
2. Write a detailed, part-based TODO list in this document and show it to the user.
3. Implement only one listed part at a time.
4. Add or update unit tests for the behavior changed by that part.
5. Run the focused tests for the part. A part may be checked only after they pass.
6. Update the relevant project Markdown when behavior, APIs, configuration, project
   structure, or test counts change.
7. Run the complete test suite and record the result before closing the session.
8. Commit and push the corresponding feature after its tests and documentation pass.
   A local implementation is not complete until the remote branch contains it, unless
   the user explicitly asks to keep the work local or the push is blocked externally.

## 3. Completion Rules

- `[ ]` means pending or not yet verified.
- `[x]` means the implementation, corresponding tests, documentation, and focused
  verification for that part are complete.
- A partially implemented part remains `[ ]`; its current state must be noted below it.
- Failed or skipped verification must be recorded explicitly and must not be described
  as complete.
- Every feature or fix must have a TODO item, focused unit-test coverage, relevant
  Markdown updates, and its own traceable commit before that item can be checked.
- Update TODO checkboxes as each part finishes; do not defer all progress updates until
  the end of the session.
- Record the pushed branch and commit in the verification log. If a push fails, leave
  the delivery item unchecked and record the exact blocker without exposing secrets.
- Existing user changes must be preserved. Unrelated cleanup is outside the task.
- Test totals in project documentation must come from the latest full test run.

### 3.1 Pre-production New-data-only Policy

This repository is pre-production. Every change must target only the current contract
and data ingested after that contract is implemented.

- Do not preserve backward compatibility. Remove superseded endpoints, field names,
  schemas, tools, classes, code paths, and tests instead of retaining aliases or
  adapters.
- Do not add legacy-data readers, version branches, dual reads/writes, migrations,
  backfills, reinterpretation rules, or conversion jobs for data created by an older
  contract.
- Existing local data is disposable. When a schema or normalization contract changes,
  clear the affected old data and ingest it again under the current contract rather
  than making new code understand the old representation.
- Do not infer a new required field from an obsolete field, endpoint label, text hint,
  unrelated metric, default value, or another device stream.
- Missing observations in newly ingested data remain explicit missing data and may
  produce `INSUFFICIENT_DATA`; this is data-quality abstention, not a compatibility
  fallback. Never replace missing measurements with zero or fabricated values.
- Tests and fixtures must exercise only the current contract. Delete obsolete contract
  expectations when the implementation changes.
- An exception requires a new, explicit user instruction that names the exact legacy
  contract or dataset to preserve. General requests to improve or extend Vitalis do
  not authorize compatibility work.

## 4. Project Documentation Map

- `README.md`: product-facing positioning, experiences, capabilities, trust boundaries,
  project status, and links to technical documentation.
- `docs/GETTING_STARTED.md`: installation, local operation, deployment, scheduling, and
  verification commands.
- `docs/ZEPP_INTEGRATION.md`: Zepp connection, credential lifecycle, normalized data
  coverage, workout heart rate, and the Balance 2 bridge.
- `docs/API.md`: current HTTP endpoints, identity requirements, and request examples.
- `docs/ARCHITECTURE.md`: system boundaries, data flow, implemented architecture, and
  future work.
- `SYSTEM.md`: working agreement, current detailed TODO list, progress, and verification
  evidence.

## 5. Current Work Session

Date: 2026-08-26

Goal: complete password-free Zepp browser login pairing. The user signs in on the
official page with any login method offered there; Vitalis automatically receives the
resulting session credential, keeps it current when the browser session changes, syncs
data, and reports a disconnected state when re-login is required.

### Detailed TODO

- [x] Part 1 - Establish the development system and baseline task record.
  - Inspect repository status and existing Markdown files.
  - Define the mandatory plan/implement/test/document/verify loop.
  - Record the detailed session checklist before changing business code.
- [x] Part 2 - Identify the unfinished login and continuity work.
  - Run the complete existing test suite as a baseline.
  - Inspect the pairing page, browser extension, token storage, sync scheduler, and
    connection-status endpoints.
  - Record the concrete gaps between one-time pairing and automatic credential renewal.
- [x] Part 3 - Complete the official-page login handoff.
  - Let the extension open the official login page when no valid browser session exists.
  - Persist a pending pairing locally and submit automatically after login completes.
  - Keep account passwords and verification codes out of Vitalis and its logs.
- [x] Part 4 - Complete credential continuity and disconnect reporting.
  - Issue a separate high-entropy browser-link token after one-time pairing and store
    only its digest on the server.
  - Update credentials when the official browser cookie changes and on a periodic check.
  - Verify renewed credentials, trigger sync, and expose connected/expired/re-login
    status without returning secrets.
- [x] Part 5 - Complete corresponding unit-test coverage.
  - Cover initial pairing and browser-link issuance without exposing the stored digest.
  - Cover credential renewal, invalid link tokens, user isolation, and re-login status.
  - Cover extension manifest/static behavior where practical.
  - Run the focused test module(s) and record the result.
- [x] Part 6 - Synchronize project Markdown.
  - Update `README.md` for user-visible behavior, commands, and verified test totals.
  - Update `docs/ARCHITECTURE.md` if boundaries, data flow, or roadmap status changed.
  - Keep implementation provenance and the external reference project name out of
    repository Markdown intended for publication.
  - Review this checklist so checked items reflect actual evidence.
- [x] Part 7 - Perform final verification.
  - Run the complete test suite.
  - Run a lightweight application import/startup check when applicable.
  - Record commands, results, and any residual limitations below.
- [x] Part 8 - Make database initialization independent of the launch command.
  - Reproduce the public pairing-page failure when Uvicorn starts the ASGI app directly.
  - Initialize the database schema from the FastAPI lifespan for every supported launch
    command, without deleting or replacing existing data.
  - Add a focused regression test, run the full suite, restart the public service, and
    verify both the health check and pairing page over the public address.
- [x] Part 9 - Provide a browser-trusted HTTPS endpoint for real pairing tests.
  - Use a temporary managed HTTPS tunnel because the server has no configured domain or
    trusted certificate; do not present a self-signed certificate as secure completion.
  - Bind Uvicorn to loopback so credential traffic reaches it only through the encrypted
    public tunnel during the test.
  - Verify the health page, pairing page, extension download, pairing-status API, and
    extension HTTPS URL validation through the public HTTPS origin.
  - Document the temporary URL limitation and the production requirement for a stable
    domain with managed TLS.
- [x] Part 10 - Preserve browser-extension pairing drafts across popup closes.
  - Reproduce that the popup stores the Vitalis address and pairing code only on form
    submission, so copying the two values one at a time clears the first value.
  - Persist each field immediately on input/paste and restore it when the popup reopens.
  - Prevent asynchronous restoration from overwriting text entered in a newly opened
    popup, then add regression coverage and verify the HTTPS extension download.
- [x] Part 11 - Make the Zepp connection API path safe to open in a browser.
  - Reproduce `GET /api/v1/connect/zepp` returning HTTP 405 because the route only
    exposes the programmatic POST operation.
  - Redirect browser GET requests to the user-facing pairing page while preserving the
    existing POST API behavior and selected Vitalis user.
  - Add regression coverage, run the full suite, restart the HTTPS backend, and verify
    the public redirect chain.
- [ ] Part 12 - Discover an existing Zepp login cookie regardless of its URL path.
  - Confirm from public logs and storage that the extension opened the official page but
    sent no credential submission, leaving the latest pairing session waiting.
  - Query the two allowed login-cookie names across permitted Zepp/Huami domains instead
    of filtering by two fixed URLs that can exclude cookies with a different Path.
  - Preserve domain/name restrictions, add regression coverage, publish a new extension
    version over HTTPS, and observe the real pairing result without exposing secrets.
- [x] Part 13 - Verify real multi-device HRV synchronization.
  - Use the saved real Zepp connection rather than mock or exported sample data.
  - Deduplicate repeated measurements within one vendor response before database upsert.
  - Decode per-sample HRV device attribution and preserve it in normalized records.
  - Confirm continuous HRV coverage for both connected devices without publishing
    identifiers or health values, then run the complete test suite.

### Baseline Notes

- Git branch: `main` at initial commit `7415e54`.
- Most implementation files are currently untracked, so repository state must be
  inferred from code, tests, and documentation rather than commit history.
- Existing user files and data are treated as authoritative working state and will not
  be reverted.
- The existing browser extension already reads the official login cookie and completes
  a one-time pairing, but it cannot resume automatically after opening the login page.
- The current pairing code expires and is single-use, so it cannot securely carry later
  browser-session updates.
- Server-side access credentials cannot be refreshed independently; continuity requires
  a still-signed-in user browser, with an explicit re-login prompt when that session is
  gone.

### Verification Log

- Part 1: repository and documentation inventory completed; `SYSTEM.md` created.
- Part 2: 62 tests collected. The baseline full run timed out after 90 seconds while
  entering the first `TestClient`; a focused run confirmed the wait occurs in the
  Starlette/AnyIO lifespan portal before the request is sent. Functional review found
  that one-time credential pairing works, but no durable browser link, cookie-change
  renewal path, or stored re-login state exists.
- Part 3: extension manifest and JavaScript syntax checks passed. Pending pairing is
  persisted, the official login page opens when needed, and the background worker
  resumes automatically when the login cookie appears. No password or verification-code
  field is collected by Vitalis.
- Part 4: Python compilation and an isolated in-memory storage check passed. Browser
  links store only SHA-256 token digests; credential renewal uses a bearer header,
  unchanged credentials are verified without redundant sync, changed credentials start
  an incremental sync, and browser/scheduled failures surface a persistent re-login
  state.
- Part 5: focused API and extension modules passed 27 tests. Coverage includes link
  issuance, digest-only server storage, renewal, disconnect reporting, invalid bearer
  tokens, resistance to user-header rebinding, extension listeners, and absence of
  account-password inputs.
- Part 6: `README.md` and `docs/ARCHITECTURE.md` now describe the official-page login,
  one-time pairing, digest-only browser link, automatic 30-minute/session-change
  renewal, disconnect state, and current API surface. Published Markdown does not name
  the implementation reference project. The documented total matches test collection.
- Part 7: `.venv/bin/python -m pytest --collect-only -q` collected 67 tests, and
  `.venv/bin/python -m pytest -q` passed all 67 in 2.75 seconds. Both extension
  JavaScript files passed `node --check`, the manifest passed JSON parsing,
  `git diff --check` passed, and importing the FastAPI app plus generating OpenAPI
  succeeded with 21 documented paths including all three browser-link endpoints.
  Uvicorn also reached `Application startup complete` while listening on port 8000.
- Part 8: the public pairing page reproduced a missing `zepp_pairing_sessions` table
  when the ASGI app was launched directly with Uvicorn. Database initialization now
  runs from the FastAPI lifespan for every launch command, with a regression test for
  that contract. Three focused tests passed, then the complete suite passed 68 tests in
  2.66 seconds. After restarting Uvicorn, the public `/healthz`, pairing page, extension
  download, and pairing-status polling all returned HTTP 200.
- Part 9: no stable domain or certificate was configured and inbound ports 80/443 were
  unavailable, so a Cloudflare Quick Tunnel provided a browser-trusted HTTPS test
  origin while Uvicorn was restricted to `127.0.0.1:8000`. TLS verification, health,
  pairing-page rendering, extension download/ZIP integrity, and pairing-status polling
  all passed over HTTPS; the old public plaintext port became unreachable. The pairing
  page advertised the HTTPS origin correctly. Four focused checks passed, followed by
  the complete suite with 69 tests passing in 2.47 seconds.
- Part 10: extension version 0.2.1 persists both popup fields on every input/paste and
  guards asynchronous restoration from overwriting new text. Three focused extension
  tests passed and the complete suite passed 70 tests in 2.36 seconds. The extension ZIP
  downloaded over HTTPS with HTTP 200, passed archive integrity checks, and contained
  the updated manifest and draft-persistence code.
- Part 11: public logs identified the reported 405 as a browser GET to the POST-only
  `/api/v1/connect/zepp` operation. GET now redirects to the pairing page with the
  selected user while POST behavior remains unchanged. Two focused tests passed, the
  complete suite passed 71 tests in 2.33 seconds, and the original public HTTPS URL
  followed the redirect to `/scan?user=001` with a final HTTP 200 HTML response.
- Part 12 (partial): production logs and storage confirmed the official page opened but
  no credential POST arrived; the newest pairing remained `waiting` with no browser
  link. Extension 0.2.2 now queries only the two known login-cookie names across allowed
  Zepp/Huami domains without a URL-path filter. Four focused tests and all 72 tests
  passed, and the HTTPS ZIP was valid and contained the new discovery logic. Completion
  still requires reloading 0.2.2 in the user's browser and observing the real submission.
- Part 13: the saved real Zepp credential completed a 30-day cloud synchronization.
  HRV was continuous across the requested window and two device identities were present.
  Batch-level duplicate timestamps no longer violate storage uniqueness, and Zepp's
  run-length encoded HRV device map is expanded per sample. Focused tests passed and the
  complete suite passed all 75 tests.

### Residual Limitations

- The full suite emits 101 Python 3.14 deprecation warnings for naive
  `datetime.utcnow()` usage in existing Pydantic, SQLAlchemy, pairing, and repository
  code. They do not fail current behavior, but migration to timezone-aware UTC values
  should be handled as a separate storage-wide compatibility change.
- Real Zepp login and cloud synchronization still require a user-owned browser session,
  the unpacked extension, and live Zepp service access; automated tests use mock mode.
- The active Quick Tunnel is temporary and has no uptime guarantee. Production still
  requires a stable domain, managed TLS certificate renewal, and a persistent reverse
  proxy or named tunnel; `VITALIS_PUBLIC_URL` is the authoritative public origin.

## 7. Health Intelligence Implementation Session

Date: 2026-08-28

Goal: replace the prototype prompt-oriented analysis path with the first executable
Vitalis Health Intelligence vertical slice. The system must turn normalized wearable
data into versioned facts, robust personal baselines, deterministic state decisions,
and renderer-ready recommendations. This installation is pre-production, so obsolete
analysis behavior does not require fallback paths or compatibility adapters.

### Detailed TODO

- [x] Part 1 - Define the intelligence contracts and explicit data-quality semantics.
  - Add versioned DailyProfile, baseline, feature, state, decision, and provenance
    models.
  - Distinguish deterministic data quality, device validity metadata, and inference
    confidence; never invent measurement-quality values.
  - Represent insufficient history or missing required signals explicitly instead of
    awarding or subtracting placeholder score points.
- [x] Part 2 - Build the profile loader and robust personal-baseline engine.
  - Load sleep, activity, training, workout, MetricSample, and DailyMetric records for
    one local identity and one requested day.
  - Keep metric and device streams separate, select a preferred stream by usable
    coverage, and report identity/history limitations without silently merging users.
  - Compute versioned 7-day and 28-day median, MAD, percentiles, trend, coverage, and
    robust deviation; use ln(RMSSD) only for positive RMSSD values.
- [x] Part 3 - Implement deterministic sleep, HRV, recovery, and training analyzers.
  - Derive features only from supported normalized fields and attach provenance and
    limitations.
  - Preserve vendor readiness and Charge values as vendor facts rather than treating
    them as the Vitalis recovery result.
  - Keep sleep staging trend-oriented and avoid diagnostic interpretations.
- [x] Part 4 - Implement the decision engine.
  - Produce TRAIN_HARD, TRAIN_NORMAL, TRAIN_LIGHT, RECOVERY, REST, or
    INSUFFICIENT_DATA from explicit multi-signal rules.
  - Return drivers, limitations, rule identifiers, and calibrated confidence bands so
    every result is explainable and reproducible.
  - Keep medical diagnosis out of the engine and surface persistent deviations as
    observation or escalation guidance only.
- [x] Part 5 - Replace the prototype analysis API and make Hermes a thin renderer.
  - Expose a versioned daily-profile API as the sole computed-analysis contract and
    remove obsolete prompt-era analysis routes and tools.
  - Update the Hermes Skill to call the structured endpoint and render morning,
    evening, weekly, and on-demand views without recomputing health decisions.
  - Add schemas, workflows, and evidence notes needed by the renderer while keeping
    calculation rules in Python.
- [x] Part 6 - Complete tests, documentation, and delivery.
  - Add focused unit and API coverage for baseline isolation, insufficient-data
    abstention, analyzer behavior, decision explanations, and the Hermes boundary.
  - Update `README.md` and `docs/ARCHITECTURE.md` for the implemented data flow, API,
    assumptions, and evidence limits.
  - Run focused tests, the complete suite, application/OpenAPI checks, and
    `git diff --check`; record results, commit, and push `main`.

### Verification Log

- Parts 1-4: 11 focused contract, profile, baseline, analyzer, and decision tests
  passed. Coverage includes device/metric isolation, daily reduction of high-frequency
  samples, lnRMSSD deviation, explicit abstention, multi-signal suppression, and
  explainable training decisions.
- Part 5: 47 focused Intelligence/API/Hermes tests passed. The obsolete analysis
  endpoints return 404, the new endpoint completes a storage-to-HTTP multi-signal
  decision, and the Skill contains no local analysis tool.
- Real-data smoke check: an incomplete current date returned `INSUFFICIENT_DATA`; the
  most recent complete date selected one device-specific RMSSD stream with an available
  28-day baseline and produced a structured decision. No health values or device/user
  identifiers were printed. Identity inspection found and surfaced the known split
  vendor identity without merging records.
- Part 6 verification: `.venv/bin/python -m pytest -q` passed all 110 tests with 151
  existing Python 3.14 `datetime.utcnow()` deprecation warnings. Python compilation,
  OpenAPI generation (24 paths), Hermes JSON Schema parsing, both browser-extension
  JavaScript syntax checks, and `git diff --check` passed. OpenAPI contains the new
  daily-profile endpoint and excludes both removed prototype analysis paths.
- Delivery: feature commit `d003587` (`feat: add deterministic health intelligence
  pipeline`) was pushed to `origin/main`.

## 6. Next Session Handoff

Paused at the user's request on 2026-08-26 after extension 0.2.2 was published. Parts
1-11 are complete and verified. Part 12 remains open only because its final acceptance
requires the user's real browser session.

### Implemented State

- FastAPI initializes all storage tables from its lifespan, so direct Uvicorn launches
  no longer leave pairing tables missing.
- Real Zepp login supports one-time user-bound pairing, encrypted saved vendor tokens,
  digest-only long-lived browser links, credential renewal, disconnect reporting, and
  sync status without returning secrets.
- Public deployment defaults to loopback binding and supports an authoritative
  `VITALIS_PUBLIC_URL`; browser GET requests to `/api/v1/connect/zepp` redirect to the
  pairing page instead of returning HTTP 405.
- Extension 0.2.2 preserves pasted address/code drafts, opens the official login page,
  listens for cookie changes, checks every 30 minutes, and searches only the two known
  login-cookie names on permitted Zepp/Huami domains without restricting Cookie Path.
- Documentation reflects HTTPS deployment and the latest verified total: 75 tests pass
  with 101 non-failing Python 3.14 `datetime.utcnow()` deprecation warnings.

### Unfinished Evidence

- The last real attempt opened the official page but produced only pairing-status GETs.
  No `POST /api/v1/connect/zepp/pair/{code}/credentials` reached Vitalis, the newest
  pairing stayed `waiting`, and no browser link was created.
- That attempt used an older extension. Version 0.2.2 contains the proposed Cookie Path
  fix and passed static/unit/archive checks, but has not yet been reloaded and exercised
  in the user's browser.
- Do not claim Part 12 complete until a real credential POST is observed, the pairing
  status becomes `connected`, a browser link exists, and sync outcome is recorded.

### Resume Procedure

1. Preserve the dirty worktree. Most implementation files are untracked relative to the
   initial commit `7415e54`; do not reset, clean, or overwrite them.
2. Start the backend on loopback:
   `.venv/bin/python -m uvicorn vitalis.api.app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1`.
3. Start a new browser-trusted HTTPS endpoint. The temporary binary used in this session
   was `/tmp/vitalis-cloudflared.u66DLF/usr/bin/cloudflared`; if still present, run
   `cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate --protocol http2`.
   Otherwise reinstall it or configure the preferred stable domain/TLS proxy.
4. Do not reuse the old Quick Tunnel URL after its process stops. Open the new HTTPS
   `/api/v1/connect/zepp/scan?user=001`, download/reload the extension, and confirm its
   manifest version is 0.2.2 before generating and submitting a fresh pairing code.
5. Watch server logs for the credential POST and inspect only non-secret pairing/link/
   sync status. Never print the cookie, apptoken, browser-link token, or stored cipher.
6. If 0.2.2 still sends no POST, add local extension diagnostics that report matched
   cookie count, allowed cookie name, and domain only (never values), then determine
   whether Zepp changed its cookie name/domain or moved the session to web storage.
7. After resolving Part 12, add focused coverage, rerun the complete suite, update this
   checklist and README's test total, and configure a stable production HTTPS origin.

## 7. Current Work Session - 2026-08-27

Goal: document and implement the verified Zepp workout-history and high-frequency
workout heart-rate path, following the repository's plan/test/document/push contract.

### Detailed TODO

- [x] Part 1 - Establish the delivery baseline.
  - Confirm the worktree is clean and inspect the current branch, commit, and remote.
  - Confirm `main` and `origin/main` both point to `661430a` before new work begins.
- [x] Part 2 - Strengthen the repository execution contract.
  - Require a written TODO for every feature or fix.
  - Require incremental checkbox updates, focused unit tests, Markdown synchronization,
    a traceable commit, and a successful push before declaring delivery complete.
- [x] Part 3 - Audit the reference implementation and real Zepp response shapes.
  - Compare Vitalis with ZeppBridge history extraction, pagination, detail fetching,
    heart-rate delta decoding, normalization, and storage behavior.
  - Record only endpoint contracts and aggregate evidence; never commit credentials,
    device identifiers, workout identifiers, or raw personal health payloads.
- [x] Part 4 - Correct real workout-history ingestion.
  - Accept the verified `data.summary` list while preserving supported legacy shapes.
  - Parse verified real summary field names and keep the opaque `source` needed by the
    detail endpoint.
  - Add focused parser and fetcher regression tests.
- [x] Part 5 - Add high-frequency workout heart-rate normalization.
  - Decode Zepp's cumulative heart-rate delta stream into timestamped samples.
  - Preserve workout scope and avoid claiming the originating sensor when Zepp omits
    per-sample device provenance.
  - Persist and expose the normalized series without storing test fixtures from the
    real account; add focused decoder, storage, and API tests.
- [x] Part 6 - Synchronize project documentation and verify the complete change.
  - Update `README.md` and `docs/ARCHITECTURE.md` with the verified workout-only
    high-frequency interface, source-attribution limitation, and current test total.
  - Run focused tests, the complete suite, and `git diff --check`; record all results.
- [x] Part 7 - Commit and push the corresponding feature.
  - Review the diff for secrets and unrelated files.
  - Commit the tested code and Markdown together, push `main` to `origin`, and record
    the resulting commit and remote synchronization state.

### Verification Log

- Part 1: the worktree was clean; local `main` and `origin/main` both pointed to
  `661430a` (`feat: implement Vitalis Zepp health platform`).
- Part 2: the required workflow and completion rules now explicitly require incremental
  TODO updates, focused unit tests, synchronized Markdown, a traceable commit, and a
  successful remote push for every delivered feature.
- Part 3: ZeppBridge reads workout history through its generic extractor, including the
  verified `data.summary` array, prioritizes each record's numeric sport type, fetches
  every detail through `/v1/sport/run/detail.json`, cumulatively decodes the compressed
  heart-rate deltas, and replaces per-second rows in a dedicated `workout_samples`
  table. Its normalized samples do not establish a separate Helio sensor identity when
  the detail payload omits that provenance; Vitalis will preserve the same limitation.
- Part 4: Vitalis now reads real workout rows from `data.summary`, supports the verified
  heart-rate/load field names and epoch start/end times, and refuses to inherit the
  aggregate `/run/history.json` label for an unknown numeric sport type. `_payload_items`
  also recognizes `data.summary`. The focused parser/fetcher run passed 28 tests.
- Part 5: Vitalis now cumulatively decodes Zepp workout heart-rate deltas into UTC
  per-second `WorkoutSample` records, atomically replaces them in the new
  `workout_samples` table, and exposes them with workout detail while preserving user
  isolation. The raw vendor detail is reduced to normalized metadata. Focused parser,
  fetcher, sync, storage, and API tests passed 39 tests. A read-only real-account check
  decoded the latest available detail into 1,995 samples over 1,994 seconds; every row
  retained `source_scope=unknown` and no device identifier because the payload supplied
  no sensor provenance.
- Part 6: `README.md` and `docs/ARCHITECTURE.md` now distinguish minute-level all-day
  band heart rate from workout-only second-level detail, document `data.summary`, the
  `workout_samples` table and API surface, and explicitly prohibit inferred Helio
  attribution. A real-account compatibility pass normalized all 426 workout summaries,
  including decimal strings, `-1` sentinels and a maximum observed training load of
  851. The complete suite passed 83 tests in 2.75 seconds with 105 existing Python 3.14
  `datetime.utcnow()` deprecation warnings; Python compilation and `git diff --check`
  also passed.
- Part 7: secret review found no saved credential, real user/device/workout identifier,
  `.env`, or database file in the diff. Commit `6a6bbda` (`feat: ingest Zepp workout
  heart rate samples`) was pushed from local `main` to `origin/main` successfully.

## 8. Current Work Session - 2026-08-27 (Zepp Pairing Diagnostics)

Goal: finish the still-open real-browser acceptance for Part 12 by diagnosing why an
already signed-in Zepp page does not produce a credential submission from extension
0.2.2, without exposing or uploading cookie values.

### Detailed TODO

- [x] Part 1 - Restore and verify the real acceptance environment.
  - Run the backend on loopback and expose it through a browser-trusted temporary HTTPS
    tunnel.
  - Verify health, pairing-page rendering, extension archive integrity, and extension
    version 0.2.2.
  - Reproduce the real-browser attempt and inspect only non-secret request/state data.
- [x] Part 2 - Add privacy-preserving local cookie diagnostics.
  - Enumerate only cookies visible under the existing Zepp/Huami host permissions.
  - Store and display cookie count, known-login-cookie match count, and unique
    name/domain pairs locally in the extension; never include cookie values or upload
    diagnostics to Vitalis.
  - Clear stale diagnostics after a successful credential submission and bump the
    extension version.
- [x] Part 3 - Add focused regression coverage and publish the diagnostic build.
  - Assert the diagnostics contain no cookie value and use only local extension storage.
  - Run extension syntax checks and focused tests, then the complete suite.
  - Download the HTTPS archive and verify its version, integrity, and diagnostic code.
- [x] Part 4 - Complete real-browser acceptance and delivery.
  - Reload the diagnostic extension, repeat a fresh pairing, and use the local result to
    identify a changed cookie contract, missing permission, or web-storage migration.
  - Resolve the discovered cause with focused tests, observe a real credential POST,
    connected pairing, browser-link creation, and sync outcome without exposing secrets.
  - Synchronize Markdown, commit the completed change, and push `main`.

### Verification Log

- Part 1: the clean `main` branch started from `af098a8`. All 83 tests passed with 105
  existing Python 3.14 deprecation warnings, both extension scripts passed `node
  --check`, and the database contained no browser link. A new HTTPS tunnel passed its
  health check and served a valid extension 0.2.2 ZIP. The user's real attempt produced
  pairing-status GET requests but no credential POST; the fresh pairing remained
  `waiting` and no browser link was created.
- Part 2: extension 0.2.3 locally enumerates only cookies under exact Zepp/Huami root or
  subdomains, stores counts and unique name/domain pairs without values, renders the
  result in the popup, and clears it after successful pairing. No diagnostic data is
  added to a Vitalis request or server endpoint. Five focused extension tests and both
  JavaScript syntax checks passed.
- Part 3: the complete suite passed all 84 tests with 105 existing Python 3.14
  deprecation warnings, and `git diff --check` passed. The live HTTPS endpoint served a
  valid ZIP whose manifest reports 0.2.3 and whose background worker contains the
  local-only diagnostic collector. The tunnel health check remained HTTP 200.
- Part 4 (diagnosis): the real 0.2.3 popup saw one unrelated gateway cookie on
  `user.zepp.com` and zero known login cookies, proving cookie permission worked but the
  former login-cookie contract no longer represented the active session. The current
  public Watchface JavaScript explicitly reads `apptoken` from `localStorage` to decide
  whether the user is signed in, so the fallback must bridge fixed page-storage keys
  from an allowlisted Zepp/Huami origin instead of asking the user to sign in again.
- Part 4 (handoff diagnosis): extension 0.2.4 confirmed the Watchface origin contained
  no `apptoken` or user-id key; its session storage contained only a cached user profile.
  The current public application constructs an explicit Zepp `universalLogin` URL with
  `project_name=watchface`, a Watchface callback, and `com.huami.webapp`. Opening only
  the Watchface home page skipped that account-center-to-application token handoff.
- Part 4 (login requirement): extension 0.2.5 reached the official `universalLogin`
  origin, but the real browser exposed only analytics/monitoring storage keys and no
  vendor credential. The current official login bundle configures Watchface with
  `getTokenMethod: "cookies"` and writes `hm-user-login-info`, `apptoken`, and `userid`
  only after its login submission succeeds. A subsequent screenshot showed the
  Watchface application itself displaying the signed-in account, so the missing result
  is instead consistent with a partitioned/profile-specific cookie-store mismatch in
  the extension background API. The content script must also inspect the current
  page's first-party `document.cookie` view for only the fixed credential names.
- Part 4 (real pairing): extension 0.2.6 read the first-party page cookie and completed
  the real credential POST. The pairing became connected, one digest-only browser link
  was created and verified, and the extension performed a linked credential update.
  Two near-simultaneous background syncs then contended on SQLite; a swallowed
  `OperationalError` left one session rollback-only and its error handler also hit the
  database lock. The connected link survived, but synchronization must be serialized
  per user and storage errors must abort their transaction before final delivery.
- Part 4 (sync fix and acceptance): all sync-manager instances now share a per-user
  process lock, SQLAlchemy errors abort the active transaction immediately, and generic
  storage failures keep a verified browser link connected instead of requesting a new
  login. The focused API/sync run passed 35 tests and three targeted regressions passed.
  After restarting the backend, a real seven-day sync returned HTTP 200 with all seven
  streams successful and 3,362 normalized records written; no database-lock error
  followed. The complete suite passed all 89 tests with 113 existing Python 3.14
  deprecation warnings, all three extension scripts passed syntax checks, and `git
  diff --check` passed.
- Part 4 (delivery): commit `e44a615` (`feat: complete Zepp browser pairing`) was
  pushed from local `main` to `origin/main` successfully.

## 9. Current Work Session - 2026-08-28 (Complete Zepp Data Coverage)

Goal: ingest every real Zepp stream that can be identified and normalized without
inventing fields, preserve Balance 2 and Helio Strap provenance, verify the longest
practical history and credential-continuity boundary, and start the Zepp OS device-side
fallback when browser credentials cannot be renewed indefinitely.

### Detailed TODO

- [x] Part 1 - Establish the clean baseline and audit the latest reference behavior.
  - Confirm local `main` and `origin/main` start at `ea6e18a` with a clean worktree.
  - Review the latest public Zepp reference changes and the official Zepp OS sensor,
    background-service, messaging, and HTTP capabilities.
  - Record only aggregate real-account evidence; never persist credentials, device
    identifiers, file identifiers, signed URLs, or measured health values in Git.
- [x] Part 2 - Inventory every real-account cloud capability and response contract.
  - Probe inline V2 events, user events, date-string events, and file-info events over a
    cadence-appropriate read-only window.
  - Attribute supported streams to Balance 2, Helio Strap, user-fused, or unknown using
    verified vendor device maps.
  - Separate unavailable streams from available-but-not-yet-decoded streams.
- [x] Part 3 - Complete structured all-day and wellness normalization.
  - Decode minute-level `band_data.data_hr` with timestamps and device provenance when
    supplied.
  - Preserve per-sample SDNN/RMSSD attribution, Charge/body-battery, readiness including
    skin-temperature and sleep-derived fields, stress summaries, SpO2/ODI, PAI, and
    lactate-threshold fields that are present in verified response shapes.
  - Add focused parser, fetcher, storage, and synchronization regressions for each new
    normalized contract.
- [x] Part 4 - Represent and investigate dense second-heart-rate files honestly.
  - Fetch `second_heart_rate/real_data` file indexes and store only normalized metadata
    needed for resumable ingestion, with device attribution and no signed URL leakage.
  - Determine the official file-resolution/download contract and decode `SEC_HR` only
    if the binary format can be verified from real data or a trustworthy public source.
  - Never present file metadata as decoded heart-rate samples; expose capability and
    coverage separately until actual samples pass validation.
- [x] Part 5 - Perform real historical synchronization and coverage acceptance.
  - Synchronize the longest safe history supported by each stream with chunking that
    avoids vendor response caps and SQLite contention.
  - Report record counts, date coverage, device attribution, gaps, and residual raw-only
    streams without exposing identifiers or measured values.
  - Run focused tests, the complete suite, syntax checks, and `git diff --check`.
- [x] Part 6 - Establish the credential-continuity boundary.
  - Verify browser-link renewal while the official session remains valid and distinguish
    server token validity from extension cookie visibility.
  - Document that logout, cookie expiry, password changes, and vendor risk controls
    cannot be bypassed without an official refresh credential.
  - Fix any false `needs_login` transition that can be reproduced while the saved
    credential still validates.
- [x] Part 7 - Start the Balance 2 Zepp OS fallback channel if renewal is finite.
  - Scaffold a Zepp OS API_LEVEL-compatible device app, persistent app service, and
    companion-side upload path for Balance 2.
  - Collect only supported sensor data with explicit permissions and bounded buffering;
    do not claim Helio Strap support because it is not a Zepp OS app device.
  - Add local validation instructions and keep cloud synchronization as the historical
    source of truth.
- [x] Part 8 - Synchronize documentation and deliver.
  - Update `README.md` and `docs/ARCHITECTURE.md` with implemented streams, provenance,
    history limits, renewal limits, and the Zepp OS fallback boundary.
  - Review changes for secrets and unrelated files, commit the verified implementation,
    push `main`, record commit hashes, and confirm local and remote branches match.

### Verification Log

- Part 1 (baseline): local `main` and `origin/main` both started at `ea6e18a`; the
  worktree was clean. The latest public reference commits dated 2026-08-25 add event
  capability probing, three event surfaces, ODI and body/training views, but still stop
  at `second_heart_rate` file metadata rather than downloading `SEC_HR` payloads.
- Part 1 (Zepp OS boundary): official documentation lists Balance 2 at API_LEVEL 4.2
  and Zepp OS 5.0, while Helio Strap is absent from the Zepp OS app-device list. Device
  app services may use HeartRate in the background but not high-power Accelerometer;
  a companion service can relay binary messages and upload through HTTP.
- Part 2 (initial capability evidence): a read-only real-account probe found available
  RMSSD/SDNN, stress, body-battery, readiness, daily health, SpO2/ODI, respiratory rate,
  PAI, lactate threshold, and `second_heart_rate` file indexes. Blood pressure, emotion,
  weight, and standalone skin-temperature events were empty; readiness still contained
  skin-temperature, AHI, AFib, sleep-HRV, and sleep-RHR fields.
- Part 2 (dense-heart evidence): the latest seven-day index contained `SEC_HR` entries
  for both devices. Helio Strap entries represented much longer sessions (about 147.5
  indexed hours total, 106.7 minutes average, 7.8 hours maximum) than Balance 2 entries
  (about 34.9 hours total, 20.1 minutes average, 1.7 hours maximum). These are coverage
  metadata, not decoded heart-rate readings.
- Part 2 (verified shapes): real `band_data.data_hr` decodes to at most 1,440
  single-byte minute slots positioned by the item's local date and summary timezone;
  Charge, SDNN and RMSSD use `startTime` plus millisecond offsets, while readiness
  directly exposes skin-temperature, AHI, AFib, sleep-HRV and sleep-RHR fields.
- Part 3 (structured normalization): minute heart rate, timestamped SDNN/RMSSD and
  Charge/readiness samples, skin-temperature and sleep-derived readiness fields,
  stress summaries, SpO2/ODI, PAI, respiratory rate and lactate threshold now use
  vendor-neutral metric models. Device attribution participates in the time-series
  uniqueness key so simultaneous Balance 2 and Helio Strap samples are not collapsed.
- Part 4 (dense files): `second_heart_rate/real_data` indexes are normalized into a
  dedicated table with coverage, device attribution and `parse_status=indexed`.
  Signed URLs are not stored or exposed, file IDs are withheld from the health API,
  and no samples are claimed because no verified `SEC_HR` payload contract was found.
- Part 5 (real acceptance): a real 30-day end-to-end run completed all eight streams
  successfully and performed 191,716 normalized write operations without an auth or
  SQLite-lock failure. The live database contains structured coverage back to late
  August 2025. A separate 365-day dense-index backfill processed all 53 seven-day
  windows and retained 3,850 unique indexes spanning 2025-10-16 through 2026-08-28,
  covering 316 dates and two device groups. The single absent dense-index date and
  sparse SpO2/readiness dates remain honest vendor/wear gaps rather than fabricated
  values.
- Part 6 (credential boundary): extension 0.2.7 validates the saved server credential
  when the browser cookie is temporarily invisible. `ZeppAuthError` now distinguishes
  explicit 401/403 credential rejection from transient network/service failures; the
  latter returns HTTP 503 and leaves the browser link connected. The focused API/sync
  run passed 41 tests. The browser link is not an official refresh credential, so
  logout, session expiry, password changes and vendor risk controls still require the
  user to log in again.
- Part 7 (Balance 2 fallback): added an API_LEVEL 4.2 Zepp OS app, bounded 3,600-sample
  queue, background HeartRate callback service, phone-side authenticated HTTPS upload,
  and digest-only server device links. High-power Accelerometer collection and Helio
  Strap support are explicitly excluded. Three static contract tests and all nine
  JavaScript syntax checks passed.
- Part 8 (verification): `README.md` and `docs/ARCHITECTURE.md` now describe all eight
  streams, provenance, dense-file limitations, credential continuity and the device
  fallback. The complete suite passed 107 tests with 149 existing Python 3.14
  deprecation warnings; `git diff --check` and the added-content secret scan passed.
- Part 8 (delivery): commit `09f2102` (`feat: expand Zepp health coverage`) was pushed
  from local `main` to `origin/main` successfully.

## 8. Workout Type Fidelity Session

Date: 2026-08-28

Goal: correct the real Zepp strength workouts that Vitalis currently normalizes as
`other`, retain raw vendor type provenance for future mapping audits, and regenerate
the current weekly training interpretation.

### Detailed TODO

- [x] Part 1 - Preserve and map verified workout types.
  - Record real-account evidence without identifiers or raw payloads.
  - Map verified Zepp OS type `52` (`0x34`) to strength training.
  - Preserve the numeric vendor type ID in normalized workout records so unknown types
    remain auditable instead of becoming irreversible `other` values.
- [x] Part 2 - Verify analysis and deliver.
  - Add parser/storage regressions and run focused plus complete tests.
  - Re-sync the current week and verify running/strength counts and today’s decision.
  - Update affected documentation and test totals, commit, and push `main`.

### Verification Log

- The real weekly workout summaries contained three type `52` records and two type `1`
  records. Public Zepp OS definitions independently identify `0x34` (`52`) as strength
  training; no personal identifiers or raw payloads were persisted in Git.
- The parser now maps `52` to strength and retains every numeric vendor type ID,
  including unknown values. Focused parser/storage/intelligence tests passed 31 tests.
- A seven-day real sync completed all streams and rewrote the week as three strength
  sessions plus two runs. The regenerated profile reports 230 total minutes, 65 known
  aerobic minutes, three strength sessions, and a moderate 45-60 minute Zone 2
  recommendation for the current day.
- The complete suite passed 111 tests with 151 existing Python 3.14 deprecation
  warnings; `git diff --check` passed.
- Delivery: commit `7816a09` (`fix: preserve Zepp strength workout types`) was pushed
  to `origin/main`.

## 9. Daily Pipeline Fidelity Session

Date: 2026-08-28

Goal: audit the real Zepp-to-DailyProfile pipeline after the workout mapping fix and
correct any remaining loss of target-day HRV, local time, workout detail, or identity.

### Detailed TODO

- [x] Part 1 - Trace real data end to end.
  - Compare normalized RMSSD streams, sleep, workout rows, analyzer features, and the
    final decision for the connected local user.
  - Verify each weekly workout type and each target-day HRV device stream.
- [x] Part 2 - Correct natural-day semantics.
  - Keep timestamp storage in UTC while grouping analysis by `Asia/Shanghai` day.
  - Render Zepp sleep start/end using its supplied timezone offset and assign workouts
    to their local start date.
- [x] Part 3 - Preserve analysis-facing detail.
  - Expose all device streams for the selected HRV metric with independent deviations.
  - Expose recent workout type, vendor type, duration, load, HR summary, and type counts.
  - Require explicit user identity in scoped APIs and Hermes tools.
- [x] Part 4 - Verify and document.
  - Re-sync the real seven-day window, regenerate the profile, run the complete suite,
    and update architecture and operator documentation.

### Verification Log

- The audit found that UTC midnight, rather than the configured personal day, split
  overnight RMSSD. The truncated target-day medians were 69/61 ms; grouping the same
  source measurements by Shanghai day produced 71/68 ms.
- A successful real seven-day sync wrote 40,087 normalized records. Sleep now renders
  00:58-08:36 for the target day instead of the UTC clock values 16:58-00:36.
- The current weekly profile explicitly contains three strength sessions (vendor type
  52) and two runs (vendor type 1), including each session's duration, vendor load,
  average/max heart rate, and detail availability.
- The regenerated decision remains `TRAIN_NORMAL`, moderate, 45-60 minutes, with Zone 2
  as the suggested type. The profile still states that aerobic intensity classification
  is unavailable; the suggestion is not a claim that prior running minutes were Zone 2.
- The complete suite passed 117 tests with 153 existing Python 3.14 deprecation
  warnings; schema JSON parsing and `git diff --check` passed.

## 10. Chinese Workout Intelligence Session

Date: 2026-08-28

Goal: preserve every publicly defined Zepp workout mode, expose explicit recognition
confidence, render all user-facing analysis in Chinese, and return deterministic,
actionable running and strength prescriptions instead of generic activity names.

### Detailed TODO

- [x] Part 1 - Complete and verify workout-mode normalization.
  - Map the public Zepp OS workout enum to stable codes, Chinese labels, broad types,
    and training families without guessing unknown future IDs.
  - Add explicit recognition confidence/source fields and retain unknown numeric IDs.
  - Lock representative decimal IDs and unknown behavior with parser tests.
- [x] Part 2 - Complete deterministic training prescriptions and Chinese output.
  - Return Chinese action, confidence, intensity, evidence, limitation, recovery,
    sleep, and load labels while retaining internal codes for programmatic consumers.
  - Return structured running, strength, recovery, and rest prescriptions from the
    decision engine; do not let Hermes invent exercise content.
  - Render specific workout modes and prescription steps in morning/evening pushes.
- [x] Part 3 - Synchronize the Hermes Skill and wire schema.
  - Teach every workflow to consume Chinese label and prescription fields only.
  - Extend the DailyProfile JSON Schema for workout-mode, recognition, state-label,
    localization, and prescription contracts.
  - Validate the Skill with the skill-creator validator.
- [x] Part 4 - Verify real data, document, and deliver.
  - Re-sync the current seven-day window and regenerate the connected user's profile.
  - Verify workout types, target-day HRV, Chinese decision output, and executable
    training content end to end.
  - Run focused and complete tests, compilation, JSON checks, and `git diff --check`;
    update documentation, commit, and push `main`.

### Verification Log

- Part 1: the workout catalog matches all 120 entries in the current public Zepp OS
  enum and adds the two distinct public legacy Huami cloud-history IDs. Decimal IDs
  `1`, `6`, `8`, `9`, `10`, `18`, `52`, `92`, `146`, and `191` are locked by tests;
  missing IDs and unknown `999` remain explicit and carry no recognition confidence;
  endpoint or text hints are never used as compatibility fallbacks.
- Part 2: the decision engine returns Chinese presentation fields and structured Zone
  2 running, full-body strength, recovery, and rest prescriptions. Push rendering uses
  only Chinese labels and includes exact workout mode and recognition confidence.
  Focused parser/analyzer/push coverage passed 40 tests.
- Part 3: all four Hermes workflows consume Chinese label and prescription fields, and
  the JSON Schema now covers mode recognition and training steps. The skill-creator
  validator passed; its validation also removed the unsupported legacy `version`
  frontmatter key while preserving wire-contract versioning.
- Part 4 (real acceptance): a seven-day real sync completed all eight streams and wrote
  41,242 normalized records. The current profile contains two separate target-day HRV
  streams, 447 minutes of sleep, RHR 47 bpm, three strength sessions and two outdoor
  runs over 230 minutes. All five recent workout types have high recognition
  confidence. The engine returns normal training, moderate confidence/intensity, and a
  45-60 minute Zone 2 running prescription in Chinese.
- Part 4 (verification): all 131 tests passed with 153 existing Python 3.14
  `datetime.utcnow()` deprecation warnings. Python compilation, JSON parsing, live
  DailyProfile schema validation, skill validation, and `git diff --check` passed.
- Part 4 (delivery): commit `c94ddd9` (`feat: add Chinese workout prescriptions`) was
  pushed from local `main` to `origin/main` successfully.
- Final no-fallback audit: commit `38d29f1` (`fix: require authoritative workout type
  ids`) removed endpoint/text inference and was pushed to `origin/main`.

## 11. Personal Health Intelligence 2.0 Session

Date: 2026-08-28

Goal: evolve the daily-only engine into a deterministic personal-health intelligence
pipeline with trends, persistent health events, weekly analysis, analysis snapshots,
subjective feedback, and Hermes Read/Analyze/Act tools. Hermes remains an interface;
all statistics, classifications, and recommendations remain owned by Vitalis.

### Detailed TODO

- [x] Part 1 - Add the deterministic trend engine.
  - Load 180 days of normalized history so current and previous 90-day windows remain
    available while preserving metric/device streams.
  - Compute current/previous period medians, percentage change, slope, variability,
    coverage, direction, and confidence for 7/28/90-day windows.
  - Never merge HRV devices or use missing observations as zero values.
- [x] Part 2 - Add health-event detection and persistence.
  - Detect only explicit, explainable persistence and period-change events for HRV,
    RHR, sleep, training load/gaps, recovery, and activity.
  - Return severity, duration, confidence, evidence, and Chinese presentation fields;
    do not diagnose disease or claim causality.
  - Persist stable event identities and support user acknowledgement.
- [x] Part 3 - Add WeeklyProfile with fact/inference/action separation.
  - Aggregate sleep, recovery signals, training, activity, trends, events, and feedback
    for one local week with comparison to the preceding week.
  - Produce deterministic weekly recommendations and explicit missing-data limits.
  - Keep factual measurements, Vitalis inferences, and actions in separate contracts.
- [x] Part 4 - Add snapshots and the subjective-feedback loop.
  - Persist versioned DailyProfile and WeeklyProfile snapshots idempotently.
  - Store session RPE, physical fatigue, mental state, muscle soreness, and optional
    notes with bounded validation and user/workout isolation.
  - Expose recent feedback to WeeklyProfile without inventing absent responses.
- [x] Part 5 - Expand the Intelligence API and Hermes Skill v2.
  - Add daily, weekly, trends, events, explanation/context, feedback, and event
    acknowledgement endpoints with explicit user identity.
  - Add Hermes Read/Analyze/Act tools and route workflows to structured contracts.
  - Keep synchronization as an explicit action and prohibit model-side aggregation.
- [x] Part 6 - Verify real data, document, and deliver.
  - Add focused engine/storage/API/Skill tests and run the complete suite.
  - Validate JSON Schemas, Skill structure, Python compilation, OpenAPI, and diffs.
  - Generate real daily/weekly/trend/event results without exposing identifiers or raw
    health payloads, then update docs, commit, and push `main`.

### Verification Log

- Parts 1-3: focused trend, event, weekly, analyzer, and profile tests passed. Coverage
  includes source/device isolation, current-versus-previous 7/28/90-day periods,
  explicit insufficient data, persistent event rules, stable event IDs, and weekly
  fact/inference/action separation.
- Parts 4-5: snapshot and feedback tests verify idempotent DailyProfile/WeeklyProfile
  storage, bounded subjective values, workout/user isolation, weekly feedback use, and
  persisted weekly event retrieval. API tests cover all eight v2 Intelligence paths,
  user-scoped event acknowledgement, feedback isolation, and removal of the old
  `/intelligence/daily-profile` compatibility route. The official Skill validator
  accepts the Hermes Read/Analyze/Act package.
- Real-data acceptance: the 2026-08-28 DailyProfile had complete sleep and two separate
  HRV streams, and produced a normal-training Zone 2 recommendation. WeeklyProfile
  recognized three strength sessions and two outdoor runs totaling 230 minutes; trend
  and event responses were generated without merging device streams. One daily and one
  weekly versioned snapshot were persisted. No identifiers or raw payloads were logged.
- Part 6 verification: all 149 tests passed with 169 existing Python 3.14
  `datetime.utcnow()` deprecation warnings. Python compilation, all Skill tool help
  entrypoints, Skill validation, four real-response JSON Schema validations, OpenAPI
  generation (31 paths, eight Intelligence paths), `git diff --check`, and the
  added-content secret scan passed.

## 12. New-data-only Development Policy Session

Date: 2026-08-28

Goal: make the repository-wide pre-production policy explicit so future changes target
only current contracts and newly ingested data, with no implicit compatibility or old
data fallback work.

### Detailed TODO

- [x] Part 1 - Add the global new-data-only policy.
  - Prohibit legacy endpoints, aliases, schema adapters, dual reads/writes, migrations,
    backfills, reinterpretation, and old-contract fixtures by default.
  - Treat affected existing local data as disposable and require re-ingestion under the
    current contract.
  - Preserve explicit missing-data abstention without confusing it with compatibility
    fallback behavior.
- [x] Part 2 - Verify and deliver the documentation contract.
  - Confirm the rule is global rather than scoped only to one implementation session.
  - Run Markdown diff validation, commit the policy, and push it to `origin/main`.

### Verification Log

- The policy is defined in `3.1 Pre-production New-data-only Policy`, before all work
  session records, so it applies to every future repository change.
- This is a documentation-only execution-policy change; application tests are not
  affected. `git diff --check` passed.

## 13. Personal Health Intelligence 3.0 Session

Date: 2026-08-28

Goal: move Vitalis from period analysis into an auditable action-response-learning
loop. This session uses only the new contracts defined here; obsolete score storage,
old schemas, compatibility migrations, and fallback reads are removed rather than
adapted.

### Detailed TODO

- [x] Part 1 - Remove obsolete score storage and compatibility behavior.
  - Delete prototype recovery/overall scores, `HealthDaily`, score aggregation, and
    obsolete response fields from the normalized data path.
  - Remove lightweight schema migrations and assume a fresh database populated only by
    current normalization contracts.
  - Keep true missing observations explicit; never replace them with old scores or zero.
- [x] Part 2 - Separate analysis commands from read-only queries.
  - Add immutable AnalysisRun and snapshot records with intelligence, decision-policy,
    and evidence versions.
  - Make analysis an explicit command and make Daily, Weekly, Trends, Events, Explain,
    and Context GET operations side-effect free.
  - Split scheduler Sync, Analyze, and Push stages while preserving failure isolation.
- [x] Part 3 - Establish the recommendation/action/feedback identity chain.
  - Persist a RecommendationInstance for each daily decision.
  - Support explicit user-scoped linking from one recommendation to one completed
    workout; do not infer completion from time or text.
  - Require workout identity for session RPE while retaining independent daily
    fatigue, mental-state, soreness, and note observations.
- [x] Part 4 - Implement Training Response and Personal Model v1.
  - Compare each eligible workout with device-isolated pre-workout baselines and
    T+1/T+2/T+3 HRV, RHR, sleep, and linked subjective observations.
  - Flag overlapping workouts and missing windows instead of claiming clean recovery;
    define return-to-baseline deterministically.
  - Aggregate responses by training family and sport mode using median, MAD, sample
    count, coverage, and explicit confidence without a synthetic stress score.
- [x] Part 5 - Add event lifecycle management.
  - Keep DETECTED, PERSISTING, IMPROVING, and RESOLVED separate from acknowledgement.
  - Persist daily event observations and deterministic transitions.
  - Expose active and resolved events without regenerating analysis in a read request.
- [x] Part 6 - Add Health Timeline and bounded layered Context.
  - Project recommendations, workouts, feedback, health events, and response summaries
    into a chronological, typed timeline without copying raw samples.
  - Replace embedded full profiles with Current, Recent, Trend, and Personal context
    layers and enforce response-size/count limits.
- [x] Part 7 - Complete API, Hermes, documentation, verification, and delivery.
  - Update current-only API and Skill contracts; remove obsolete tools and schemas.
  - Add focused storage/engine/API tests and run the complete suite.
  - Validate OpenAPI, JSON Schemas, Skill structure, Python compilation, context size,
    diffs, and secrets; then commit and push `main`.

### Verification Log

- Part 1: obsolete daily score models, score aggregation, compatibility migrations,
  and fallback HRV storage were removed. Focused profile, API, health-data, and sync
  coverage passed 57 tests using only the current normalized contracts.
- Part 2: `POST /api/v1/intelligence/analyze` now creates one auditable AnalysisRun and
  immutable Daily/Weekly snapshots with separate intelligence, decision-policy, and
  evidence versions. All Intelligence GET operations read persisted results only and
  return 404 when a requested snapshot does not exist. Connect and scheduler paths use
  the explicit command, while push remains renderer-only. Focused storage, weekly, API,
  sync, and push coverage passed 58 tests.
- Part 3: every daily decision now has a persisted RecommendationInstance linked to its
  AnalysisRun. A user-scoped completion action explicitly links one recommendation to
  one real workout; ownership, relinking, and duplicate-claim checks prevent inferred
  or ambiguous completion. Session RPE requires a workout ID, and recommendation-aware
  feedback must match the completed link. Focused contracts, analyzers, storage,
  weekly, and API coverage passed 61 tests.
- Part 4: Training Response v1 compares each eligible workout with its preceding
  device-isolated 28-day baselines and explicit T+1/T+2/T+3 HRV, RHR, sleep, and linked
  subjective observations. Missing/future windows remain missing, overlapping workouts
  mark the result confounded, and return-to-baseline uses a documented deterministic
  multi-signal rule. Personal Model v1 groups response distributions by training family
  and sport mode using median, MAD, sample count, coverage, and coverage-derived
  confidence. No combined stress score or causal claim is produced. Focused response,
  personal-model, storage, analyzer, and API coverage passed 59 tests.
- Part 5: Health events now transition independently through DETECTED, PERSISTING,
  IMPROVING, and RESOLVED. Every analysis writes an immutable per-run event observation;
  repeated analysis on the same date cannot advance an event through multiple absence
  states. Acknowledgement remains a separate timestamp and survives every physiological
  lifecycle transition. Focused event, storage, and API coverage passed 53 tests.
- Part 6: Health Timeline projects only typed summaries for analysis runs,
  recommendations, workouts, feedback, event transitions, and training responses; it
  never embeds raw measurement or workout samples. Agent Context 3.0 now contains only
  bounded Current, Recent, Trend, and Personal layers with explicit list caps instead
  of full Daily/Weekly payloads. Focused timeline, context, storage, and event coverage
  passed 14 tests, including a serialized response-size assertion below 20 KB.
- Part 7: the current-only API exposes explicit Analyze plus read-only Daily, Weekly,
  Trends, Events, Explain, Context, Training Responses, Personal Model, Timeline, and
  Recommendation queries/actions. Hermes v3 has matching Read/Analyze/Act tools and
  schemas, while remaining renderer/orchestrator-only. The full suite passed all 161
  tests with 184 existing Python 3.14 `datetime.utcnow()` deprecation warnings. Python
  compilation, all Skill tool help entrypoints, Skill validation, eight live-response
  JSON Schema validations, OpenAPI generation (37 paths), and `git diff --check` passed.
  Validation used a fresh current-schema database; no legacy database was migrated,
  read, backfilled, or used as a fallback.
- Part 7 delivery: implementation commit `aa04d31` (`feat: add personal health
  intelligence loop`) was pushed from local `main` to `origin/main` successfully.

## 14. Longitudinal Health Intelligence Session

Date: 2026-08-28

Goal: extend the current-only intelligence pipeline with a directly computed 28-day
MonthlyProfile and deterministic 60/90-day personal associations. All results belong
to one immutable AnalysisRun, preserve metric and device identity, and remain bounded
structured inputs for Hermes rather than model-side calculations.

### Detailed TODO

- [x] Part 1 - Define MonthlyProfile and personal-association contracts.
  - Define one month as the 28 local days ending on the requested date and compare it
    with the immediately preceding 28 local days; do not support calendar-month or
    legacy variants.
  - Preserve fact, inference, and action separation, explicit missingness, version
    identity, source/device identity, and Chinese presentation fields.
  - Define associations as descriptive patterns only, never diagnoses or causal claims.
- [x] Part 2 - Implement MonthlyProfile from normalized history.
  - Recompute sleep, recovery observations, training, activity, feedback, changes,
    events, and recommendations directly from normalized records.
  - Never construct MonthlyProfile by combining WeeklyProfile snapshots.
  - Exclude absent observations rather than replacing them with zero or vendor scores.
- [x] Part 3 - Implement deterministic 60/90-day personal associations.
  - Use Spearman rank correlation with deterministic average ranks for ties and
    pairwise exclusion of missing days.
  - Require at least 30 paired days for 60-day windows and 45 for 90-day windows,
    adequate coverage, and meaningful variation in both variables.
  - Keep HRV and RHR streams isolated by metric, source, scope, device, and unit; add
    overlap and observational-design limitations instead of causal interpretation.
- [x] Part 4 - Persist and expose longitudinal intelligence.
  - Persist immutable MonthlyProfile and personal-association snapshots under the same
    successful AnalysisRun as Daily, Weekly, Training Response, and Personal Model.
  - Add current-only, read-only API queries and incorporate supported patterns into
    Personal Model v2, Timeline, and bounded Current/Recent/Trend/Personal Context.
  - Keep every GET side-effect free and return explicit not-found/insufficient results.
- [x] Part 5 - Extend Hermes and documentation.
  - Add Read tools and JSON Schemas for MonthlyProfile and personal associations.
  - Route monthly, personal-pattern, and explanation requests to Vitalis outputs; Hermes
    must not recompute correlations, aggregate periods, or infer causal relationships.
  - Update README and architecture documentation with current contracts and boundaries.
- [x] Part 6 - Verify, deliver, and record the result.
  - Run focused and full tests, Python compilation, Skill validation, OpenAPI checks,
    live JSON Schema validation, context-size validation, diff checks, and secret scan.
  - Commit and push the implementation to `origin/main`, then record the exact delivery
    commit and final verification state in this session.

### Verification Log

- Parts 1-3: MonthlyProfile uses one fixed 28-local-day period and the preceding 28
  days, recomputing facts directly from normalized history. Personal associations use
  deterministic Spearman average ranks across fixed 60/90-day candidates, pairwise
  missing-data exclusion, 30/45-pair minimums, 50% coverage, meaningful-variation
  gates, and full metric/source/scope/device/unit identity. Training on an outcome day
  is recorded as a potential confounder and can lower confidence. Focused tests verify
  positive and negative device-isolated HRV associations, tied values, insufficient
  variation, exact month boundaries, and explicit missing training records.
- Part 4: one successful AnalysisRun now persists immutable Daily, Weekly, Monthly,
  Training Response, Personal Association, and Personal Model v2 snapshots. New GET
  queries are read-only; supported associations are bounded in Personal Model, Context
  4.0, and Timeline summaries. Missing training coverage remains null and produces an
  abstention instead of a zero-derived volume recommendation.
- Part 5: Hermes exposes dedicated Monthly and Personal Association Read tools, a
  monthly workflow, and current JSON Schemas. It is explicitly prohibited from
  assembling monthly output, recomputing correlations, or converting association into
  causality or a new action. The official Skill validator and every tool `--help`
  entrypoint passed.
- Part 6 verification: all 165 tests passed with 184 existing Python 3.14
  `datetime.utcnow()` deprecation warnings. Python compilation, `git diff --check`, and
  added-content secret scanning passed. A fresh current-schema database produced five
  live responses that passed JSON Schema validation; OpenAPI contains 39 paths, the
  MonthlyProfile spans exactly 28 days, and serialized Context is 5,837 bytes against
  the 20 KB limit.
- Part 6 delivery: implementation commit `7a844dd` (`feat: add longitudinal health
  intelligence`) was pushed from local `main` to `origin/main` successfully.

## 15. Product README Session

Date: 2026-08-28

Goal: turn the repository root README into a product showcase that explains what
Vitalis does and why it is useful without forcing visitors through implementation
details. Preserve all operational and integration information in focused files under
`docs/` and keep the architecture contract in `docs/ARCHITECTURE.md`.

### Detailed TODO

- [x] Part 1 - Establish the publication structure.
  - Reserve README for product positioning, user questions, experiences, capability
    summary, trust boundaries, current status, and documentation navigation.
  - Move installation, deployment, scheduling, test commands, API tables, credentials,
    data-stream details, and repository internals out of README.
- [x] Part 2 - Create focused technical guides.
  - Add Getting Started, Zepp Integration, and API documents without dropping current
    setup, security, data-coverage, endpoint, or verification information.
  - Link detailed intelligence policies to Architecture instead of duplicating them.
- [x] Part 3 - Rewrite the product README.
  - Lead with the personal health intelligence product rather than the implementation.
  - Show representative Morning, Evening, Weekly, Monthly, training-response, and
    conversational experiences without presenting fabricated live user measurements.
  - State the deterministic/LLM boundary, explicit missing-data behavior, device
    isolation, non-diagnostic scope, and current supported ecosystem in plain language.
- [x] Part 4 - Verify and deliver.
  - Check internal Markdown links, headings, formatting, moved-content coverage, and
    `git diff --check`.
  - Commit and push the documentation restructure to `origin/main`, then record the
    delivery commit here.

### Verification Log

- Parts 1-3: the root README is now a 194-line product page centered on the questions
  Vitalis answers, representative Morning/Evening and longitudinal experiences,
  training response, Agent interaction, product capabilities, and trust boundaries.
  Project layout, setup, deployment, scheduler, test commands, credential mechanics,
  source payload semantics, API tables, and request examples were removed from README.
- Technical content now lives in `docs/GETTING_STARTED.md`,
  `docs/ZEPP_INTEGRATION.md`, `docs/API.md`, and the existing architecture document.
  The documentation map identifies one owner for each type of information.
- Part 4 verification: all local Markdown links resolve, heading structure and moved
  technical-content coverage were reviewed, `git diff --check` and the added-content
  secret scan passed, and the full suite passed all 165 tests with 184 existing Python
  3.14 `datetime.utcnow()` deprecation warnings.
- Part 4 delivery: commit `3708aeb` (`docs: turn README into product showcase`) was
  pushed from local `main` to `origin/main` successfully.

## 16. Hermes Runtime Integration Session

Date: 2026-08-28

Goal: connect the local Hermes Agent runtime to the current Vitalis Health Intelligence
API so Hermes can read and render one user's Balance 2 and Helio-derived structured
results without performing health calculations or merging device streams.

### Detailed TODO

- [x] Part 1 - Register the current Vitalis Skill with Hermes.
  - Install the repository Skill into the local Hermes Skill discovery path without
    copying or forking its contracts.
  - Keep Hermes as a Read / Analyze / Act orchestrator over the checked-in tools.
  - Verify Hermes discovers and enables the Skill from a new session.
- [x] Part 2 - Configure the explicit local runtime identity and API origin.
  - Configure `VITALIS_API` for the loopback Vitalis service and set the explicit
    current local user ID in Hermes' private environment.
  - Do not add an implicit user fallback, expose vendor credentials, or commit local
    identifiers and secrets.
- [x] Part 3 - Verify the live Vitalis boundary end to end.
  - Start the current Vitalis API against the existing current-schema database.
  - Run a fresh deterministic analysis and verify Daily, Context, workout, and source
    identity responses for the configured user.
  - Confirm missing signals remain missing and Balance 2 / Helio-derived streams are
    not averaged or relabeled by Hermes.
- [x] Part 4 - Document, test, and deliver the integration contract.
  - Add only reusable Hermes runtime setup details to technical documentation; keep
    machine-local IDs and credentials out of the repository.
  - Add focused coverage for repo-to-Hermes discovery or runtime configuration where
    the repository owns the behavior.
  - Run focused and complete verification, commit and push the repository changes, and
    record the exact live integration result here.

### Verification Log

- Part 1: `/root/.hermes/skills/health/vitalis` is a symbolic link to the checked-in
  repository Skill, and its content matches the repository without a copy or fork.
  Hermes Agent 0.20.5 lists `vitalis` as `health / local / enabled`. A fresh offline
  prompt-size session resolved and loaded the linked `vitalis/SKILL.md`.
- Part 2: Hermes' private environment contains the loopback
  `VITALIS_API=http://127.0.0.1:8000`, an explicit current `VITALIS_USER`, and loopback
  proxy exclusion. The local identifier remains outside the repository, and the Skill
  has no implicit identity fallback.
- Part 3: the current-schema real database contained sleep, activity, metric, daily
  metric, and workout records through 2026-08-28. A fresh analysis for that latest
  complete day succeeded under contracts Daily 3.0, Context 4.0, Training Response
  1.0, and intelligence policy 3.0. Daily, Context, and all 25 training responses shared
  one immutable AnalysisRun. Two independently attributed HRV device groups remained
  separate in Daily and Training Response output. An inspected workout detail retained
  2,829 sensor-unattributed samples as `source_scope=unknown` and `device_id=null`.
  Insufficient 90-day trends, missing training-response windows, and unavailable
  associations remained explicit rather than being filled or recalculated.
- Part 3 (Hermes): one fresh Hermes session invoked `daily.py` and rendered only the
  returned Chinese labels, prescriptions, limitations, nulls, and separate device
  streams. A second fresh session invoked both `context.py` and
  `training_responses.py`, confirmed the shared run identity, summarized available and
  missing states, and did not output health measurements or device identifiers.
- Part 4 (pre-delivery verification): `docs/GETTING_STARTED.md` now documents
  repository-linked Hermes discovery,
  private runtime configuration, and fresh-session checks without a machine-local user
  identity. Two focused tests cover the absent user fallback, loopback API default, API
  origin override, and exact `X-User-Id` forwarding. The focused Skill suite passed all
  5 tests; the complete suite passed all 167 tests in 2.47 seconds with 184 existing
  Python 3.14 `datetime.utcnow()` deprecation warnings. Repository and Skill tool
  compilation plus `git diff --check` passed.
- Part 4 (delivery): commit `ef263bb` (`docs: integrate Hermes runtime`) was pushed
  from local `main` to `origin/main` successfully.

## 17. Hermes Daily PushPlus Automation Session

Date: 2026-08-29

Goal: run one explicit-user Vitalis morning pipeline every day under Hermes Cron and
deliver the already-computed Chinese DailyProfile through PushPlus, while keeping the
PushPlus token private and keeping health calculations inside Vitalis.

### Detailed TODO

- [x] Part 1 - Add the PushPlus delivery boundary.
  - Read `PUSHPLUS_TOKEN` only from the private runtime environment and never place it
    in a URL, repository file, log, exception, or model prompt.
  - Send only the rendered title and body to the documented PushPlus HTTPS endpoint.
  - Treat transport and PushPlus application errors as failed delivery and add focused
    unit coverage without live network access.
- [x] Part 2 - Add the deterministic daily Act tool.
  - Require the explicit `VITALIS_USER` and configured PushPlus token before doing work.
  - Synchronize the current user, run one fresh deterministic analysis, render the
    returned DailyProfile, and send exactly one morning report.
  - Keep Hermes as the scheduler/orchestrator; do not ask an LLM to calculate health
    values, handle the token, or assemble shell-escaped report content.
- [x] Part 3 - Make the local runtime persistent.
  - Run the Vitalis loopback API as a restartable boot service with its embedded
    scheduler disabled to prevent duplicate pushes.
  - Run Hermes Gateway as a boot service and register one daily 09:30 Asia/Shanghai
    cron job linked to the checked-in Act tool.
  - Verify service health, job discovery, explicit identity, and missing-token behavior
    without sending a live notification before the user supplies the token.
- [x] Part 4 - Document, verify, and deliver.
  - Document the one private token variable, daily schedule, service operations, manual
    verification, and failure visibility without committing local identifiers.
  - Run focused and complete tests, compilation, format and secret checks.
  - Commit and push the implementation, then record exact delivery evidence here.

### Verification Log

- Part 1: `PushService` now enables PushPlus only when `PUSHPLUS_TOKEN` is present,
  sends the token in an HTTPS JSON body, and validates both HTTP and PushPlus application
  status. Error results contain only the application status code, not the token,
  response body, or health message. Five focused push-service tests passed with all
  network access mocked.
- Part 2: `tools/daily_push.py` requires the explicit runtime user and token before it
  creates a network client. Its service path performs exactly one two-day sync, one
  deterministic analysis, and one PushPlus morning delivery. Failed synchronization
  stops before analysis and push. The combined daily-tool, PushPlus, and Skill suite
  passed all 13 tests; missing-user and missing-token CLI preflights exited before any
  live request.
- Part 3: `vitalis.service` and `hermes-gateway.service` are enabled and active under
  systemd. Vitalis listens only on `127.0.0.1:8000`, passes `/healthz`, and has its
  embedded scheduler disabled. Hermes Cron has one active no-agent job named
  `vitalis-morning-pushplus`, scheduled for `30 9 * * *`; the system timezone is
  synchronized `Asia/Shanghai` and Hermes reports the next run at `09:30 +08:00`.
  A manual missing-token cron run failed at preflight with exit code 2, while the
  Vitalis service received no synchronization or analysis request.
- Part 4 (pre-delivery verification): reusable documentation now covers the private
  token, one 09:30 dispatcher, service status, cron history, and manual acceptance
  commands without a local user ID or job ID. Focused automation tests passed all 13
  tests. The complete suite passed all 173 tests in 4.28 seconds with 184 existing
  Python 3.14 `datetime.utcnow()` deprecation warnings. Repository and Skill tool
  compilation, Skill validation, and `git diff --check` passed. Live PushPlus acceptance
  was intentionally deferred until the user supplied the private token.
- Part 4 (delivery): commit `8081dda` (`feat: automate daily PushPlus report`) was
  pushed from local `main` to `origin/main` successfully. The live runtime remains
  enabled and will read the private token at execution time without a service restart.
- Part 4 (live acceptance): after the user added the token to the private Hermes
  environment, the existing cron job discovered it without a restart and completed a
  manual run successfully. Vitalis recorded one two-day synchronization with HTTP 200
  followed by one deterministic analysis with HTTP 201; PushPlus accepted the rendered
  report, and the next automatic run remained scheduled for 09:30 Asia/Shanghai. No
  token, local user ID, device ID, or health measurement was recorded in the repository.

## 18. Daily Report Date and PushPlus Formatting Fix

Date: 2026-08-29

Goal: prevent an early-day PushPlus report from choosing an empty current-day profile
when the previous day has a complete independent profile, and render the selected
DailyProfile as readable WeChat Markdown.

### Detailed TODO

- [x] Part 1 - Select one honest report date.
  - Analyze the current local day first; use it when all required signals are present.
  - If required signals are missing, analyze the previous local day independently and
    select it only when its required-signal coverage is better.
  - Never merge observations across dates, silently relabel yesterday as today, or hide
    remaining missing data; include the selected data date in the title and body.
- [x] Part 2 - Redesign the PushPlus report renderer.
  - Use Markdown sections, bullets, emphasis, and spacing that survive WeChat rendering.
  - Never render dangling units such as `暂无 分钟`; absent values render as `暂无`.
  - Separate status, recommendation, drivers, limitations, workout review, and
    structured prescription content instead of collapsing them into one paragraph.
- [x] Part 3 - Add regression coverage and deliver a corrected live report.
  - Cover current-day selection, previous-day selection, no cross-day merging, explicit
    report date, Markdown structure, and missing-value unit handling.
  - Run focused and complete verification, restart no services unless required, and
    trigger the existing Hermes cron job for one corrected PushPlus acceptance report.
- [x] Part 4 - Commit, push, and record delivery.
  - Review for health-data, identifier, and token leakage.
  - Push the implementation and exact anonymized live acceptance evidence.

### Verification Log

- Part 1: the morning job now sends explicit `day` parameters. It analyzes the current
  local day first and analyzes the previous day only when current required signals are
  missing. The previous DailyProfile is selected only when it has fewer missing required
  signals; the selected response object is passed intact without cross-day merging.
  Five focused date-selection and pipeline tests passed.
- Part 2: the report title and body now identify the selected ISO data date. Markdown
  headings, blockquote metadata, bullets, emphasis, and blank lines separate status,
  recommendation, drivers, limitations, workout review, and prescription content.
  Missing values render as `暂无` without a dangling unit. The combined date-selection
  and renderer suite passed all 11 tests.
- Part 3: the complete suite passed all 176 tests in 3.34 seconds with 184 existing
  Python 3.14 `datetime.utcnow()` deprecation warnings; compilation and
  `git diff --check` passed. The existing Hermes cron job completed a corrected live
  run: synchronization returned HTTP 200, independent analyses for the current and
  previous local dates both returned HTTP 201, and the persisted non-health result was
  `status=sent`, `quality=SUFFICIENT`, and `used_previous_day=true`. The selected report
  date was the previous complete day, and the next automatic 09:30 run remained active.
- Part 4 (delivery): commit `26a1166` (`fix: select complete daily report`) was pushed
  from local `main` to `origin/main` successfully. The corrected PushPlus message was
  accepted before delivery, so the live cron path and the pushed implementation match.

## 19. Sleep-Aware Morning Retry and Distinct Evening Report

Date: 2026-08-29

Goal: defer the morning PushPlus report until today's sleep record is complete, retry
hourly without duplicate delivery, and add a materially distinct 22:30 evening report
with interpreted recovery and training context.

### Detailed TODO

- [x] Part 1 - Make morning delivery sleep-aware and idempotent.
  - Analyze only the current local date and defer without sending while sleep is
    unavailable or has no wake time; never substitute the previous day.
  - Retry hourly from 09:30 until the evening window and send exactly once after sleep
    completes, using private atomic delivery state to prevent duplicate sends.
- [x] Part 2 - Separate morning and evening report contracts.
  - Keep the morning report focused on interpreted recovery and the training plan.
  - Make the evening report summarize actual workouts, today's and seven-day load,
    recovery signals, trends, events, and the engine's evening action.
  - Keep deterministic interpretations ahead of measurements and place data
    limitations at the end of both reports.
- [x] Part 3 - Configure and verify the live runtime.
  - Change the morning Hermes Cron job to hourly retries and add one no-agent 22:30
    evening job without exposing the private token or local identity.
  - Verify deferred, sent, and already-sent behavior plus service and cron health.
- [x] Part 4 - Clean, document, test, and deliver.
  - Update workflow and operator documentation to match actual runtime behavior.
  - Run focused and complete tests, compilation, formatting, and secret checks.
  - Commit and push the implementation, then record anonymized delivery evidence.

### Verification Log

- Part 1: the daily pipeline now analyzes only the current local date. Morning delivery
  requires both an available sleep feature and a wake time; otherwise it returns
  `status=deferred` without constructing the PushPlus service or writing a success
  marker. Per-user, per-date, per-period file locks serialize overlapping runs, private
  atomic markers skip later successful retries, and failed delivery remains retryable.
- Part 2: the Morning renderer leads with recovery, sleep, action, confidence, positive
  and negative recovery signals, and 28-day deviation interpretation before supporting
  measurements and the structured training plan. The Evening renderer instead leads
  with completed workouts and tonight's action, then renders exact workout modes,
  vendor load, seven-day context, recovery signals, available trends, and active event
  summaries. Real current-profile render checks passed for both periods without sending;
  in both messages `数据限制` was the final heading.
- Part 3: Hermes Gateway now has two active no-agent jobs. Morning runs at
  `30 9-21 * * *` and Evening runs at `30 22 * * *`; both persistent services remain
  enabled and active. A live current-day sync showed unavailable sleep and no wake time,
  and the actual Morning script returned `status=deferred` without a PushPlus send or
  success marker. Loopback API calls now explicitly bypass environment proxies after a
  manual run exposed an inherited-proxy 502; the retried live path completed normally.
- Part 4 (pre-delivery verification): 20 focused automation, renderer, and Skill tests
  passed. The complete suite passed all 180 tests in 2.38 seconds with 184 existing
  Python 3.14 `datetime.utcnow()` deprecation warnings. Repository and Skill tool
  compilation, line-length review, `git diff --check`, and private-value diff scans
  passed.
- Part 4 (delivery): implementation commit `8c49d87`
  (`feat: add sleep-aware daily reports`) was pushed from local `main` to `origin/main`.
  A final repeat
  of the complete suite passed all 180 tests in 2.22 seconds. `hermes status` reported
  the gateway running with both jobs active, `hermes gateway status` reported the
  boot-enabled system service active, the latest Morning execution was completed, and
  `hermes doctor` finished with all required checks passed.
