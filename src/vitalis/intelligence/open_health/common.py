"""Input types and small normalization helpers for shadow-only insights."""

from __future__ import annotations

from datetime import date, datetime, time
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
