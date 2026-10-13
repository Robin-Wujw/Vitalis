"""Pure stream-freshness calculations for stored health observations.

This module does not query persistence or infer absent records. Callers pass the
last observed timestamp and the actual observation dates they found in storage;
unknown values remain ``None`` rather than becoming zeroes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone


SEVEN_DAY_WINDOW = 7

# Expected intervals are intentionally conservative. They describe when a
# healthy source normally produces a new observation, not a promise that every
# user owns that capability.
EXPECTED_INTERVAL_SECONDS: dict[str, int] = {
    "heart_rate/minute_endpoint": 60 * 60,
    "heart_rate": 60 * 60,
    "sleep": 24 * 60 * 60,
    "hrv": 24 * 60 * 60,
    "workouts": 24 * 60 * 60,
    "workout_detail": 24 * 60 * 60,
    "heart_rate/dense_file": 24 * 60 * 60,
    "heart_rate/dense_archive": 24 * 60 * 60,
    "daily_summary": 24 * 60 * 60,
    "devices": 7 * 24 * 60 * 60,
    "wellness": 24 * 60 * 60,
}


def expected_interval_seconds(stream: str) -> int:
    """Return the expected observation interval for one health stream."""
    if stream in EXPECTED_INTERVAL_SECONDS:
        return EXPECTED_INTERVAL_SECONDS[stream]
    if stream.startswith("daily_summary/") or stream.startswith("wellness/"):
        return 24 * 60 * 60
    if stream.startswith("heart_rate/"):
        return 24 * 60 * 60
    return 24 * 60 * 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def freshness_state(
    last_observed_at: datetime | None,
    as_of: datetime,
    *,
    expected_seconds: int,
) -> tuple[str, float | None]:
    """Return ``(freshness, age_seconds)`` without fabricating a timestamp."""
    if last_observed_at is None:
        return "unknown", None
    age = max(0.0, (_as_utc(as_of) - _as_utc(last_observed_at)).total_seconds())
    return ("fresh" if age <= max(1, int(expected_seconds)) else "stale"), round(age, 3)


def coverage_7d(
    observed_dates: Iterable[date] | None,
    window_end: date,
    *,
    window_days: int = SEVEN_DAY_WINDOW,
) -> dict[str, object]:
    """Summarize only dates actually observed in the bounded calendar window.

    ``observed_days`` and ``coverage`` are ``None`` when no stored observation
    supports the window. A missing stream is therefore distinguishable from a
    real observation count of one or more days.
    """
    days = max(1, int(window_days))
    window_start = window_end - timedelta(days=days - 1)
    actual = {
        item for item in (observed_dates or ())
        if isinstance(item, date) and window_start <= item <= window_end
    }
    observed = len(actual) or None
    fraction = observed / days if observed is not None else None
    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "expected_days": days,
        "observed_days": observed,
        "coverage": fraction,
        "observed_dates": sorted(item.isoformat() for item in actual) or None,
    }


def stream_freshness(
    *,
    stream: str,
    last_observed_at: datetime | None,
    observed_dates: Iterable[date] | None,
    as_of: datetime,
    window_end: date,
) -> dict[str, object]:
    """Build the stable API fields for one stored synchronization stream."""
    expected = expected_interval_seconds(stream)
    freshness, age = freshness_state(
        last_observed_at,
        as_of,
        expected_seconds=expected,
    )
    return {
        "expected_interval_seconds": expected,
        "age_seconds": age,
        "freshness": freshness,
        "coverage_7d": coverage_7d(observed_dates, window_end),
    }


# Streams that are event-driven rather than periodic, so silence is normal:
# Zepp's ``hrv`` endpoint carries manual spot measurements (SDNN) only, the
# minute heart-rate endpoint is empty for watches that upload dense files,
# and workouts, details and devices follow the user's activity.
NOT_MONITORED_STREAMS = frozenset({
    "hrv",
    "heart_rate/minute_endpoint",
    "workouts",
    "workout_detail",
    "devices",
})
# A stale stream gets one automatic re-fetch; it is reported only when it is
# still stale this long after its expected interval and an hour after the
# re-fetch was queued.
ALERT_GRACE = timedelta(hours=6)
REFETCH_SETTLE = timedelta(hours=1)


def freshness_actions(
    streams: Iterable[dict],
    markers: dict[str, datetime],
    now: datetime,
) -> list[tuple[str, str, dict[str, object]]]:
    """Decide ``("refetch" | "alert", event_key, payload)`` for stale streams.

    ``streams`` are ``/api/data-status`` stream rows; ``markers`` maps the event
    keys already stored to when they were stored.  One staleness episode is one
    stream at one ``last_sample_at``: new data starts a new episode.
    """
    now = _as_utc(now)
    actions = []
    for row in streams:
        stream = str(row.get("stream") or "")
        if (
            not stream or stream in NOT_MONITORED_STREAMS
            or row.get("freshness") != "stale" or not row.get("last_sample_at")
        ):
            continue
        episode = f"{stream}@{row['last_sample_at']}"
        expected = timedelta(seconds=int(row.get("expected_interval_seconds") or 86_400))
        age = timedelta(seconds=float(row.get("age_seconds") or 0))
        payload = {
            "stream": stream,
            "last_sample_at": row["last_sample_at"],
            "expected_interval_seconds": int(expected.total_seconds()),
            "age_seconds": round(age.total_seconds()),
        }
        refetch_key = f"freshness_refetch:{episode}"
        if refetch_key not in markers:
            actions.append(("refetch", refetch_key, payload))
            continue
        alert_key = f"stream_stale:{episode}"
        if (
            alert_key not in markers
            and age >= expected + ALERT_GRACE
            and now - _as_utc(markers[refetch_key]) >= REFETCH_SETTLE
        ):
            actions.append(("alert", alert_key, payload))
    return actions
