"""Explicit chart context and uploaded strength records behind a persistence port.

These commands preserve user annotations and external observations. They neither
infer physiology or muscle identity nor mutate canonical vendor workout facts.
"""

from __future__ import annotations

from collections.abc import Callable
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_CSV_BYTES = 1_048_576
MAX_CSV_ROWS = 2_000
MAX_IMPORT_SESSIONS = 100


class ChartInputValidationError(ValueError):
    """An explicit input is malformed or cannot be applied."""


class ChartInputLimitError(ChartInputValidationError):
    """The bounded upload or read exceeds its supported budget."""


class ChartInputConflict(ValueError):
    """A revision, idempotency key, or external identity conflicts."""


class ChartInputNotFound(ValueError):
    """A resource is missing or is owned by a different user."""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ChartInputValidationError("timestamps require an explicit UTC offset")
    return value.astimezone(timezone.utc)


class ExplicitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    confirmed: Literal[True]


class AnnotationInput(ExplicitInput):
    started_at: datetime
    ended_at: datetime
    text: str = Field(min_length=1, max_length=2_000)
    tag: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("started_at", "ended_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("text", "tag")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("annotation text and tag cannot be blank")
        return value

    @model_validator(mode="after")
    def ordered_interval(self):
        if not timedelta(0) <= self.ended_at - self.started_at <= timedelta(days=31):
            raise ValueError("annotation interval must be ordered and at most 31 days")
        return self


class AnnotationPatch(ExplicitInput):
    expected_revision: int = Field(ge=1, strict=True)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    text: str | None = Field(default=None, min_length=1, max_length=2_000)
    tag: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("started_at", "ended_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    @field_validator("text", "tag")
    @classmethod
    def nonempty_text(cls, value: str | None) -> str | None:
        return AnnotationInput.nonempty_text(value)

    @model_validator(mode="after")
    def explicit_changes(self):
        changed = self.model_fields_set - {"confirmed", "expected_revision"}
        if not changed:
            raise ValueError("annotation patch must contain a change")
        if any(getattr(self, name) is None for name in changed - {"tag"}):
            raise ValueError("only tag can be cleared with null")
        return self


class DeleteAnnotationInput(ExplicitInput):
    expected_revision: int = Field(ge=1, strict=True)


class ActivityReviewInput(ExplicitInput):
    workout_source: Literal["zepp"] = "zepp"
    workout_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(default=0, ge=0, strict=True)
    action: Literal["classify", "ignore", "restore"]
    classification: Literal["strength", "running", "cycling", "swimming", "walking", "hiit", "yoga", "other"] | None = None
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def classification_is_explicit(self):
        if (self.action == "classify") != (self.classification is not None):
            raise ValueError("only classify requires an explicit classification")
        return self


class ActivityRuleConditions(BaseModel):
    """Conjunctive exact/bounded conditions; never a free-form expression."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    title_equals: str | None = Field(default=None, min_length=1, max_length=200)
    sport_mode_equals: str | None = Field(default=None, min_length=1, max_length=100)
    min_duration_minutes: int | None = Field(default=None, ge=0, le=1440, strict=True)
    max_duration_minutes: int | None = Field(default=None, ge=0, le=1440, strict=True)
    weekdays: list[int] | None = Field(default=None, min_length=1, max_length=7)

    @model_validator(mode="after")
    def bounded_conditions(self):
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("at least one rule condition is required")
        if self.weekdays is not None and (len(set(self.weekdays)) != len(self.weekdays) or any(day not in range(1, 8) for day in self.weekdays)):
            raise ValueError("weekdays must be unique ISO weekdays")
        if self.min_duration_minutes is not None and self.max_duration_minutes is not None and self.min_duration_minutes > self.max_duration_minutes:
            raise ValueError("duration range must be ordered")
        return self


class ActivityRuleInput(ExplicitInput):
    rule_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_revision: int = Field(default=0, ge=0, strict=True)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    classification: Literal["strength", "running", "cycling", "swimming", "walking", "hiit", "yoga", "other"]
    conditions: ActivityRuleConditions

    @model_validator(mode="after")
    def rule_revision(self):
        if (self.rule_id is None) != (self.expected_revision == 0):
            raise ValueError("new rules use revision 0; existing rules require their current revision")
        if not self.name.strip():
            raise ValueError("rule name cannot be blank")
        return self


MuscleGroup = Literal["chest", "back", "shoulders", "biceps", "triceps", "quadriceps", "hamstrings", "glutes", "calves", "core"]


class HevySetMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_id: str = Field(min_length=1, max_length=64)
    exercise_id: str | None = Field(default=None, min_length=1, max_length=100)
    primary_muscles: list[MuscleGroup] = Field(default_factory=list, max_length=10)
    secondary_muscles: list[MuscleGroup] = Field(default_factory=list, max_length=10)
    side: Literal["left", "right", "bilateral", "unknown"] = "unknown"

    @model_validator(mode="after")
    def separate_muscle_roles(self):
        values = self.primary_muscles + self.secondary_muscles
        if len(set(values)) != len(values):
            raise ValueError("muscles cannot be repeated or occupy both roles")
        return self


class HevySetMappingInput(ExplicitInput):
    expected_revision: int = Field(ge=1, strict=True)
    mappings: list[HevySetMapping] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_sets(self):
        if len({item.set_id for item in self.mappings}) != len(self.mappings):
            raise ValueError("a set may be mapped only once per request")
        return self


def activity_suggestions(data: dict, *, weekday: int, rules: list[dict], external_ids: list[str]) -> dict:
    """Suggest exact user rules or uploaded session context without applying it."""
    suggestions = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        conditions = rule["conditions"]
        if conditions.get("title_equals") is not None and data.get("title") != conditions["title_equals"]:
            continue
        if conditions.get("sport_mode_equals") is not None and data.get("sport_mode") != conditions["sport_mode_equals"]:
            continue
        if conditions.get("weekdays") and weekday not in conditions["weekdays"]:
            continue
        duration = data.get("duration")
        if any(conditions.get(name) is not None for name in ("min_duration_minutes", "max_duration_minutes")):
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration):
                continue
            if conditions.get("min_duration_minutes") is not None and duration < conditions["min_duration_minutes"]:
                continue
            if conditions.get("max_duration_minutes") is not None and duration > conditions["max_duration_minutes"]:
                continue
        suggestions.append({"classification": rule["classification"], "source": "user_rule", "rule_id": rule["id"], "rule_revision": rule["revision"], "applied": False})
    if external_ids:
        suggestions.append({"classification": "strength", "source": "hevy_csv", "external_workout_ids": external_ids, "basis": "same_activity_day_or_overlap", "applied": False})
    classifications = {item["classification"] for item in suggestions}
    return {"suggestions": suggestions, "suggestion_state": "conflicting" if len(classifications) > 1 else "suggested" if classifications else "none", "suggested_classification": next(iter(classifications)) if len(classifications) == 1 else None}


class HevyImportInput(ExplicitInput):
    csv: str = Field(min_length=1, max_length=MAX_CSV_BYTES)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("timezone must be an IANA timezone") from exc
        return value


class HevyLinkInput(ExplicitInput):
    expected_revision: int = Field(ge=1, strict=True)
    workout_source: Literal["zepp"] = "zepp"
    workout_id: str = Field(min_length=1, max_length=128)


class ImportedStrengthSet(BaseModel):
    """One uploaded row; unknown units, identities, and effort stay unknown."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    order: int
    source_row: int
    exercise_name: str
    exercise_id: str | None = None
    set_index: int | None = None
    sets: Literal[1] = 1
    reps: int | None = None
    weight_value: float | None = None
    weight_unit: str | None = None
    set_type: str | None = None
    rpe: float | None = None
    source: Literal["hevy"] = "hevy"
    source_scope: Literal["hevy_csv"] = "hevy_csv"
    qualification: Literal["external_user_upload"] = "external_user_upload"
    muscle_groups: list[str] = Field(default_factory=list)
    mapping_status: Literal["unknown"] = "unknown"


@dataclass(frozen=True)
class ImportedStrengthSession:
    identity: str
    content_hash: str
    title: str
    started_at: datetime
    ended_at: datetime
    timezone_name: str
    source_record_id: str | None
    sets: tuple[ImportedStrengthSet, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _csv_time(raw: str, zone: ZoneInfo) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%d %b %Y, %H:%M", "%d %b %Y, %H:%M:%S", "%d %B %Y, %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ChartInputValidationError("CSV contains an unsupported timestamp")
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    candidates = {
        parsed.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        for fold in (0, 1)
        if parsed.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == parsed
    }
    if len(candidates) != 1:
        raise ChartInputValidationError("ambiguous or nonexistent local time requires an explicit offset")
    return candidates.pop()


def _number(raw: str, *, integer: bool = False, minimum: float = 0, maximum: float = 100_000) -> int | float | None:
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ChartInputValidationError("CSV contains an invalid number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum or integer and not value.is_integer():
        raise ChartInputValidationError("CSV number is outside its supported range")
    return int(value) if integer else value


def parse_hevy_csv(body: HevyImportInput, *, timezone_name: str) -> tuple[ImportedStrengthSession, ...]:
    """Parse a bounded uploaded Hevy export; no account, file, or network access.

    Native Hevy kg/lb columns and a weight+weight_unit pair are supported. An
    absent unit remains absent. English Hevy local dates use the explicit upload
    timezone or the configured service timezone, which is saved with the import.
    """
    try:
        encoded_size = len(body.csv.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ChartInputValidationError("CSV must be valid UTF-8 text") from exc
    if encoded_size > MAX_CSV_BYTES:
        raise ChartInputLimitError("CSV exceeds one MiB")
    if "\x00" in body.csv:
        raise ChartInputValidationError("CSV contains null bytes")
    selected_zone = body.timezone or timezone_name
    try:
        zone = ZoneInfo(selected_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ChartInputValidationError("unknown upload timezone") from exc
    reader = csv.DictReader(io.StringIO(body.csv.lstrip("\ufeff"), newline=""), strict=True)
    try:
        headers = reader.fieldnames or []
        required = {"title", "start_time", "end_time", "exercise_title"}
        if not required <= set(headers) or len(headers) != len(set(headers)) or len(headers) > 50:
            raise ChartInputValidationError("CSV requires the supported Hevy export headers")
        grouped: dict[str, dict[str, Any]] = {}
        for number, raw in enumerate(reader, start=1):
            if number > MAX_CSV_ROWS:
                raise ChartInputLimitError("CSV exceeds 2000 set rows")
            if None in raw or any(value is None for value in raw.values()):
                raise ChartInputValidationError("CSV row has the wrong number of cells")
            row = {key: value.strip() for key, value in raw.items()}
            title, exercise = row["title"], row["exercise_title"]
            if not title or len(title) > 200 or not exercise or len(exercise) > 200:
                raise ChartInputValidationError("CSV title and exercise name must be nonempty and bounded")
            started, ended = _csv_time(row["start_time"], zone), _csv_time(row["end_time"], zone)
            if not timedelta(0) <= ended - started <= timedelta(days=7):
                raise ChartInputValidationError("CSV workout interval is invalid")
            source_id = row.get("workout_id") or None
            if source_id is not None and len(source_id) > 128:
                raise ChartInputValidationError("CSV workout identity is too long")
            identity = _digest({"source_id": source_id} if source_id else {"title": title, "start": started.isoformat(), "end": ended.isoformat()})
            group = grouped.setdefault(identity, {"title": title, "start": started, "end": ended, "source_id": source_id, "sets": []})
            if (group["title"], group["start"], group["end"]) != (title, started, ended):
                raise ChartInputValidationError("CSV workout identity has inconsistent timestamps or titles")
            if len(grouped) > MAX_IMPORT_SESSIONS:
                raise ChartInputLimitError("CSV exceeds 100 sessions")
            weights = [(column, unit) for column, unit in (("weight_kg", "kg"), ("weight_lbs", "lb"), ("weight_lb", "lb"), ("weight", row.get("weight_unit") or None)) if row.get(column)]
            if len(weights) > 1:
                raise ChartInputValidationError("CSV row supplies multiple weight representations")
            weight, unit = (None, row.get("weight_unit") or None)
            if weights:
                column, unit = weights[0]
                weight = _number(row[column], maximum=20_000)
            if unit and len(unit) > 32:
                raise ChartInputValidationError("CSV weight unit is too long")
            if unit in {"lbs", "pounds"}:
                unit = "lb"
            set_type = row.get("set_type") or None
            if set_type and len(set_type) > 32:
                raise ChartInputValidationError("CSV set type is too long")
            group["sets"].append(ImportedStrengthSet(
                order=len(group["sets"]) + 1, source_row=number + 1,
                exercise_name=exercise,
                set_index=_number(row.get("set_index", ""), integer=True),
                reps=_number(row.get("reps", ""), integer=True, maximum=10_000),
                weight_value=weight, weight_unit=unit, set_type=set_type,
                rpe=_number(row.get("rpe", ""), minimum=1, maximum=10),
            ))
    except (csv.Error, OverflowError) as exc:
        raise ChartInputValidationError("CSV cannot be parsed") from exc
    if not grouped:
        raise ChartInputValidationError("CSV contains no workout sets")
    return tuple(ImportedStrengthSession(
        identity=identity, content_hash=_digest({
            "title": group["title"], "start": group["start"].isoformat(), "end": group["end"].isoformat(),
            "sets": [item.model_dump(exclude={"source_row"}) for item in group["sets"]],
        }), title=group["title"], started_at=group["start"], ended_at=group["end"],
        timezone_name=selected_zone, source_record_id=group["source_id"], sets=tuple(group["sets"]),
    ) for identity, group in grouped.items())


class ChartInputStorePort(Protocol):
    """Each mutation commits the input, revision, idempotency result, and jobs."""

    def create_annotation(self, user_id: str, body: AnnotationInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def patch_annotation(self, user_id: str, annotation_id: str, body: AnnotationPatch, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def delete_annotation(self, user_id: str, annotation_id: str, body: DeleteAnnotationInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def review_activity(self, user_id: str, body: ActivityReviewInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def import_hevy(self, user_id: str, sessions: tuple[ImportedStrengthSession, ...], *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def link_hevy(self, user_id: str, external_id: str, body: HevyLinkInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def save_activity_rule(self, user_id: str, body: ActivityRuleInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def activity_rules(self, user_id: str) -> dict: ...
    def map_hevy_sets(self, user_id: str, external_id: str, body: HevySetMappingInput, *, key: str, fingerprint: str, now: datetime) -> dict: ...
    def annotations(self, user_id: str, start: date, end: date, *, limit: int) -> dict: ...
    def activity_reviews(self, user_id: str, start: date, end: date, *, limit: int, status: str) -> dict: ...
    def external_workouts(self, user_id: str, start: date, end: date, *, limit: int) -> dict: ...
    def external_workout(self, user_id: str, external_id: str) -> dict: ...
    def hevy_candidates(self, user_id: str, external_id: str) -> dict: ...
    def snapshot(self, user_id: str, start: date, end: date, *, as_of: datetime, timezone_name: str | None = None) -> dict: ...


class ChartInputService:
    def __init__(self, store: ChartInputStorePort, *, today_factory: Callable[[], date], now_factory: Callable[[], datetime] | None = None, timezone_name: str = "UTC") -> None:
        self.store = store
        self._today = today_factory
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self.timezone_name = timezone_name

    def _write_args(self, operation: str, resource_id: str | None, body: BaseModel, key: str) -> dict:
        if not isinstance(key, str) or not 16 <= len(key) <= 128 or key.strip() != key or any(ord(char) < 32 for char in key):
            raise ChartInputValidationError("Idempotency-Key must contain 16 through 128 printable characters")
        return {"key": key, "fingerprint": _digest({"operation": operation, "resource_id": resource_id, "body": body.model_dump(mode="json", exclude_unset=True), "timezone": self.timezone_name}), "now": _aware(self._now())}

    def create_annotation(self, user_id: str, body: AnnotationInput, *, idempotency_key: str) -> dict:
        return self.store.create_annotation(user_id, body, **self._write_args("annotation_create", None, body, idempotency_key))

    def patch_annotation(self, user_id: str, annotation_id: str, body: AnnotationPatch, *, idempotency_key: str) -> dict:
        return self.store.patch_annotation(user_id, annotation_id, body, **self._write_args("annotation_patch", annotation_id, body, idempotency_key))

    def delete_annotation(self, user_id: str, annotation_id: str, body: DeleteAnnotationInput, *, idempotency_key: str) -> dict:
        return self.store.delete_annotation(user_id, annotation_id, body, **self._write_args("annotation_delete", annotation_id, body, idempotency_key))

    def review_activity(self, user_id: str, body: ActivityReviewInput, *, idempotency_key: str) -> dict:
        return self.store.review_activity(user_id, body, **self._write_args("activity_review", None, body, idempotency_key))

    def import_hevy(self, user_id: str, body: HevyImportInput, *, idempotency_key: str) -> dict:
        options = self._write_args("hevy_import", None, body, idempotency_key)
        sessions = parse_hevy_csv(body, timezone_name=self.timezone_name)
        if any(item.ended_at > options["now"] for item in sessions):
            raise ChartInputValidationError("uploaded completed workouts cannot be in the future")
        return self.store.import_hevy(user_id, sessions, **options)

    def link_hevy(self, user_id: str, external_id: str, body: HevyLinkInput, *, idempotency_key: str) -> dict:
        return self.store.link_hevy(user_id, external_id, body, **self._write_args("hevy_link", external_id, body, idempotency_key))

    def save_activity_rule(self, user_id: str, body: ActivityRuleInput, *, idempotency_key: str) -> dict:
        return self.store.save_activity_rule(user_id, body, **self._write_args("activity_rule", body.rule_id, body, idempotency_key))

    def activity_rules(self, user_id: str) -> dict:
        return self.store.activity_rules(user_id)

    def map_hevy_sets(self, user_id: str, external_id: str, body: HevySetMappingInput, *, idempotency_key: str) -> dict:
        return self.store.map_hevy_sets(user_id, external_id, body, **self._write_args("hevy_set_mapping", external_id, body, idempotency_key))

    def _period(self, start: date | None, end: date | None) -> tuple[date, date]:
        last = end or self._today()
        first = start or last - timedelta(days=27)
        if first > last or (last - first).days >= 366:
            raise ChartInputValidationError("period must be ordered and at most 366 calendar days")
        return first, last

    @staticmethod
    def _limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ChartInputValidationError("limit must be between 1 and 100")
        return limit

    def annotations(self, user_id: str, start: date | None = None, end: date | None = None, *, limit: int = 100) -> dict:
        return self.store.annotations(user_id, *self._period(start, end), limit=self._limit(limit))

    def activity_reviews(self, user_id: str, start: date | None = None, end: date | None = None, *, limit: int = 100, status: str = "pending") -> dict:
        if status not in {"pending", "classified", "ignored", "all"}:
            raise ChartInputValidationError("unsupported review state")
        return self.store.activity_reviews(user_id, *self._period(start, end), limit=self._limit(limit), status=status)

    def external_workouts(self, user_id: str, start: date | None = None, end: date | None = None, *, limit: int = 100) -> dict:
        return self.store.external_workouts(user_id, *self._period(start, end), limit=self._limit(limit))

    def external_workout(self, user_id: str, external_id: str) -> dict:
        return self.store.external_workout(user_id, external_id)

    def hevy_candidates(self, user_id: str, external_id: str) -> dict:
        return self.store.hevy_candidates(user_id, external_id)

    def snapshot(self, user_id: str, start: date, end: date, *, as_of: datetime | None = None, timezone_name: str | None = None) -> dict:
        return self.store.snapshot(user_id, *self._period(start, end), as_of=_aware(as_of or self._now()), timezone_name=timezone_name or self.timezone_name)
