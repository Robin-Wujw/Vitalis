# Vitalis Balance 2 Bridge

Zepp OS fallback for Balance 2 only. Helio Strap cannot run Zepp OS apps and is
therefore not supported by this channel.

The background app service is the sole writer of a versioned NDJSON journal. Each valid
`HeartRate.onCurrentChange()` callback receives a stable local sample ID and is appended
with one file write; the callback path does not parse, sort, or replace the full queue.
Zepp does not document a fixed callback frequency, so these records are callback-level
measurements, not a guaranteed 1 Hz stream. Zepp OS exposes no `fsync` guarantee, so the
bridge reports append and recovery status rather than claiming power-loss-proof storage.

The foreground page owns a separate recoverable acknowledgement checkpoint and uploads
one fixed high-water snapshot at a time. The v2 server response settles exact sample IDs:
committed or replayed samples and explicit permanent rejections leave the pending queue;
timeouts, malformed responses, authentication failures, and server errors retain them.
Deferred service maintenance repairs a torn final journal line, compacts settled records,
and retains the newest 3,600 pending records while reporting
capacity loss, permanent rejection, and corruption counters. The service is started with
Zepp OS's documented `file` property. Accelerometer collection is intentionally absent
because high-power sensors are not supported in a background app service.

## Setup

1. Replace the placeholder `app.appId` in `app.json` with an app ID owned by the
   developer account.
2. From Vitalis, call `POST /api/v1/connect/zepp/device-link` with `X-User-Id` and
   retain the returned device token; the server stores only its SHA-256 digest.
3. In the Zepp app settings, enter the public Vitalis HTTPS base URL and the device
   token.
4. Use Node 24 with npm 11, install the locked dependencies, preview on Balance 2,
   grant heart-rate/background permissions, and start background collection. The app
   targets Zepp OS API 4.2; the latest published `@zeppos/device-types` remains 4.0.0.

```bash
npm ci
npm test
npm run preview
npm run build
```

Open the device app periodically and choose `同步到 Vitalis` to flush the bounded
queue. Cloud sync remains the historical source of truth.
