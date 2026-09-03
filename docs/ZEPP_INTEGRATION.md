# Zepp Integration

This document describes browser pairing, credential handling, normalized data
coverage, workout heart rate, and the Balance 2 bridge.

## Browser Pairing

Vitalis connects through the user's official Zepp browser session. Account passwords
and verification codes are submitted only to the official login page and are never
received or stored by Vitalis.

1. Open `https://<vitalis-origin>/api/v1/connect/zepp/scan?user=<local-user-id>`.
2. Install the extension from `browser_extension/` as instructed by the pairing page.
3. Enter the displayed Vitalis origin and one-time pairing code in the extension.
4. Select “登录并自动连接”; the extension opens the official Zepp login page.
5. After successful official login, the extension submits the available temporary
   session credential and Vitalis begins synchronization.

The pairing implementation:

- extracts `userid`, `apptoken`, and region from the extension cookie API or the
  official page's first-party cookie view;
- probes the supported Zepp regional hosts and selects the working region;
- updates credentials when the official browser cookie changes and checks the browser
  session periodically;
- records first-time, renewal, manual, and scheduled synchronization in a persistent attempt/chunk ledger;
- fences attempt and chunk ownership with renewable leases so expired workers cannot commit or claim more work;
- resumes queued, retry-wait, expired, and explicitly cancelled work through the ASGI-owned dispatcher after restart;
- applies bounded exponential backoff to transient network/service failures while preserving completed chunks;
- reports `needs_login=true` only after an explicit vendor authentication rejection;
- issues a separate high-entropy browser-link token and stores only its SHA-256 digest;
- accepts synchronization windows up to 730 days and fetches history in bounded chunks;
- reports the eight streams independently: heart rate, daily summary, workouts,
  workout detail, sleep, HRV, wellness, and dense-file indexes.

Temporary cookie visibility or network failures do not automatically disconnect the
account. The extension calls `/connect/zepp/link/validate` to verify the server-side
credential state before reporting a loss of connection. A local Vitalis user cannot be
rebound to a different Zepp vendor account without first disconnecting and clearing that
user's data, preventing two vendor accounts from sharing one analysis history.

## Credential Lifecycle

Vendor responses commonly report:

| Field | Reported duration | Meaning |
| --- | --- | --- |
| `ttl` | 31,536,000 seconds | Browser login session lifetime |
| `app_ttl` | 3,456,000 seconds | App token lifetime |

These are vendor response fields, not official refresh credentials available to
Vitalis. The extension can update the app token only while the official browser
session remains valid and readable. Logout, session expiration, password changes, or
vendor risk controls may require a new official login.

## Normalized Data Coverage

Vitalis currently normalizes:

- type-2 `/users/{id}/heartRate` measurements encoded as `generatedTime` plus a
  single-byte base64 `heartRateData` value; multi-byte payloads remain unrecognized
  until their sampling interval and timestamp direction can be verified;
- minute heart rate from `band_data.data_hr`, up to 1,440 local-day slots;
- RMSSD, SDNN, sleep HRV/RHR, readiness components, skin-temperature delta, Charge,
  stress, SpO2/ODI, PAI, respiratory rate, and lactate-threshold fields when present;
- sleep duration, timing, stages, awakenings, scores, respiration, and other available
  vendor sleep fields;
- daily activity, resting heart rate, steps, active minutes, and training summaries;
- workout summaries plus typed workout-detail heart rate, speed, equivalent pace,
  cadence, distance, altitude, running power, laps, pauses, and explicit strength sets;
- decoded second-level heart rate from dense `SEC_HR` cloud files, with per-device
  coverage and decode status retained alongside the samples.

Dense indexes are fetched on every sync, but an archive is downloaded only when its
index gains an unseen interval. Intervals checked against a valid archive without a
matching sample block are retained as `no_data`, preventing an open-ended vendor
placeholder from forcing the same daily archive to be downloaded repeatedly.

HRV sources are not interchangeable:

- `readiness.sleepHRV` is one nightly recovery summary per device and is preferred for
  recovery/baseline decisions;
- `hrv_sdnn/real_data` is a separate sparse SDNN event stream and keeps an independent
  baseline;
- `HRVRMSSD/real_data` is a timestamped, non-continuous RMSSD event stream used only
  for the gap-aware daily record chart. On the connected devices it may contain long
  sleep runs and only short runs outside the main sleep interval; its first and last
  timestamps are not coverage.

The file-info endpoint returns ordinary inline events for both HRV event types on the
connected account, not downloadable archive descriptors. Only `second_heart_rate`
currently returns `fileId`/`fileType` archive indexes. Vitalis therefore does not
download an additional HRV file or derive HRV from the one-second heart-rate archive.

UTC measurements are assigned to local days using `VITALIS_TIMEZONE` (default
`Asia/Shanghai`). Sleep clocks preserve vendor-provided local offset semantics.

The public Zepp OS catalog contributes 120 current activity IDs, and two additional
public legacy Huami cloud-history IDs are mapped separately. Each known workout retains
its vendor ID, stable mode, exact Chinese label, training family, mapping source, and
recognition confidence. Unknown or missing IDs remain explicit and are never inferred
from an endpoint name or descriptive text.

## Workout Detail

Zepp workout history is returned by `/v1/sport/run/history.json`; the response may mix
multiple activities, so each record's numeric `type` is authoritative. Workout detail
comes from `/v1/sport/run/detail.json`. Vitalis normalizes its compressed series into
typed UTC observations in `workout_metric_samples`. The current contract supports
heart rate, speed, equivalent pace, cadence, stride length, cumulative distance, altitude, running
power, ground-contact time, vertical oscillation, vertical stride ratio, laps, pauses,
and explicit vendor strength sets when present. The three `runPosture` sentinels are
discarded rather than stored as zero. Workout summaries retain a valid six-boundary
`heart_range` and `heartrate_setting_type` for device-aligned zone analysis.

