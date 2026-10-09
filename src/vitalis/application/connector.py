"""The application boundary for fetching normalized source data."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from vitalis.domain import NormalizedDaily, User


@dataclass
class ConnectorAuth:
    token: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ConnectorSyncResult:
    user_id: str
    source: str
    days_synced: int
    sleep_count: int
    workout_count: int
    activity_count: int


class HealthConnector(ABC):
    source: str
    source_mode: str

    def __init__(self, auth: ConnectorAuth | None = None) -> None:
        self.auth = auth or ConnectorAuth()

    @abstractmethod
    def authenticate(self) -> ConnectorAuth:
        """Establish or check a source credential."""

    @abstractmethod
    def sync(self, user: User, start: date | None = None, end: date | None = None) -> ConnectorSyncResult:
        """Synchronize one bounded source window."""

    @abstractmethod
    def fetch(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> list[NormalizedDaily]:
        """Fetch and normalize without inserting observations."""
