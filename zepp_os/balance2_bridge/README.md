# Vitalis Balance 2 Bridge

Zepp OS fallback for Balance 2 only. Helio Strap cannot run Zepp OS apps and is
therefore not supported by this channel.

The background app service records `HeartRate.onCurrentChange()` callbacks into a
bounded 3,600-sample local queue. Zepp does not document a fixed callback frequency,
so these records are callback-level measurements, not a guaranteed 1 Hz stream. The
foreground page sends queued batches through the phone-side service to Vitalis over
HTTPS. Accelerometer collection is intentionally absent because high-power sensors
are not supported in a background app service.

## Setup

1. Replace the placeholder `app.appId` in `app.json` with an app ID owned by the
   developer account.
2. From Vitalis, call `POST /api/v1/connect/zepp/device-link` with `X-User-Id` and
   retain the returned device token; the server stores only its SHA-256 digest.
3. In the Zepp app settings, enter the public Vitalis HTTPS base URL and the device
   token.
4. Install dependencies, preview on Balance 2, grant heart-rate/background
   permissions, and start background collection.

```bash
npm install
npm run preview
npm run build
```

Open the device app periodically and choose `同步到 Vitalis` to flush the bounded
queue. Cloud sync remains the historical source of truth.
