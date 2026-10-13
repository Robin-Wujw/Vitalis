"""Daily summaries of dense sample streams.

The analysis reads full samples only for the most recent days; older days use
one summary per local day and stream.  The summary keeps what the analysis
needs from the samples: the median of plausible values, the latest value,
the sample count and the observed minutes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from statistics import median

# Dense streams with many samples per day, with the plausible value range the
# signal registry accepts for each (``vitalis.intelligence.signals``).  Sparse
# vendor streams (one or a few values per day, such as readiness) keep their
# samples.
DAILY_FEATURE_RANGES: dict[str, tuple[float, float]] = {
    "hrv_rmssd": (1.0, 400.0),
    "hrv_sdnn": (1.0, 400.0),
    "spo2": (50.0, 100.0),
    "spo2_apnea_low": (50.0, 100.0),
    "stress": (0.0, 100.0),
    "hybrid_charge": (0.0, 100.0),
    "physical_charge": (0.0, 100.0),
    "mental_charge": (0.0, 100.0),
}
DAILY_FEATURE_METRICS = tuple(DAILY_FEATURE_RANGES)

# Days up to and including the target that analysis reads as full samples:
# the target night spans midnight, and same-day responses need sample times.
DETAIL_DAYS = 2


@dataclass(frozen=True)
class DailyFeature:
    metric: str
    day: date
    source: str
    source_scope: str
    device_id: str
    unit: str
    sample_count: int
    rejected_count: int
    observed_minutes: int
    median: float
    minimum: float
    maximum: float
    last_value: float
    first_observed_at: datetime
    last_observed_at: datetime


def summarize_day(
    metric: str,
    day: date,
    samples: list[tuple[datetime, float, str, str, str, str]],
) -> list[DailyFeature]:
    """Summarize ``(timestamp, value, source, scope, device_id, unit)`` rows by stream.

    Values outside the metric's plausible range are counted as rejected and
    left out of every statistic, matching the per-sample qualification.  A
    stream with no plausible value produces no feature.
    """
    low, high = DAILY_FEATURE_RANGES[metric]
    streams: dict[tuple[str, str, str, str], list[tuple[datetime, float]]] = {}
    rejected: dict[tuple[str, str, str, str], int] = {}
    for timestamp, value, source, scope, device_id, unit in samples:
        key = (source, scope or "unknown", device_id or "", unit or "")
        number = float(value)
        if not isfinite(number) or not low <= number <= high:
            rejected[key] = rejected.get(key, 0) + 1
            continue
        streams.setdefault(key, []).append((timestamp, number))
    features = []
    for (source, scope, device_id, unit), rows in sorted(streams.items()):
        values = [value for _, value in rows]
        last_at = max(timestamp for timestamp, _ in rows)
        features.append(DailyFeature(
            metric=metric,
            day=day,
            source=source,
            source_scope=scope,
            device_id=device_id,
            unit=unit,
            sample_count=len(rows),
            rejected_count=rejected.get((source, scope, device_id, unit), 0),
            observed_minutes=len({
                timestamp.replace(second=0, microsecond=0) for timestamp, _ in rows
            }),
            median=float(median(values)),
            minimum=min(values),
            maximum=max(values),
            last_value=float(median(value for timestamp, value in rows if timestamp == last_at)),
            first_observed_at=min(timestamp for timestamp, _ in rows),
            last_observed_at=last_at,
        ))
    return features
