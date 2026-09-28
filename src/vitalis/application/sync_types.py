"""Small durable-sync value objects shared by storage and orchestration."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SyncChunkSpec:
    """One stable item in a synchronization manifest."""

    stable_key: str
    stream: str
    health_stream: str | None = None
    partition: str = ""
    ordinal: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    cursor: Any | None = None
    allow_unavailable: bool = False
    stages: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncLease:
    """Lease credentials returned by an explicit lease-acquisition helper."""

    entity_id: str | int
    token: str
    epoch: int
    expires_at: datetime


@dataclass(frozen=True)
class SyncAttemptAggregate:
    """Bounded aggregate of an attempt and its chunk outcomes."""

    attempt_id: str
    status: str
    total_chunks: int
    succeeded_chunks: int
    unavailable_chunks: int
    failed_chunks: int
    cancelled_chunks: int
    retrying_chunks: int
    running_chunks: int
    queued_chunks: int
    raw_records: int
    records_written: int

    @property
    def completed_count(self) -> int:
        return self.succeeded_chunks + self.unavailable_chunks

    @property
    def complete(self) -> bool:
        return not (self.queued_chunks or self.running_chunks or self.retrying_chunks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "status": self.status,
            "total_chunks": self.total_chunks,
            "succeeded_chunks": self.succeeded_chunks,
            "unavailable_chunks": self.unavailable_chunks,
            "failed_chunks": self.failed_chunks,
            "cancelled_chunks": self.cancelled_chunks,
            "retrying_chunks": self.retrying_chunks,
            "running_chunks": self.running_chunks,
            "queued_chunks": self.queued_chunks,
            "completed_count": self.completed_count,
            "raw_records": self.raw_records,
            "records_written": self.records_written,
            "complete": self.complete,
        }