This is a workout-only stream, not continuous all-day high-frequency heart rate. The
detail response does not identify the sensor for each sample, so normalized detail
samples retain `source_scope=workout_detail` and `device_id=null`. A workout summary associated
with Balance 2 is not sufficient evidence to label its samples as Balance 2 or Helio.

Empty fields remain absent. In particular, a strength workout may contain second-level
heart rate and vendor assessment data while providing no explicit exercise sets. In
that case Vitalis does not infer an exercise name at the connector boundary or the
intelligence layer. It may estimate work/rest structure from heart rate while keeping
the movement and target muscle unknown until an explicit vendor set or user
confirmation is available.

`second_heart_rate/real_data` returns file indexes rather than samples. Ordinary health
sync stores those indexes without downloading large archives. When
`decode_dense_files=true` is explicitly requested, Vitalis decodes at most one new
archive through Zepp's official `queryDownUrlList` endpoint. The signed HTTPS ZIP is
downloaded without forwarding `apptoken`, then its protobuf heartbeat blocks are
stored as device-scoped heart-rate samples.
Each block starts at a Unix-second timestamp and contains consecutive one-second heart
rate values; `255` is treated as missing. ZIP entries are assigned to indexed devices
using a global one-to-one maximum-overlap match. Successfully decoded index rows store
`parse_status=decoded` and `sample_count`; later syncs skip those exact file/device/time
rows, so historical files are not downloaded repeatedly. File identifiers remain
private and are not exposed through the health-query API.

## Balance 2 Bridge

`zepp_os/balance2_bridge/` contains an API_LEVEL 4.2 Zepp OS application for Balance 2.
Its background service is the sole writer of an append-only callback journal; the
foreground page owns a separate acknowledgement checkpoint. Logical pending coverage is
bounded to the newest 3,600 callback records, while compaction and recovery counters make
capacity loss or storage damage visible. Zepp OS exposes no `fsync` durability guarantee.

The phone side uploads fixed high-water batches over HTTPS with a one-time displayed
Bearer token. The v2 endpoint settles exact client sample IDs after the metric transaction
commits and separately identifies permanent validation rejections. Network, authentication,
protocol, or server failures leave unsettled samples in place. Server-side `(user, source,
metric, timestamp, source_scope, device)` identity keeps committed batch replay idempotent.
For callbacks sharing one millisecond, the client sends `sample_ordinal`; persistence adds
that ordinal as a microsecond tie-breaker while retaining the original millisecond and
ordinal in the settlement response.

Callback frequency is controlled by Zepp OS and is not claimed to be fixed at 1 Hz.
The bridge does not continuously collect high-power accelerometer data. Helio Strap
cannot run Zepp OS applications, so this path applies only to Balance 2. Cloud sync
remains the primary history source.

Build and device-side setup instructions are in
[`zepp_os/balance2_bridge/README.md`](../zepp_os/balance2_bridge/README.md).

## Identity and Device Boundaries

Every request uses an explicit local user identity. Vendor identities are not silently
merged across local users. Metrics retain source, source scope, device ID, and unit;
the device inventory maps verified product IDs to Balance 2 and Helio Strap without
persisting device authentication material. Their HRV values remain separate: Vitalis
compares each stream with its own baseline and fuses only the resulting directions.
Upper-arm evidence can choose Helio as the display stream when equivalent baselines are
available, but it does not override cross-device disagreement or imply ECG equivalence.

User physiology such as sex, confirmed HRmax, and sleep target is stored separately in a revisioned Vitalis `UserProfile`. The current regional `apptoken` client does not call a guessed Zepp profile endpoint. Historical OAuth profile fields and Zepp OS device profile/zone settings are only future candidate sources; they never override `USER_CONFIRMED` values. Workout maximum heart rate and zone boundaries remain observations or device settings, not confirmed HRmax.

## Canonical Persistence and Sync Outcomes

Workout history can repeat the same day or workout across sport endpoints, pages, and
overlapping synchronization windows. Vitalis first upserts each stable workout identity,
then rebuilds every affected `training_records` day from the complete canonical workout
table across all connector sources using `VITALIS_TIMEZONE`. Page order therefore cannot
replace a multi-workout day, equal workout IDs from different sources remain separate, and
a corrected start timestamp updates both the old and new local dates.

Timestamped and daily metric identities include `source`, `source_scope`, and `device_id`;
two connectors, devices, or semantically different source scopes never overwrite or
aggregate into one another merely because their metric and time match. Raw, hourly, daily,
and sparse-daily API results expose that provenance. Daily timestamped-metric buckets use
`VITALIS_TIMEZONE`; missing device attribution remains absent at API and analysis boundaries.

HTTP 200 empty payloads, unsupported endpoints, authentication rejection, transient
network/service failure, and non-empty unrecognized payloads are separate outcomes. Only
an explicit `not_available` endpoint can be skipped as an optional capability and is
recorded as a substream diagnostic. Within one capability, all chunks must be available or
all unavailable: mixed successful and unavailable chunks are partial coverage and block
complete synchronization. Successful chunks completed before a later terminal error are
persisted before the stream is marked failed. Explicit local-date requests are serialized
back to vendor date parameters in `VITALIS_TIMEZONE`, not from their UTC boundary date.
A real optional-stream or dense-file network/authentication failure is retained as failed
and is never reported as an empty account. Authentication failures mark the browser link
for login; transient failures keep the connection and remain retryable.
