"""Pure date-range aggregation over normalized daily records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .models import NormalizedDaily

Granularity = Literal["180d", "90d", "30d", "7d", "1d"]

GRANULARITY_DAYS: dict[Granularity, int] = {
    "180d": 180,
    "90d": 90,
    "30d": 30,
    "7d": 7,
    "1d": 1,
}


@dataclass
class AggregatedBlock:
    """Aggregated values for one bounded date block."""

    start: date
    end: date
    days_with_data: int = 0
    days_total: int = 0

    # Sleep aggregates.
    sleep_duration_avg: float | None = None
    deep_sleep_avg: float | None = None
    rem_sleep_avg: float | None = None
    light_sleep_avg: float | None = None
    awake_avg: float | None = None
    sleep_score_avg: float | None = None

    # Activity aggregates.
    steps_avg: float | None = None
    calories_total: int | None = None
    distance_km_total: float | None = None
    resting_hr_avg: float | None = None

    # Training aggregates.
    workout_count_total: int = 0
    training_duration_total: int = 0
    training_load_total: int | None = None

    raw_days: list[NormalizedDaily] = field(default_factory=list, repr=False)


def aggregate_block(block: AggregatedBlock) -> None:
    """Populate a block from its normalized daily source records.

    A value is included only when it is present and, for legacy fields whose
    default was zero, explicitly marked as observed. This keeps missing values
    separate from observed zeroes in the range response.
    """
    days = block.raw_days
    block.days_with_data = sum(
        any((day.sleep, day.activity, day.training)) for day in days
    )
    if not days:
        return

    # Sleep.
    sleep_days = [day.sleep for day in days if day.sleep]
    if sleep_days:
        count = len(sleep_days)
        block.sleep_duration_avg = round(
            sum(sleep.sleep_duration for sleep in sleep_days) / count, 1
        )
        for field_name, target in (
            ("deep_sleep", "deep_sleep_avg"),
            ("light_sleep", "light_sleep_avg"),
            ("awake", "awake_avg"),
        ):
            values = [
                getattr(sleep, field_name)
                for sleep in sleep_days
                if getattr(sleep, field_name) is not None
                and (
                    getattr(sleep, field_name) != 0
                    or field_name in sleep.observed_fields
                )
            ]
            if values:
                setattr(block, target, round(sum(values) / len(values), 1))
        rem_values = [
            sleep.rem_sleep for sleep in sleep_days if sleep.rem_sleep is not None
        ]
        if rem_values:
            block.rem_sleep_avg = round(sum(rem_values) / len(rem_values), 1)
        scores = [
            sleep.sleep_score for sleep in sleep_days if sleep.sleep_score is not None
        ]
        if scores:
            block.sleep_score_avg = round(sum(scores) / len(scores), 1)

    # Activity.
    activity_days = [day.activity for day in days if day.activity]
    if activity_days:
        def values(name: str) -> list:
            return [
                getattr(record, name)
                for record in activity_days
                if getattr(record, name) is not None
                and (
                    getattr(record, name) != 0
                    or name in record.observed_fields
                )
            ]

        steps = values("steps")
        calories = values("calories")
        distances = values("distance_km")
        block.steps_avg = round(sum(steps) / len(steps), 0) if steps else None
        block.calories_total = sum(calories) if calories else None
        block.distance_km_total = round(sum(distances), 2) if distances else None
        resting_hrs = [record.resting_hr for record in activity_days if record.resting_hr]
        if resting_hrs:
            block.resting_hr_avg = round(sum(resting_hrs) / len(resting_hrs), 1)

    # Training.
    training_days = [day.training for day in days if day.training]
    if training_days:
        block.workout_count_total = sum(day.workout_count for day in training_days)
        block.training_duration_total = sum(
            day.total_duration for day in training_days
        )
        loads = [day.total_load for day in training_days]
        block.training_load_total = (
            sum(loads) if all(value is not None for value in loads) else None
        )
