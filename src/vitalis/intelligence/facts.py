"""Pure target-day fact projection helpers."""

from __future__ import annotations

from datetime import date, datetime
from statistics import median
from typing import Any

from .contracts import MeasurementFact, Provenance


def facts_for_day(raw: Any) -> dict[str, list[MeasurementFact]]:
    """Build target-day facts without repository access or ambient state."""
    facts: dict[str, list[MeasurementFact]] = {}
    for metric, points in raw.series.items():
        current = [point for point in points if point.day == raw.day]
        if not current:
            continue
        streams: dict[tuple[str, str, str | None, str], list[Any]] = {}
        for point in current:
            streams.setdefault(
                (point.source, point.source_scope, point.device_id, point.unit),
                [],
            ).append(point)
        facts[metric] = [
            MeasurementFact(
                metric=metric,
                value=round(float(median(item.value for item in stream)), 3),
                unit=unit,
                observed_at=max(
                    stream,
                    key=lambda item: _observed_key(item.observed_at),
                ).observed_at,
                provenance=Provenance(
                    source=source,
                    source_scope=scope,
                    device_id=device_id,
                ),
            )
            for (source, scope, device_id, unit), stream in sorted(
                streams.items(),
                key=lambda item: tuple(value or "" for value in item[0]),
            )
        ]
    return facts


def _observed_key(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return value
