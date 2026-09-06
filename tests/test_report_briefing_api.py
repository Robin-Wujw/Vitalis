from datetime import date

import pytest

from vitalis.intelligence.service import IntelligenceCommand
from vitalis.storage import HealthRepository, session_scope
from vitalis.storage.models import AnalysisRun


@pytest.mark.parametrize("period", ["morning", "evening", "weekly", "monthly"])
def test_complete_report_queries_are_user_scoped_read_only_projections(client, monkeypatch, period):
    user = f"report-read-{period}"
    target = date(2026, 8, 28)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user)
        repo.upsert_user(user)
    analyzed = client.post(
        "/api/v1/intelligence/analyze", params={"day": target.isoformat()},
        headers={"X-User-Id": user},
    )
    assert analyzed.status_code == 201
    run_id = analyzed.json()["run"]["id"]
    with session_scope() as db:
        before = db.query(AnalysisRun).filter_by(user_id=user).count()
    monkeypatch.setattr(
        IntelligenceCommand, "analyze",
        lambda *args, **kwargs: pytest.fail("reading a report must not analyze or synchronize"),
    )
    response = client.get(
        f"/api/v1/intelligence/{period}-briefing", params={"day": target.isoformat()},
        headers={"X-User-Id": user},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["analysis_run_id"] == run_id
    assert report["date"] == target.isoformat()
    assert report["sections"]
    assert "feedback_prompt" not in report
    assert "训练后告诉我" not in response.text
    if period != "morning":
        assert report["period"] == period
        assert report["period_end"] == target.isoformat()
    with session_scope() as db:
        assert db.query(AnalysisRun).filter_by(user_id=user).count() == before
    other = client.get(
        f"/api/v1/intelligence/{period}-briefing", params={"day": target.isoformat()},
        headers={"X-User-Id": "report-read-other-user"},
    )
    assert other.status_code == 404


@pytest.mark.parametrize("period", ["evening", "weekly", "monthly"])
def test_new_report_queries_require_explicit_identity(client, period):
    assert client.get(f"/api/v1/intelligence/{period}-briefing").status_code == 422
