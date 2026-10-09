"""Contracts for durable analysis work and transactional completion."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Protocol

from vitalis.domain import NormalizedDaily


class RangeSummaryReader(Protocol):
    """Load normalized daily records for an inclusive user-scoped range."""

    def load_daily_records(
        self, user_id: str, start: date, end: date
    ) -> list[NormalizedDaily]: ...


@dataclass(frozen=True)
class HealthStreamStateRead:
    """Detached synchronization state used by the raw health status query."""

    stream: str
    fetch_status: str
    fetched_at: datetime | None
    parse_status: str
    parsed_at: datetime | None
    write_status: str
    written_at: datetime | None
    last_sample_at: datetime | None
    raw_records: int
    records_written: int
    error_kind: str | None


@dataclass(frozen=True)
class HealthSyncAttemptRead:
    """Detached synchronization attempt metadata for a user-scoped read."""

    attempt_id: str
    status: str
    trigger: str
    window_start: datetime
    window_end: datetime
    deadline_at: datetime | None
    retry_count: int
    next_retry_at: datetime | None


@dataclass(frozen=True)
class HealthSyncChunkRead:
    """Detached synchronization chunk state."""

    stream: str
    health_stream: str | None
    status: str
    records_written: int
    raw_records: int


@dataclass(frozen=True)
class HealthMetricRead:
    """Detached timestamped metric sample, including source identity."""

    timestamp: datetime
    value: float
    unit: str
    source: str
    source_scope: str
    device_id: str | None
    source_record_id: str | None
    sample_ordinal: int | None


@dataclass(frozen=True)
class HealthDailyMetricRead:
    """Detached sparse daily metric."""

    date: date
    metric: str
    value: float
    unit: str
    source: str
    source_scope: str
    device_id: str | None


@dataclass(frozen=True)
class HealthDenseFileRead:
    """Detached dense-file index projection without its private file id."""

    file_type: str
    date: date | None
    start_utc: datetime | None
    end_utc: datetime | None
    source_scope: str
    device_id: str | None
    parse_status: str
    sample_count: int


@dataclass(frozen=True)
class HealthWorkoutRead:
    """Detached workout summary and detail availability."""

    source: str
    workout_id: str
    data: dict[str, Any]
    detail: dict[str, Any] | None
    detail_synced: bool


@dataclass(frozen=True)
class HealthWorkoutSampleRead:
    """Detached typed workout metric sample."""

    timestamp: datetime
    metric: str
    value: float
    unit: str
    source_scope: str
    device_id: str | None


@dataclass(frozen=True)
class HealthTokenMetadataRead:
    """Stored token metadata; no vendor secret is exposed or decrypted."""

    vendor_user_id: str | None
    region_host: str
    expires_at: datetime | None


@dataclass(frozen=True)
class HealthBrowserLinkRead:
    """Detached browser-link status associated with a stored source token."""

    status: str
    message: str | None
    last_verified_at: datetime | None
    last_sync_at: datetime | None


class HealthReader(Protocol):
    """Persistence port for user-scoped raw health reads."""

    def sync_stream_states(self, user_id: str) -> tuple[HealthStreamStateRead, ...]: ...

    def latest_sync_attempt(
        self, user_id: str, source: str = "zepp"
    ) -> HealthSyncAttemptRead | None: ...

    def sync_chunks(
        self, attempt_id: str, user_id: str
    ) -> tuple[HealthSyncChunkRead, ...]: ...

    def metric_samples(
        self,
        user_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        *,
        limit: int | None = None,
    ) -> tuple[HealthMetricRead, ...]: ...

    def metric_sample_rows(
        self, user_id: str, metric: str, start: datetime, end: datetime
    ) -> AbstractContextManager[Iterator[HealthMetricRead]]: ...

    def daily_metrics(
        self, user_id: str, start: date, end: date, metric: str | None = None
    ) -> tuple[HealthDailyMetricRead, ...]: ...

    def dense_files(
        self, user_id: str, stream: str, start: date, end: date, limit: int
    ) -> tuple[HealthDenseFileRead, ...]: ...

    def workouts(
        self, user_id: str, start: date, end: date, limit: int
    ) -> tuple[HealthWorkoutRead, ...]: ...

    def workout_detail(
        self, user_id: str, workout_id: str, source: str
    ) -> tuple[HealthWorkoutRead, tuple[HealthWorkoutSampleRead, ...]] | None: ...

    def token_metadata(
        self, user_id: str, source: str = "zepp"
    ) -> HealthTokenMetadataRead | None: ...

    def latest_browser_link(self, user_id: str) -> HealthBrowserLinkRead | None: ...


@dataclass(frozen=True)
class CredentialInput:
    """Vendor credential material parsed by an external provider."""

    vendor_user_id: str
    app_token: str
    region_hint: str | None = None


@dataclass(frozen=True)
class SourceClaim:
    """Snapshot fencing a credential operation across external I/O."""

    user_id: str
    source: str
    account_id: str | None
    fence_epoch: int | None
    vendor_id: str | None
    status: str | None


@dataclass(frozen=True)
class PairingClaim:
    """One committed pairing-processing lease and its source-account snapshot."""

    pairing_id: str
    user_id: str
    sync_days: int
    processing_token: str
    processing_epoch: int
    source_claim: SourceClaim


@dataclass(frozen=True)
class BrowserLinkClaim:
    """One committed browser-link operation and its source-account snapshot."""

    token_digest: str
    user_id: str
    source_claim: SourceClaim


class CredentialProvider(Protocol):
    """Vendor-owned parsing, probing, verification, and exchange boundary."""

    source: str

    def authorize_url(self) -> tuple[str, str]: ...

    def authorize_url_for(self, state: str) -> str: ...

    def parse_cookie(self, raw: str) -> CredentialInput | None: ...

    def verify_credentials(
        self,
        user_id: str,
        credentials: CredentialInput,
        *,
        saved_host: str | None = None,
    ) -> Any: ...

    def exchange_code(self, user_id: str, code: str, state: str = "") -> Any: ...

    def verify_saved(self, token: Any) -> None: ...


class SourceSyncCreator(Protocol):
    """Adapter callback that creates a durable sync attempt without network I/O."""

    def __call__(
        self,
        user_id: str,
        *,
        days: int,
        trigger: str,
        trigger_ref: str | None = None,
        repository: object | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class SyncJobCommand:
    """User-scoped request for one durable source synchronization job."""

    user_id: str
    source: str
    idempotency_key: str
    days: int = 7
    from_date: date | None = None
    to_date: date | None = None
    decode_dense_files: bool = False
    detail_backfill: bool = False
    workout_only: bool = False
    detail_only: bool = False
    detail_limit: int | None = None
    detail_refresh_before: str | None = None


@dataclass(frozen=True)
class SyncJobCreated:
    """Stable projection returned after a sync request is durably enqueued."""

    job_id: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "status_url": f"/api/jobs/{self.job_id}",
        }


class SyncJobPort(Protocol):
    """Adapter boundary for durable sync creation, reads, and cancellation."""

    def create(self, command: SyncJobCommand) -> SyncJobCreated: ...

    def status(self, user_id: str, job_id: str) -> dict[str, Any] | None: ...

    def cancel(self, user_id: str, job_id: str) -> bool | None: ...


class SourceAccountRepository(Protocol):
    """Persistence operations needed by source-account management."""

    def revoke_source_account(
        self, user_id: str, source: str = "zepp", *, reason: str = "source_account_revoked"
    ) -> bool: ...

    def source_account_status(
        self, user_id: str, source: str = "zepp"
    ) -> dict | None: ...

    def claim_source_account(self, user_id: str, source: str = "zepp") -> SourceClaim: ...

    def source_claim_current(self, claim: SourceClaim) -> bool: ...

    def source_identity_owned_by_other(
        self, user_id: str, source: str, source_user_id: str
    ) -> bool: ...

    def save_token(
        self,
        token: Any,
        *,
        allow_create_user: bool = True,
        allow_reactivate: bool = True,
        expected_account_id: str | None = None,
        expected_fence_epoch: int | None = None,
    ) -> bool: ...

    def get_token(self, user_id: str, source: str = "zepp") -> Any: ...

    def save_oauth_state(
        self, state: str, user_id: str, source: str = "zepp", *,
        expires_at: datetime | None = None, sync_days: int = 180
    ) -> None: ...

    def oauth_state_exists(self, state: str, *, now: datetime | None = None) -> bool: ...

    def consume_oauth_state(self, state: str, *, now: datetime | None = None) -> tuple[str, int] | None: ...

    def claim_pairing_session(
        self,
        pairing_id: str,
        processing_lease_seconds: int = 120,
        *,
        rate_limit_attempts: int = 12,
        rate_window_seconds: int = 60,
        now: datetime | None = None,
    ) -> str | None: ...

    def pairing_claim(self, pairing_id: str, processing_token: str) -> PairingClaim | None: ...

    def pairing_session(self, pairing_id: str) -> Any: ...

    def pairing_retry_after(
        self, pairing_id: str, rate_limit_attempts: int, rate_window_seconds: int,
        *, now: datetime | None = None,
    ) -> int | None: ...

    def create_pairing_session(
        self, pairing_id: str, user_id: str, expires_at: datetime, sync_days: int = 30
    ) -> Any: ...

    def lock_pairing_claim(
        self,
        pairing_id: str,
        user_id: str,
        processing_token: str,
        *,
        processing_epoch: int | None = None,
    ) -> bool: ...

    def finish_pairing_session(
        self, pairing_id: str, processing_token: str,
        message: str = "已连接", sync_attempt_id: str | None = None,
    ) -> bool: ...

    def fail_pairing_session(self, pairing_id: str, processing_token: str, message: str) -> bool: ...

    def create_browser_link(
        self, token_digest: str, user_id: str, sync_attempt_id: str | None = None
    ) -> Any: ...

    def claim_browser_link(self, token_digest: str) -> BrowserLinkClaim | None: ...

    def lock_browser_link(self, claim: BrowserLinkClaim) -> bool: ...

    def browser_link(self, token_digest: str) -> Any: ...

    def latest_browser_link(self, user_id: str) -> Any: ...

    def mark_browser_link_verified(self, token_digest: str, message: str = "登录状态有效") -> None: ...

    def mark_browser_link_reauth(self, token_digest: str, message: str) -> None: ...

    def mark_browser_link_synced(
        self, token_digest: str, message: str, sync_attempt_id: str | None = None
    ) -> None: ...

    def mark_browser_link_sync_failed(self, token_digest: str, message: str) -> None: ...

    def attach_browser_link_attempt(
        self, token_digest: str, sync_attempt_id: str | None
    ) -> None: ...

    def sync_attempt(self, attempt_id: str, user_id: str | None = None) -> Any: ...


class UnitOfWorkPort(Protocol):
    """Application-facing transaction contract with explicit commit."""

    repository: SourceAccountRepository
    transaction: object

    def __enter__(self) -> "UnitOfWorkPort": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class IntelligenceUnitOfWork(Protocol):
    """Neutral transaction boundary used by intelligence application services."""

    repository: Any
    transaction: object

    def __enter__(self) -> "IntelligenceUnitOfWork": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


IntelligenceUowFactory = Callable[[], IntelligenceUnitOfWork]


class IdempotencyConflict(ValueError):
    """This user's key was already used for a different analysis request."""


