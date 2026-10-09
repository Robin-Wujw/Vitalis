"""Data provenance stays bound to the dataset and the queued sync request."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.repositories import SourceIdentityConflict
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
from vitalis.bootstrap import get_intelligence_command, get_intelligence_query
from vitalis.config import settings


DAY = date(2026, 9, 20)
NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("mode", ["mock", "real", "replay"])
def test_saved_reports_keep_declared_dataset_mode_after_settings_change(mode, monkeypatch):
    user_id = f"mode-report-{mode}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.bind_source_mode(user_id, mode)
    get_intelligence_command(now_factory=lambda: NOW).analyze(user_id, DAY)
    monkeypatch.setattr(settings, "zepp_mock", mode != "mock")
    query = get_intelligence_query()
    for reader in (query.daily, query.morning_briefing, query.evening_briefing,
                   query.weekly, query.monthly):
        report = reader(user_id, DAY)
        assert report.report_context["source_mode"] == mode


def test_unmarked_input_is_unknown_instead_of_inferred_real():
    user_id = "mode-report-unmarked"
    result = get_intelligence_command(now_factory=lambda: NOW).analyze(user_id, DAY)
    assert result.daily.report_context["source_mode"] == "unknown"


def test_dataset_mode_cannot_be_rebound():
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("mode-bound-real")
        repo.bind_source_mode("mode-bound-real", "real")
    with pytest.raises(SourceIdentityConflict):
        with session_scope() as db:
            HealthRepository(db).bind_source_mode("mode-bound-real", "mock")
    with session_scope() as db:
        assert HealthRepository(db).source_mode("mode-bound-real") == "real"


def test_production_rejects_non_real_dataset_before_writes(monkeypatch):
    monkeypatch.setattr(settings, "env", "prod")
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user("mode-prod")
        for mode in ("mock", "replay"):
            with pytest.raises(SourceIdentityConflict):
                repo.bind_source_mode("mode-prod", mode)
        assert repo.source_mode("mode-prod") == "unknown"


def test_queued_source_mode_is_frozen_and_mismatch_never_fetches(monkeypatch):
    user_id = "mode-queue-switch"
    calls = []
    connector = SimpleNamespace(source_mode="mock", mock=True)
    coordinator = ZeppSyncCoordinator(connector=connector, wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt(
        user_id, window=FetchWindow.local_dates(DAY, DAY),
    )
    assert attempt.options["source_mode"] == "mock"
    connector.source_mode = "real"
    connector.mock = False
    monkeypatch.setattr(coordinator, "_connector_for", lambda *_: calls.append("fetch"))
    report = coordinator.run_attempt(attempt.id, max_chunks=1)
    assert not calls
    assert not report.success
    with session_scope() as db:
        repo = HealthRepository(db)
        assert repo.source_mode(user_id) == "mock"
        assert repo.sync_attempt(attempt.id).options["source_mode"] == "mock"
        assert repo.sleep_range(user_id, DAY, DAY) == []
