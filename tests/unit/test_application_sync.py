from datetime import date

import pytest

from vitalis.application.connector import ConnectorAuth, ConnectorSyncResult, HealthConnector
from vitalis.application.sync import SyncCommand, SyncService
from vitalis.domain import NormalizedDaily, User


class _Connector(HealthConnector):
    source = "synthetic"

    def __init__(self, transaction_state):
        super().__init__(ConnectorAuth())
        self.transaction_state = transaction_state
        self.fetch_window = None

    def authenticate(self) -> ConnectorAuth:
        return self.auth

    def sync(
        self, user: User, start: date | None = None, end: date | None = None
    ) -> ConnectorSyncResult:
        raise NotImplementedError

    def fetch(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> list[NormalizedDaily]:
        self.fetch_window = (user, start, end)
        assert self.transaction_state["active"] is False
        return [
            NormalizedDaily(user_id=user.id, date=start),
            NormalizedDaily(user_id=user.id, date=end),
        ]


class _Repository:
    def __init__(self, transaction_state, *, fail=False):
        self.transaction_state = transaction_state
        self.fail = fail
        self.user = None
        self.dailies = []

    def upsert_user(self, user_id, name="", source="zepp"):
        assert self.transaction_state["active"] is True
        self.user = (user_id, name, source)

    def save_daily(self, daily):
        assert self.transaction_state["active"] is True
        if self.fail:
            raise RuntimeError("synthetic persistence failure")
        self.dailies.append(daily)


class _UnitOfWork:
    def __init__(self, repository):
        self.repository = repository
        self.state = repository.transaction_state
        self.committed = False
        self.exit_error = None

    def __enter__(self):
        self.state["active"] = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exit_error = exc_value
        self.state["active"] = False
        return False

    def commit(self):
        self.committed = True


def test_sync_fetches_before_entering_uow_and_commits_normalized_data():
    state = {"active": False}
    repository = _Repository(state)
    unit_of_work = _UnitOfWork(repository)
    connector = _Connector(state)
    service = SyncService(connector, lambda: unit_of_work)

    result = service.execute(SyncCommand(
        user_id="demo",
        name="Synthetic demo",
        start=date(2026, 9, 1),
        end=date(2026, 9, 2),
    ))

    assert result.as_dict() == {
        "user_id": "demo",
        "source": "synthetic",
        "start": "2026-09-01",
        "end": "2026-09-02",
        "days_synced": 2,
    }
    assert repository.user == ("demo", "Synthetic demo", "synthetic")
    assert [item.date for item in repository.dailies] == [
        date(2026, 9, 1), date(2026, 9, 2)
    ]
    assert unit_of_work.committed is True
    assert unit_of_work.exit_error is None
    assert state["active"] is False


def test_sync_rolls_back_when_persistence_fails():
    state = {"active": False}
    repository = _Repository(state, fail=True)
    unit_of_work = _UnitOfWork(repository)
    service = SyncService(_Connector(state), lambda: unit_of_work)

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        service.execute(SyncCommand(
            user_id="demo", start=date(2026, 9, 1), end=date(2026, 9, 1)
        ))

    assert unit_of_work.committed is False
    assert unit_of_work.exit_error is not None
    assert state["active"] is False
