"""Input types and small normalization helpers for shadow-only insights."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator


class OpenHealthObservation(BaseModel):
    """One dated, already-normalized night; no database access is implied."""

    model_config = ConfigDict(extra="allow")

    date: date
    rmssd_ms: float | None = None
    rhr_bpm: float | None = None
    respiratory_rate: float | None = None
    rr_available: bool | None = None
    sleep_minutes: float | None = None
    time_in_bed_minutes: float | None = None
    bedtime: time | str | None = None
    wake_time: time | str | None = None
    nap_minutes: float | None = None
    naps_known: bool | None = None
    source: str = "unknown"
    source_scope: str = "nightly_observation"
    device_id: str | None = None
    sample_count: int | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_vendor_neutral_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        aliases = {
            "day": "date",
            "rmssd": "rmssd_ms",
            "hrv_rmssd": "rmssd_ms",
            "rhr": "rhr_bpm",
            "resting_hr": "rhr_bpm",
            "resp": "respiratory_rate",
            "respiratory_rate_bpm": "respiratory_rate",
            "sleep_duration_minutes": "sleep_minutes",
            "duration_minutes": "sleep_minutes",
            "tib_minutes": "time_in_bed_minutes",
            "time_in_bed": "time_in_bed_minutes",
            "bed_time": "bedtime",
            "wake": "wake_time",
            "nap": "nap_minutes",
        }
        for old, new in aliases.items():
            if new not in data and old in data:
                data[new] = data[old]
        return data


def as_observation(value: OpenHealthObservation | dict[str, Any]) -> OpenHealthObservation:
    return value if isinstance(value, OpenHealthObservation) else OpenHealthObservation.model_validate(value)


def sorted_observations(values: list[OpenHealthObservation | dict[str, Any]]) -> list[OpenHealthObservation]:
    return sorted((as_observation(value) for value in values), key=lambda item: item.date)


_TIMESTAMP_KEYS = (
    "observed_at",
    "timestamp",
    "recorded_at",
    "measured_at",
    "measurement_timestamp",
    "source_timestamp",
)


def _parse_observation_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def observation_timestamp(row: OpenHealthObservation) -> datetime | None:
    """Return an explicit measurement timestamp, if the input carries one."""
    extra = row.model_extra or {}
    for key in _TIMESTAMP_KEYS:
        timestamp = _parse_observation_timestamp(extra.get(key))
        if timestamp is not None:
            return timestamp
    return None


def _timestamp_sort_key(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def distinct_observations(
    values: list[OpenHealthObservation | dict[str, Any]],
    *,
    valid: Callable[[OpenHealthObservation], bool] | None = None,
) -> list[OpenHealthObservation]:
    """Choose one valid observation per calendar day without averaging sources.

    A dated fact with an explicit timestamp wins by timestamp. When timestamps
    are absent, the last valid input is retained as a deterministic tie-breaker;
    this keeps duplicate rows from inflating coverage while never synthesizing a
    value across sources.
    """
    selected: dict[date, tuple[OpenHealthObservation, int, datetime | None]] = {}
    for index, row in enumerate(sorted_observations(values)):
        if valid is not None and not valid(row):
            continue
        timestamp = observation_timestamp(row)
        previous = selected.get(row.date)
        if previous is None:
            selected[row.date] = (row, index, timestamp)
            continue
        previous_row, previous_index, previous_timestamp = previous
        if timestamp is not None:
            if previous_timestamp is None or _timestamp_sort_key(timestamp) >= _timestamp_sort_key(previous_timestamp):
                selected[row.date] = (row, index, timestamp)
        elif previous_timestamp is None and index >= previous_index:
            selected[row.date] = (row, index, timestamp)
        else:
            # An untimestamped row cannot displace an explicitly timestamped fact.
            selected[row.date] = (previous_row, previous_index, previous_timestamp)
    return [selected[day][0] for day in sorted(selected)]


def profile_value(profile: Any, name: str) -> Any:
    """Read either the current UserProfile field wrapper or a plain test object."""
    if profile is None:
        return None
    value = profile.get(name) if isinstance(profile, dict) else getattr(profile, name, None)
    if hasattr(value, "value"):
        value = value.value
    return value


def parse_clock(value: time | str | None) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, pattern).time()
            except ValueError:
                continue
    return None


def as_minutes(value: time) -> float:
    return value.hour * 60 + value.minute + value.second / 60
