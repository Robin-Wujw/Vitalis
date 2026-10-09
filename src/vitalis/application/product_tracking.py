"""Explicit product inputs, neutral persistence ports, and pure goal summaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from statistics import mean
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PRODUCT_TRACKING_VERSION = "1.0"
FeedbackKind = Literal[
    "report_usefulness", "data_correction", "recommendation_completion",
    "recommendation_outcome", "event_assessment",
]
_INPUT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProductTrackingValidationError(ValueError):
    """An explicit product request is invalid."""


class ProductTrackingConflict(ValueError):
    """A request cannot overwrite current user-owned state."""

    kind = "revision_conflict"


class ProductRevisionConflict(ProductTrackingConflict):
    """A mutable goal or feedback aggregate has a newer revision."""


class ProductIdempotencyConflict(ProductTrackingConflict):
    """A user reused a request key for another body or operation."""

    kind = "idempotency_conflict"


class ProductResourceNotFound(ValueError):
    """A reference is missing or is not owned by the authenticated user."""


class ProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    input_event_ref: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("input_event_ref")
    @classmethod
    def opaque_reference(cls, value: str | None) -> str | None:
        if value is not None and not _INPUT_REF.fullmatch(value):
            raise ValueError("input_event_ref must be an opaque non-sensitive reference")
        return value


class GoalInput(ProductInput):
    """Only an explicit confirmation creates an accepted goal.

    Training days and pain fields update the existing TrainingPreferences record.
    Its content fingerprint is required when changing any of those fields.
    """

    confirmed: Literal[True]
    expected_revision: int = Field(default=0, ge=0)
    goal_type: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
    target_value: float = Field(ge=0)
    target_unit: str = Field(min_length=1, max_length=24)
    target_date: date
    metric_key: str | None = Field(default=None, min_length=1, max_length=64)
    comparison: Literal["at_least", "at_most", "equal"] = "at_least"
    aggregation: Literal["mean", "sum", "latest"] = "mean"
    window_days: int = Field(default=7, ge=1, le=90)
    available_training_days: list[int] | None = Field(default=None, max_length=7)
    pain_or_injury_status: Literal["NONE", "PRESENT", "UNKNOWN"] | None = None
    pain_or_injury_notes: str | None = Field(default=None, max_length=500)
    expected_training_preferences_revision: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$",
    )

    @field_validator("target_unit", "metric_key")
    @classmethod
    def nonempty_unit_or_key(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("unit and metric key must be nonempty")
        return value

    @field_validator("available_training_days")
    @classmethod
    def iso_weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is not None:
            if len(set(value)) != len(value) or any(day < 1 or day > 7 for day in value):
                raise ValueError("available_training_days must be unique ISO weekdays 1 through 7")
            return sorted(value)
        return value

    @model_validator(mode="after")
    def preference_revision_required(self):
        fields = self.model_fields_set
        preference_fields = {
            "available_training_days", "pain_or_injury_status", "pain_or_injury_notes",
        }
        if fields & preference_fields and self.expected_training_preferences_revision is None:
            raise ValueError("training preference changes require their current revision")
        if "available_training_days" in fields and self.available_training_days is None:
            raise ValueError("use [] to explicitly clear available training days")
        if "pain_or_injury_status" in fields and self.pain_or_injury_status is None:
            raise ValueError("use UNKNOWN for an unknown pain/injury status")
        if self.pain_or_injury_status == "PRESENT" and not (self.pain_or_injury_notes or "").strip():
            raise ValueError("pain_or_injury_notes is required when pain or injury is present")
        return self


class GoalPatch(ProductInput):
    confirmed: Literal[True]
    expected_revision: int = Field(ge=1)
    goal_type: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
    target_value: float | None = Field(default=None, ge=0)
    target_unit: str | None = Field(default=None, min_length=1, max_length=24)
    target_date: date | None = None
    metric_key: str | None = Field(default=None, min_length=1, max_length=64)
    comparison: Literal["at_least", "at_most", "equal"] | None = None
    aggregation: Literal["mean", "sum", "latest"] | None = None
    window_days: int | None = Field(default=None, ge=1, le=90)
    available_training_days: list[int] | None = Field(default=None, max_length=7)
    pain_or_injury_status: Literal["NONE", "PRESENT", "UNKNOWN"] | None = None
    pain_or_injury_notes: str | None = Field(default=None, max_length=500)
    expected_training_preferences_revision: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$",
    )

    @field_validator("target_unit", "metric_key")
    @classmethod
    def nonempty_unit_or_key(cls, value: str | None) -> str | None:
        return GoalInput.nonempty_unit_or_key(value)

    @field_validator("available_training_days")
    @classmethod
    def iso_weekdays(cls, value: list[int] | None) -> list[int] | None:
        return GoalInput.iso_weekdays(value)

    @model_validator(mode="after")
    def patch_has_explicit_changes(self):
        immutable = {"confirmed", "expected_revision", "expected_training_preferences_revision", "input_event_ref"}
        if not self.model_fields_set - immutable:
            raise ValueError("goal patch must contain at least one change")
        nullable = {"pain_or_injury_notes", "metric_key"}
        for name in self.model_fields_set - immutable - nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        preference_fields = {"available_training_days", "pain_or_injury_status", "pain_or_injury_notes"}
        if self.model_fields_set & preference_fields and self.expected_training_preferences_revision is None:
            raise ValueError("training preference changes require their current revision")
        return self


class ProductFeedbackInput(ProductInput):
    """Four independent feedback records plus explicit event assessments.

    Updating an existing record uses its ID and current revision.  Every accepted
    revision is retained, and its kind and links cannot be silently reclassified.
    """

    confirmed: Literal[True]
    kind: FeedbackKind
    occurred_on: date | None = None
    feedback_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_revision: int = Field(default=0, ge=0)
    report_run_id: str | None = Field(default=None, min_length=1, max_length=64)
    workout_source: str | None = Field(default=None, min_length=1, max_length=32)
    workout_id: str | None = Field(default=None, min_length=1, max_length=128)
    recommendation_id: str | None = Field(default=None, min_length=1, max_length=64)
    health_event_id: str | None = Field(default=None, min_length=1, max_length=64)
    usefulness: Literal["useful", "partly_useful", "not_useful"] | None = None
    correction_field: str | None = Field(default=None, min_length=1, max_length=128)
    corrected_value: float | str | bool | None = None
    correction_unit: str | None = Field(default=None, min_length=1, max_length=24)
    correction_observed_on: date | None = None
    completed: bool | None = None
    outcome: Literal["beneficial", "neutral", "worse", "unknown"] | None = None
    false_positive: bool | None = None
    next_experiment: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("notes", "next_experiment", "correction_field", "correction_unit")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def explicit_feedback(self):
        if (self.workout_source is None) != (self.workout_id is None):
            raise ValueError("workout_source and workout_id must be supplied together")
        if (self.feedback_id is None) != (self.expected_revision == 0):
            raise ValueError("new feedback uses revision 0; existing feedback requires its positive revision")
        content = {
            "usefulness", "correction_field", "corrected_value", "correction_unit",
            "correction_observed_on", "completed", "outcome", "false_positive", "next_experiment",
        }
        allowed = {
            "report_usefulness": {"usefulness", "notes"},
            "data_correction": {
                "correction_field", "corrected_value", "correction_unit",
                "correction_observed_on", "notes",
            },
            "recommendation_completion": {"completed", "notes"},
            "recommendation_outcome": {"outcome", "next_experiment", "notes"},
            "event_assessment": {"false_positive", "notes"},
        }[self.kind]
        if any(getattr(self, name) is not None for name in content - allowed):
            raise ValueError("feedback kinds are independently audited")
        if self.kind == "report_usefulness" and (self.report_run_id is None or self.usefulness is None):
            raise ValueError("report usefulness requires a report run and an explicit rating")
        if self.kind == "data_correction" and any(value is None for value in (
            self.correction_field, self.corrected_value, self.correction_unit, self.correction_observed_on,
        )):
            raise ValueError("data correction requires field, value, unit, and observed date")
        if self.kind == "recommendation_completion" and (self.recommendation_id is None or self.completed is None):
            raise ValueError("recommendation completion requires an explicit completion value and recommendation")
        if self.kind == "recommendation_outcome" and (self.recommendation_id is None or self.outcome is None):
            raise ValueError("recommendation outcome requires an explicit result and recommendation")
        if self.kind == "event_assessment" and (self.health_event_id is None or self.false_positive is None):
            raise ValueError("false-positive assessment requires an explicit boolean and health event")
        if self.kind != "event_assessment" and self.health_event_id is not None:
            raise ValueError("health_event_id is reserved for event assessment")
        if self.kind == "recommendation_completion" and self.completed is False and self.workout_id is not None:
            raise ValueError("an incomplete recommendation cannot be linked to a completed workout")
        if isinstance(self.corrected_value, str) and (not self.corrected_value.strip() or len(self.corrected_value) > 500):
            raise ValueError("corrected_value must be nonempty and at most 500 characters")
        return self


class ProductTrackingStorePort(Protocol):
    """An adapter atomically saves a request, revisions, input event, and jobs."""

    def write_goal(
        self, user_id: str, body: GoalInput, *, goal_id: str | None,
        key: str, fingerprint: str, today: date, now: datetime,
    ) -> dict[str, Any]: ...

    def patch_goal(
        self, user_id: str, goal_id: str, body: GoalPatch, *,
        key: str, fingerprint: str, today: date, now: datetime,
    ) -> dict[str, Any]: ...

    def record_feedback(
        self, user_id: str, body: ProductFeedbackInput, *,
        key: str, fingerprint: str, today: date, now: datetime,
    ) -> dict[str, Any]: ...

    def goals(self, user_id: str) -> dict[str, Any]: ...

    def feedback(self, user_id: str, start: date, end: date, *, limit: int) -> list[dict[str, Any]]: ...

    def input_events(self, user_id: str, *, limit: int) -> list[dict[str, Any]]: ...

    def summary(self, user_id: str, start: date, end: date, *, as_of: datetime) -> dict[str, Any]: ...

    def metrics(self, user_id: str, start: date, end: date, *, as_of: datetime) -> dict[str, Any]: ...

    def analysis_context(
        self, user_id: str, day: date, *, as_of: datetime | None = None,
    ) -> dict[str, Any]: ...


def request_fingerprint(operation: str, resource_id: str | None, body: BaseModel) -> str:
    payload = {"operation": operation, "resource_id": resource_id, "body": body.model_dump(mode="json", exclude_unset=True)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProductTrackingService:
    """Read saved evidence and persist only confirmed user commands."""

    def __init__(
        self, store: ProductTrackingStorePort, *,
        today_factory: Callable[[], date],
        now_factory: Callable[[], datetime] | None = None,
        timezone_name: str = "UTC",
    ) -> None:
        self.store = store
        self._today = today_factory
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self.timezone_name = timezone_name

    @staticmethod
    def _key(key: str) -> str:
        if (
            not isinstance(key, str)
            or not 16 <= len(key) <= 128
            or key.strip() != key
            or not key.strip()
        ):
            raise ProductTrackingValidationError("Idempotency-Key must contain 16 through 128 characters")
        return key

    def put_goal(self, user_id: str, body: GoalInput, *, idempotency_key: str, goal_id: str | None = None) -> dict[str, Any]:
        return self.store.write_goal(
            user_id, body, goal_id=goal_id, key=self._key(idempotency_key),
            fingerprint=request_fingerprint("goal", goal_id, body), today=self._today(), now=self._now(),
        )

    def patch_goal(self, user_id: str, goal_id: str, body: GoalPatch, *, idempotency_key: str) -> dict[str, Any]:
        return self.store.patch_goal(
            user_id, goal_id, body, key=self._key(idempotency_key),
            fingerprint=request_fingerprint("goal_patch", goal_id, body), today=self._today(), now=self._now(),
        )

    def record_feedback(self, user_id: str, body: ProductFeedbackInput, *, idempotency_key: str) -> dict[str, Any]:
        return self.store.record_feedback(
            user_id, body, key=self._key(idempotency_key),
            fingerprint=request_fingerprint("feedback", body.feedback_id, body), today=self._today(), now=self._now(),
        )

    def goals(self, user_id: str) -> dict[str, Any]:
        return self.store.goals(user_id)

    def _period(self, start: date | None, end: date | None) -> tuple[date, date]:
        last = end or self._today()
        first = start or last - timedelta(days=27)
        if first > last or (last - first).days >= 366:
            raise ProductTrackingValidationError("period must be ordered and at most 366 days")
        if last > self._today():
            raise ProductTrackingValidationError("future dates cannot supply product metrics")
        return first, last

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ProductTrackingValidationError("limit must be between 1 and 100")
        return limit

    def feedback(self, user_id: str, start: date | None = None, end: date | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        first, last = self._period(start, end)
        return self.store.feedback(user_id, first, last, limit=self._limit(limit))

    def input_events(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.input_events(user_id, limit=self._limit(limit))

    def summary(self, user_id: str, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        first, last = self._period(start, end)
        return self.store.summary(user_id, first, last, as_of=self._now())

    def metrics(self, user_id: str, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        first, last = self._period(start, end)
        return self.store.metrics(user_id, first, last, as_of=self._now())

    def analysis_context(
        self, user_id: str, day: date | None = None, *, as_of: datetime | None = None,
    ) -> dict[str, Any]:
        return self.store.analysis_context(
            user_id, day or self._today(), as_of=as_of or self._now(),
        )


@dataclass(frozen=True)
class ProductSummary:
    user_id: str
    as_of: date
    goals: tuple[dict[str, Any], ...]
    next_experiment: dict[str, Any]
    training_preferences: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PRODUCT_TRACKING_VERSION, "user_id": self.user_id, "as_of": self.as_of,
            "goals": list(self.goals), "goal_progress": list(self.goals),
            "next_experiment": self.next_experiment, "training_preferences": self.training_preferences,
        }


def _get(value: object, name: str, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def calculate_progress(context: Mapping[str, Any], detached_series: Mapping[str, Sequence[object]]) -> ProductSummary:
    """Summarize explicit goals from already-qualified detached observations.

    SeriesPoint objects and dicts share this port.  Source, scope, device, unit,
    day, and observed_at must be present.  Conflicting comparable values and
    mixed streams are disclosed; missing days never become zero.  This function
    does not read storage, choose a health intervention, or accept an experiment.
    """
    day = _date(context.get("day") or context.get("as_of"))
    if day is None:
        raise ProductTrackingValidationError("product context requires an explicit day")
    as_of = context.get("observed_as_of")
    if isinstance(as_of, str):
        try:
            as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        except ValueError:
            as_of = None
    summaries: list[dict[str, Any]] = []
    for goal in context.get("goals") or []:
        if not isinstance(goal, Mapping) or goal.get("source") != "user_confirmed":
            continue
        key = str(goal.get("metric_key") or goal.get("goal_type") or "")
        unit = goal.get("target_unit")
        target = _number(goal.get("target_value"))
        window = int(goal.get("window_days") or 7)
        first = day - timedelta(days=window - 1)
        by_day: dict[date, dict[tuple, set[float]]] = defaultdict(lambda: defaultdict(set))
        rejected = 0
        for point in detached_series.get(key, ()):
            point_day = _date(_get(point, "day") or _get(point, "date"))
            value = _number(_get(point, "value"))
            source = _get(point, "source")
            scope = _get(point, "source_scope")
            observed_at = _get(point, "observed_at")
            if isinstance(observed_at, str):
                try:
                    observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if "T" in observed_at else date.fromisoformat(observed_at)
                except ValueError:
                    observed_at = None
            valid = (
                point_day is not None and first <= point_day <= day and value is not None
                and _get(point, "unit") == unit and bool(source) and bool(scope)
                and isinstance(observed_at, (date, datetime))
            )
            if valid and isinstance(observed_at, datetime) and isinstance(as_of, datetime):
                observed_utc = observed_at.replace(tzinfo=timezone.utc) if observed_at.tzinfo is None else observed_at.astimezone(timezone.utc)
                cutoff = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)
                valid = observed_utc <= cutoff
            if not valid:
                rejected += 1
                continue
            identity = (str(source), str(scope), _get(point, "device_id"), unit)
            by_day[point_day][identity].add(value)
        streams = {identity for identities in by_day.values() for identity in identities}
        conflicts = sum(any(len(values) != 1 for values in identities.values()) for identities in by_day.values())
        if len(streams) > 1:
            conflicts = len(by_day)
        qualified = {
            date_value: next(iter(next(iter(identities.values()))))
            for date_value, identities in by_day.items()
            if len(streams) == 1 and len(identities) == 1 and len(next(iter(identities.values()))) == 1
        }
        count = len(qualified)
        status = "CONFLICT" if conflicts else "UNKNOWN" if not count else "AVAILABLE" if count == window else "PARTIAL"
        aggregation = str(goal.get("aggregation") or "mean")
        value = None
        observed_on = max(qualified, default=None)
        if qualified and not conflicts:
            value = mean(qualified.values()) if aggregation == "mean" else sum(qualified.values()) if aggregation == "sum" else qualified[observed_on]
        comparison = str(goal.get("comparison") or "at_least")
        ratio = None
        met = None
        if value is not None and target is not None:
            if comparison == "at_least" and target > 0:
                ratio = value / target
            elif comparison == "at_most" and value > 0:
                ratio = target / value
            if status == "AVAILABLE":
                met = value >= target if comparison == "at_least" else value <= target if comparison == "at_most" else value == target
        source = next(iter(streams))[0] if len(streams) == 1 else None
        coverage = {"observed_days": count, "expected_days": window, "ratio": count / window, "conflict_days": conflicts, "rejected_points": rejected, "period_start": first, "period_end": day}
        summaries.append({
            **dict(goal), "metric": key, "key": key, "unit": unit, "accepted": True,
            "status": status, "value": value, "target_value": target,
            "progress_ratio": ratio, "target_met": met, "source": "user_confirmed",
            "observation_source": source, "observed_at": observed_on, "as_of": day,
            "coverage": coverage,
        })
    proposal = dict(context.get("next_experiment") or {})
    if not proposal:
        proposal = {"status": "UNKNOWN", "title": None, "source": None}
    proposal["accepted"] = False
    if proposal.get("title") is not None:
        proposal.setdefault("status", "PROPOSED")
    return ProductSummary(
        user_id=str(context.get("user_id") or ""), as_of=day, goals=tuple(summaries),
        next_experiment=proposal, training_preferences=dict(context.get("training_preferences") or {}),
    )


__all__ = [
    "GoalInput", "GoalPatch", "ProductFeedbackInput", "ProductTrackingStorePort",
    "ProductTrackingService", "ProductSummary", "ProductTrackingValidationError",
    "ProductTrackingConflict", "ProductRevisionConflict", "ProductIdempotencyConflict",
    "ProductResourceNotFound", "calculate_progress", "request_fingerprint",
]
