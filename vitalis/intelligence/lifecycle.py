"""Deterministic health-event lifecycle reconciliation."""

from datetime import date
from uuid import uuid4

from vitalis.storage import HealthRepository

from .contracts import (
    EventLifecycle,
    EventSeverity,
    HealthEvent,
    HealthEventObservation,
)


LIFECYCLE_LABELS = {
    EventLifecycle.DETECTED: "已发现",
    EventLifecycle.PERSISTING: "持续中",
    EventLifecycle.IMPROVING: "改善中",
    EventLifecycle.RESOLVED: "已恢复",
}


class EventLifecycleEngine:
    def reconcile(
        self,
        repo: HealthRepository,
        analysis_run_id: str,
        user_id: str,
        target: date,
        detected: list[HealthEvent],
    ) -> list[HealthEvent]:
        active = repo.active_health_events(user_id)
        active_by_key = {(item.type, item.metric): item for item in active}
        detected_keys: set[tuple[str, str | None]] = set()
        output: list[HealthEvent] = []

        for candidate in detected:
            key = (candidate.type, candidate.metric)
            detected_keys.add(key)
            previous = active_by_key.get(key)
            if previous is None:
                lifecycle = EventLifecycle.DETECTED
                event = candidate.model_copy(update={
                    "lifecycle": lifecycle,
                    "lifecycle_label": LIFECYCLE_LABELS[lifecycle],
                    "last_observed_date": target,
                    "last_evaluated_date": target,
                })
                previous_lifecycle = None
            else:
                if previous.last_evaluated_date == target:
                    lifecycle = previous.lifecycle
                elif _is_improving(previous, candidate):
                    lifecycle = EventLifecycle.IMPROVING
                else:
                    lifecycle = EventLifecycle.PERSISTING
                event = candidate.model_copy(update={
                    "id": previous.id,
                    "start_date": previous.start_date,
                    "end_date": target,
                    "duration_days": (target - previous.start_date).days + 1,
                    "lifecycle": lifecycle,
                    "lifecycle_label": LIFECYCLE_LABELS[lifecycle],
                    "last_observed_date": target,
                    "last_evaluated_date": target,
                    "acknowledged": previous.acknowledged,
                    "acknowledged_at": previous.acknowledged_at,
                    "resolved_at": None,
                })
                previous_lifecycle = previous.lifecycle
            stored = repo.save_health_event(user_id, event)
            repo.save_event_observation(_observation(
                analysis_run_id, user_id, target, stored, True, previous_lifecycle
            ))
            output.append(stored)

        for previous in active:
            if (previous.type, previous.metric) in detected_keys:
                continue
            lifecycle = EventLifecycle.IMPROVING
            resolved_at = None
            if (
                previous.lifecycle == EventLifecycle.IMPROVING
                and previous.last_evaluated_date is not None
                and previous.last_evaluated_date < target
            ):
                lifecycle = EventLifecycle.RESOLVED
                resolved_at = target
            event = previous.model_copy(update={
                "lifecycle": lifecycle,
                "lifecycle_label": LIFECYCLE_LABELS[lifecycle],
                "last_evaluated_date": target,
                "resolved_at": resolved_at,
            })
            stored = repo.save_health_event(user_id, event)
            repo.save_event_observation(_observation(
                analysis_run_id, user_id, target, stored, False, previous.lifecycle
            ))
            output.append(stored)

        return sorted(output, key=lambda item: (item.lifecycle.value, item.type, item.id))


def _observation(run_id, user_id, target, event, detected, previous_lifecycle):
    return HealthEventObservation(
        id=uuid4().hex,
        analysis_run_id=run_id,
        event_id=event.id,
        user_id=user_id,
        date=target,
        detected=detected,
        previous_lifecycle=previous_lifecycle,
        lifecycle=event.lifecycle,
    )


def _is_improving(previous: HealthEvent, current: HealthEvent) -> bool:
    severity_rank = {
        EventSeverity.INFO: 1,
        EventSeverity.MODERATE: 2,
        EventSeverity.HIGH: 3,
    }
    if severity_rank[current.severity] < severity_rank[previous.severity]:
        return True
    if previous.deviation_percent is None or current.deviation_percent is None:
        return False
    return abs(current.deviation_percent) <= abs(previous.deviation_percent) * 0.75
