"""Application use case for bounded source synchronization."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from vitalis.application.connector import HealthConnector
from vitalis.domain import NormalizedDaily, User
from vitalis.time import local_today


class SyncRepository(Protocol):
    """Persistence operations required by the application sync use case."""

    def upsert_user(
        self, user_id: str, name: str = "", source: str = "zepp"
    ) -> object: ...

    def bind_source_mode(self, user_id: str, mode: str) -> None: ...

    def save_daily(self, daily: NormalizedDaily) -> None: ...


class SyncUnitOfWork(Protocol):
    """Explicit transaction boundary used by source synchronization."""

    repository: SyncRepository

    def __enter__(self) -> "SyncUnitOfWork": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool: ...

    def commit(self) -> None: ...


@dataclass(frozen=True)
class SyncCommand:
    """Request to fetch and persist one bounded user window."""

    user_id: str
    name: str = ""
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True)
class SyncResult:
    """Stable result projection for synchronous callers such as the demo CLI."""

    user_id: str
    source: str
    start: date
    end: date
    days_synced: int

    def as_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "source": self.source,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days_synced": self.days_synced,
        }


class SyncService:
    """Fetch normalized source data, then persist it in one explicit transaction."""

    def __init__(
        self,
        connector: HealthConnector,
        unit_of_work_factory: Callable[[], SyncUnitOfWork],
    ) -> None:
        self.connector = connector
        self._unit_of_work_factory = unit_of_work_factory

    def execute(self, command: SyncCommand) -> SyncResult:
        end = command.end or local_today()
        start = command.start or (end - timedelta(days=14))
        if start > end:
            raise ValueError("sync start date must not be after end date")

        user = User(id=command.user_id, name=command.name)
        # Fetching may perform network I/O; keep it outside the write transaction.
        dailies = self.connector.fetch(user, start, end)

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.repository.upsert_user(
                command.user_id,
                name=command.name,
                source=self.connector.source,
            )
            unit_of_work.repository.bind_source_mode(
                command.user_id, self.connector.source_mode,
            )
            for daily in dailies:
                unit_of_work.repository.save_daily(daily)
            unit_of_work.commit()

        return SyncResult(
            user_id=command.user_id,
            source=self.connector.source,
            start=start,
            end=end,
            days_synced=len(dailies),
        )



__all__ = [
    "SyncCommand",
    "SyncRepository",
    "SyncResult",
    "SyncService",
    "SyncUnitOfWork",
]
