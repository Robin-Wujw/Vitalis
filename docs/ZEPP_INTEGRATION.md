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
- serializes first-time, renewal, manual, and scheduled synchronization per user;
- reports `needs_login=true` only after an explicit vendor authentication rejection;
- issues a separate high-entropy browser-link token and stores only its SHA-256 digest;
- accepts synchronization windows up to 730 days and fetches history in bounded chunks;
- reports the eight streams independently: heart rate, daily summary, workouts,
  workout detail, sleep, HRV, wellness, and dense-file indexes.

Temporary cookie visibility or network failures do not automatically disconnect the
account. The extension calls `/connect/zepp/link/validate` to verify the server-side
credential state before reporting a loss of connection.

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

- minute heart rate from `band_data.data_hr`, up to 1,440 local-day slots;
- RMSSD, SDNN, sleep HRV/RHR, readiness components, skin-temperature delta, Charge,
  stress, SpO2/ODI, PAI, respiratory rate, and lactate-threshold fields when present;
- sleep duration, timing, stages, awakenings, scores, respiration, and other available
  vendor sleep fields;
- daily activity, resting heart rate, steps, active minutes, and training summaries;
- workout summaries plus typed workout-detail heart rate, speed, equivalent pace,
  cadence, distance, altitude, running power, laps, pauses, and explicit strength sets;
- dense `SEC_HR` file coverage metadata when the payload itself is not decoded.

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
heart rate, speed, equivalent pace, cadence, cumulative distance, altitude, running
power, laps, pauses, and explicit vendor strength sets when present.

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

`second_heart_rate/real_data` currently stores only opaque `SEC_HR` coverage metadata.
`sample_count=0` and `parse_status=indexed` explicitly mean that no measurement samples
have been decoded. File identifiers are not exposed through the health-query API.

## Balance 2 Bridge

`zepp_os/balance2_bridge/` contains an API_LEVEL 4.2 Zepp OS application skeleton for
Balance 2. Its background service listens only to system-permitted heart-rate callbacks,
buffers at most 3,600 records locally, and uploads through the phone side over HTTPS
using a one-time displayed device Bearer token.

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
