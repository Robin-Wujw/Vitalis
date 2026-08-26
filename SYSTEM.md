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

## 4. Project Documentation Map

- `README.md`: user-facing capabilities, setup, API usage, and current test result.
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
