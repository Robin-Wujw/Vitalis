"""Pure composition entry point for Open Health Insights 1.0."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from vitalis.intelligence.contracts import OpenHealthBundle

from .anomaly import compute_anomaly
from .common import OpenHealthObservation, profile_value, sorted_observations
from .readiness import compute_readiness
from .sleep import compute_sleep
from .load import compute_training_load


class OpenHealthEngine:
    """Compose shadow-only algorithms from explicit observations and profile."""

    def analyze_load(
        self,
        workouts: Iterable[Any],
        profile: Any = None,
        *,
        target_date: date | None = None,
        period_start: date | None = None,
        queried_days: Iterable[date] | None = None,
        rhr_by_day: dict[Any, Any] | None = None,
        upstream_coverage_verified: bool = False,
        input_truncated: bool = False,
    ):
        """Explicitly calculate load from caller-provided inputs; never reads DB."""
        return compute_training_load(
            workouts,
            profile,
            target_date=target_date,
            period_start=period_start,
            queried_days=queried_days,
            rhr_by_day=rhr_by_day,
            upstream_coverage_verified=upstream_coverage_verified,
            input_truncated=input_truncated,
        )

    def analyze(
        self,
        observations: Iterable[OpenHealthObservation | dict[str, Any]],
        profile: Any = None,
        *,
        target_date: date | None = None,
    ) -> OpenHealthBundle:
        rows = sorted_observations(list(observations))
        target = target_date or (rows[-1].date if rows else date.today())
        revision = int(profile_value(profile, "revision") or 0)
        return OpenHealthBundle(
            target_date=target,
            profile_revision_used=revision,
            readiness=compute_readiness(rows, target_date=target, profile_revision_used=revision),
            anomaly=compute_anomaly(rows, target_date=target, profile_revision_used=revision),
            sleep=compute_sleep(rows, profile, target_date=target, profile_revision_used=revision),
        )


def build_open_health_bundle(
    observations: Iterable[OpenHealthObservation | dict[str, Any]],
    profile: Any = None,
    *,
    target_date: date | None = None,
) -> OpenHealthBundle:
    return OpenHealthEngine().analyze(observations, profile, target_date=target_date)
