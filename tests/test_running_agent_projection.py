"""Synthetic saved-snapshot projection; reads cannot compute or enqueue work."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from vitalis.application.analysis import build_daily_profile
from vitalis.application.intelligence_service import IntelligenceQuery
from vitalis.intelligence.contracts import (
    DataQuality, RunningAnalysis, RunningPerformance, TrainingPreferences, TrendFeature,
)
from vitalis.intelligence.profile import RawDailyProfile


DAY = date(2026, 10, 7)
AS_OF = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
USER = "synthetic-running-reader"


@pytest.fixture()
def saved():
    daily = build_daily_profile(
        "synthetic-running-run", RawDailyProfile(
            user_id=USER, day=DAY, as_of=AS_OF,
            training_preferences=TrainingPreferences(user_id=USER),
            data_quality=DataQuality(status="INSUFFICIENT"),
        ), {}
    )
    daily.features.training.running = RunningAnalysis(
        status="INSUFFICIENT_DATA", status_label="Insufficient", sessions_7d=1,
        duration_minutes_7d=30, sessions_28d=1, duration_minutes_28d=30,
        limitations=["distance unavailable"],
        performance=RunningPerformance(composite_index=101, limitations=["not validated"]),
    )
    daily.trends = [TrendFeature(
        metric=metric, metric_label=metric, window_days=7, source="zepp",
        source_scope="workout", unit="km", status="INSUFFICIENT_DATA", status_label="Insufficient",
        current_distinct_days=1, previous_distinct_days=0, minimum_days=3,
        coverage_ratio=1 / 7, direction="INSUFFICIENT_DATA", direction_label="Unknown",
        calendar_semantics="activity_day", period_start=DAY, period_end=DAY,
        confidence="LOW", confidence_label="Low",
    ) for metric in ("running_distance", "sleep_duration", "running_pace")]
    daily.report_context.update({"source_mode": "mock", "as_of": AS_OF.isoformat()})
    return daily


class ReadOnlyRepository:
    """Only the existing daily state reads exist; any write/other fetch fails."""

    def __init__(self, daily, state):
        self.daily = daily
        self.state = state
        self.calls = []
        self.row = SimpleNamespace(
            payload=daily.model_dump(mode="json"), analysis_run_id=daily.analysis_run_id,
            profile_type="daily", period_start=DAY, period_end=DAY, generated_at=AS_OF,
            schema_version=daily.schema_version, intelligence_version=daily.intelligence_version,
            decision_policy_version=daily.decision_policy_version,
            evidence_version=daily.evidence_version,
        )

    def latest_analysis_snapshot(self, user_id, kind, day):
        self.calls.append((user_id, kind, day))
        return self.row if (user_id, kind, day) == (USER, "daily", DAY) and self.state == "current" else None

    def latest_good_analysis_snapshot(self, user_id, kind, day):
        self.calls.append((user_id, kind, day))
        return self.row if (user_id, kind, day) == (USER, "daily", DAY) else None

    def analysis_run(self, user_id, run_id):
        assert (user_id, run_id) == (USER, self.daily.analysis_run_id)
        return SimpleNamespace(input_revision_used=1, started_at=AS_OF, completed_at=AS_OF)

    def analysis_jobs_for_target(self, user_id, day):
        self.calls.append((user_id, "jobs", day))
        if user_id != USER or day != DAY or self.state not in {"queued", "running", "failed"}:
            return []
        return [SimpleNamespace(
            id="synthetic-pending", status=self.state, input_revision=2,
            stale_since=AS_OF, error="analysis_failed",
        )]


@pytest.fixture()
def query_factory(saved):
    def make(state="current"):
        repo = ReadOnlyRepository(saved, state)

        @contextmanager
        def uow():
            yield SimpleNamespace(repository=repo)  # Deliberately no commit method.

        return IntelligenceQuery(uow, today_factory=lambda: DAY), repo
    return make


@pytest.mark.parametrize("state", ["current", "stale", "queued", "running", "failed"])
def test_running_projects_saved_facts_and_freshness_only(query_factory, saved, state):
    query, repo = query_factory(state)
    before = saved.model_dump(mode="json")
    result = query.running(USER)
    assert result["user_id"] == USER
    assert result["date"] == DAY.isoformat()
    assert result["generated_at"] == saved.model_dump(mode="json")["generated_at"]
    assert result["analysis_run_id"] == saved.analysis_run_id
    assert result["report_context"]["report_state"]["state"] == state
    assert result["data_quality"] == before["data_quality"]
    assert result["running"] == saved.features.training.running.model_dump(mode="json", exclude={"performance"})
    assert result["running"]["distance_km_7d"] is None
    assert result["trends"] == [item for item in before["trends"] if item["metric"].startswith("running_")]
    assert "research" not in result
    assert saved.model_dump(mode="json") == before
    assert all(call[0] == USER and call[2] == DAY for call in repo.calls)


def test_running_projection_exposes_saved_effort_alongside_inferred_classification(query_factory, saved):
    from vitalis.intelligence.contracts import WorkoutFeature
    saved.features.training.recent_workouts = [WorkoutFeature(
        workout_id="synthetic-run", source="mock", date=DAY, type="running", type_label="跑步",
        sport_mode="outdoor_running", sport_mode_label="户外跑", training_family="running",
        training_family_label="跑步", recognition_confidence="high", recognition_confidence_label="高",
        recognition_source="vendor", recognition_source_label="来源", duration_minutes=30,
        session_rpe={"value": 4, "source": "zepp", "source_scope": "workout_summary", "source_field": "rpe"},
    )]
    query, _ = query_factory()
    result = query.running(USER)
    assert result["recorded_effort"][0]["session_rpe"]["value"] == 4
    assert result["classification_basis"] == "inferred_not_user_goal"
    assert "not" in result["classification_use"]


def test_running_research_is_opt_in_labeled_and_separate(query_factory, saved):
    query, _ = query_factory()
    result = query.running(USER, DAY, include_research=True)
    assert "performance" not in result["running"]
    assert result["research"]["performance"] == saved.features.training.running.performance.model_dump(mode="json")
    assert result["research"]["performance"]["shadow_only"] is True
    assert result["research"]["performance"]["validation_state"] == "unvalidated"
    assert "total_score" not in result["research"]["performance"]


@pytest.mark.parametrize("missing_running", [True, False])
def test_running_does_not_invent_missing_features_or_research(query_factory, saved, missing_running):
    if missing_running:
        saved.features.training.running = None
    else:
        saved.features.training.running.performance = None
    query, _ = query_factory()
    result = query.running(USER, include_research=True)
    assert result["research"]["performance"] is None
    if missing_running:
        assert result["running"] is None


def test_running_never_falls_back_to_other_user_or_day(query_factory):
    query, _ = query_factory()
    assert query.running("synthetic-other", DAY, include_research=True) is None
    assert query.running(USER, date(2026, 10, 6)) is None


def test_running_api_requires_read_scope_and_preserves_usual_errors(client, issue_token, monkeypatch, query_factory):
    from vitalis.entrypoints.api.routes import intelligence

    query, _ = query_factory("queued")
    monkeypatch.setattr(intelligence, "get_intelligence_query", lambda: query)
    read = {"Authorization": f"Bearer {issue_token(USER, {'read'})}"}
    response = client.get("/api/intelligence/running", params={"day": DAY.isoformat()}, headers=read)
    assert response.status_code == 200
    assert "research" not in response.json()
    assert response.json()["report_context"]["report_state"]["state"] == "queued"
    research = client.get("/api/intelligence/running?include_research=true", headers=read)
    assert research.json()["research"]["performance"]["shadow_only"] is True
    denied = {"Authorization": f"Bearer {issue_token(USER, {'analyze'})}"}
    assert client.get("/api/intelligence/running", headers=denied).status_code == 403
    assert client.get("/api/intelligence/running", authenticate=False).status_code == 401
    other = {"Authorization": f"Bearer {issue_token('synthetic-other', {'read'})}"}
    missing = client.get("/api/intelligence/running", headers=other)
    assert missing.status_code == 404
    assert missing.json()["state"] == "failed"
    assert {"failure_code", "retryable", "next_action"} <= missing.json().keys()
    assert client.get("/api/intelligence/running?day=invalid", headers=read).status_code == 422
