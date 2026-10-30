"""Server-authorized conversation operations, independent of persistence adapters."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class CoachPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    revision: str = Field(default="denied", min_length=1, max_length=128)
    expires_at: AwareDatetime | None = None
    max_lookback_days: int = Field(default=28, strict=True, ge=1, le=28)
    max_requests_per_hour: int = Field(default=3, strict=True, ge=1, le=3)
    max_inflight: int = Field(default=1, strict=True, ge=1, le=1)
    allow_refresh: StrictBool = False
    allow_analyze: StrictBool = False

    @model_validator(mode="after")
    def explicit_grant(self):
        if self.enabled and (not self.expires_at or self.revision == "denied" or not self.revision.strip()):
            raise ValueError("Enabled policy requires expiry and explicit revision")
        return self

    def permits(self, now: datetime) -> bool:
        return bool(self.enabled and self.expires_at and self.expires_at > now)


class CoachPolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    users: dict[str, CoachPolicy]


class CoachOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["refresh_workouts", "analyze"]
    day: date

    @field_validator("day", mode="before")
    @classmethod
    def exact_day(cls, value):
        if type(value) is date:
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("day must be YYYY-MM-DD")
        return value


class CoachError(ValueError):
    def __init__(self, message: str, status_code: int = 403, *,
                 failure_code: str | None = None, retryable: bool = False,
                 next_action: str = "correct_request"):
        super().__init__(message)
        self.status_code = status_code
        self.failure_code = failure_code
        self.retryable = retryable
        self.next_action = next_action


def load_policy(user_id: str, *, path: str | Path | None = None) -> CoachPolicy:
    """Reload the administrator-owned file on every acceptance; fail closed."""
    configured = path if path is not None else os.environ.get("VITALIS_COACH_POLICY_FILE")
    if not configured:
        return CoachPolicy()
    try:
        policy_path = Path(configured)
        if not policy_path.is_absolute() or policy_path.is_symlink():
            return CoachPolicy()
        metadata = policy_path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            return CoachPolicy()
        if os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            return CoachPolicy()
        document = CoachPolicyFile.model_validate_json(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CoachPolicy()
    return document.users.get(user_id, CoachPolicy())


class CoachRequestStore(Protocol):
    def lookup(self, user_id: str, key: str, request_hash: str) -> dict | None: ...
    def reserve(self, user_id: str, key: str, request: CoachOperation, request_hash: str,
                policy: CoachPolicy, now: datetime) -> dict: ...
    def dispatch(self, reservation: dict) -> dict: ...


class CoachOperationService:
    def __init__(self, store: CoachRequestStore, *, policy_loader: Callable = load_policy,
                 today: Callable[[], date], clock: Callable[[], datetime] | None = None):
        self.store = store
        self.policy_loader = policy_loader
        self.today = today
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def policy(self, user_id: str) -> dict:
        policy = self.policy_loader(user_id)
        result = policy.model_dump(mode="json")
        result["enabled"] = policy.permits(self.clock())
        if not result["enabled"]:
            result["allow_refresh"] = result["allow_analyze"] = False
        result["budget_unit"] = "root_requests"
        result["today"] = self.today().isoformat()
        result["notification_policy"] = "suppress"
        result["max_operations_per_question"] = 2
        result["max_job_polls"] = 3
        result["refresh_days"] = [self.today().isoformat(), date.fromordinal(self.today().toordinal() - 1).isoformat()]
        result["revocation_scope"] = "new_acceptance_only"
        return result

    def create(self, user_id: str, key: str, request: CoachOperation) -> dict:
        if not isinstance(key, str) or not 16 <= len(key) <= 128 or key.strip() != key:
            raise CoachError("Invalid Idempotency-Key", 422)
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        existing = self.store.lookup(user_id, key, digest)
        if existing and (existing["job_id"] or existing["state"] not in {"queued", "running"}):
            return existing
        now = self.clock()
        policy = self.policy_loader(user_id)
        try:
            self._authorize(policy, now, request)
        except CoachError as exc:
            if existing:
                # No dispatch-owner fencing exists across the sync transaction boundary.
                # A missing downstream row cannot prove that an earlier call is inactive;
                # retain the slot and hourly charge rather than falsely cancel/refund it.
                raise CoachError(
                    "Dispatch unresolved and current authorization denies execution; "
                    "reservation retained for administrator inspection",
                    exc.status_code, failure_code="dispatch_pending",
                    next_action="check_resource") from exc
            raise
        reservation = self.store.reserve(user_id, key, request, digest, policy, now)
        # reserve commits before dispatch: sync service uses its own transaction.
        return self.store.dispatch(reservation)

    def _authorize(self, policy: CoachPolicy, now: datetime, request: CoachOperation) -> None:
        if not policy.permits(now):
            raise CoachError("Coach policy is disabled or expired")
        age = (self.today() - request.day).days
        if request.operation == "refresh_workouts":
            if not policy.allow_refresh:
                raise CoachError("Workout refresh is not authorized")
            if age not in (0, 1):
                raise CoachError("Workout refresh only supports today or yesterday", 422)
        else:
            if not policy.allow_analyze:
                raise CoachError("Analysis is not authorized")
            if not 0 <= age < policy.max_lookback_days:
                raise CoachError("Analysis date exceeds the authorized lookback", 422)