class FeedbackIdempotencyConflict(ValueError):
    """This user's feedback key was already used for a different request body."""


@dataclass(frozen=True)
class AnalysisJobState:
    id: str
    user_id: str
    target_date: date
    status: str
    run_id: str | None
    error: str | None
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    delivery_period: str | None = None


@dataclass(frozen=True)
class JobClaim:
    id: str
    user_id: str
    target_date: date
    token: str
    epoch: int
    delivery_period: str | None = None


class AnalysisJobRepository(Protocol):
    def enqueue(
        self, user_id: str, day: date, key: str, request_hash: str,
        delivery_period: str | None = None,
    ) -> str: ...

    def get(self, user_id: str, job_id: str) -> AnalysisJobState | None: ...

    def claim(self, lease_seconds: int) -> JobClaim | None: ...

    def renew(self, claim: JobClaim, lease_seconds: int) -> bool: ...

    def attach_run(self, transaction: object, claim: JobClaim, run_id: str) -> bool: ...

    def succeed(self, transaction: object, claim: JobClaim, run_id: str) -> bool: ...

    def finish(self, claim: JobClaim, *, error: str) -> bool: ...

    def requeue_input_changed(self, claim: JobClaim, *, max_attempts: int) -> bool: ...


class AnalysisRunner(Protocol):
    """Commit analysis outputs and job success in one transaction."""

    def __call__(self, claim: JobClaim) -> str: ...
