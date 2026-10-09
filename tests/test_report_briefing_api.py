from datetime import date

import pytest

from vitalis.application.jobs import drain_analysis_jobs
from vitalis.application.intelligence_service import IntelligenceCommand
from vitalis.bootstrap import get_intelligence_command
from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import AnalysisRun


@pytest.mark.parametrize("period", ["morning", "evening", "weekly", "monthly"])
def test_complete_report_queries_are_user_scoped_read_only_projections(client, monkeypatch, period):
    user = f"report-read-{period}"
    target = date(2026, 8, 28)
    kind = period if period in {"morning", "evening"} else f"{period}-briefing"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user)
        repo.upsert_user(user)
    headers = {"X-User-Id": user, "Idempotency-Key": f"report-read-{period}-request"}
    queued = client.post(
        "/api/analysis-runs", json={"day": target.isoformat()}, headers=headers,
    )
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    assert client.get(f"/api/reports/{period}?day={target}", headers=headers).status_code == 404
    assert drain_analysis_jobs(max_jobs=1) == 1
    job = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    run_id = job.json()["analysis_run_id"]
    with session_scope() as db:
        before = db.query(AnalysisRun).filter_by(user_id=user).count()
    monkeypatch.setattr(
        IntelligenceCommand, "analyze",
        lambda *args, **kwargs: pytest.fail("reading a report must not analyze or synchronize"),
    )
    response = client.get(
        f"/api/reports/{kind}", params={"day": target.isoformat()},
        headers={"X-User-Id": user},
    )
    assert response.status_code == 200
    report = response.json()
    assert report["analysis_run_id"] == run_id
    assert report["sections"]
    assert "feedback_prompt" not in report
    assert "训练后告诉我" not in response.text
    if period in {"morning", "evening"}:
        assert report["date"] == target.isoformat()
    else:
        expected_end = date(2026, 8, 23) if period == "weekly" else date(2026, 7, 31)
        assert report["date"] == expected_end.isoformat()
        assert report["period_end"] == expected_end.isoformat()
    if period != "morning":
        assert report["period"] == period
    if period in {"weekly", "monthly"}:
        future_target = target + date.resolution
        assert client.get(
            f"/api/reports/{kind}", params={"day": future_target.isoformat()},
            headers={"X-User-Id": user},
        ).status_code == 404
    legacy_read = client.get(
        f"/api/intelligence/{period}-briefing", params={"day": target.isoformat()},
        headers={"X-User-Id": user},
    )
    assert legacy_read.status_code == 404
    with session_scope() as db:
        assert db.query(AnalysisRun).filter_by(user_id=user).count() == before
    other = client.get(
        f"/api/reports/{kind}", params={"day": target.isoformat()},
        headers={"X-User-Id": "report-read-other-user"},
    )
    assert other.status_code == 404


def test_daily_api_returns_full_profile_while_evening_returns_briefing(client):
    user = "report-read-daily-evening"
    target = date(2026, 8, 28)
    headers = {"X-User-Id": user, "Idempotency-Key": "report-read-daily-evening-request"}
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user)
        repo.upsert_user(user)
    queued = client.post(
        "/api/analysis-runs", json={"day": target.isoformat()}, headers=headers,
    )
    assert queued.status_code == 202
    assert drain_analysis_jobs(max_jobs=1) == 1
    daily = client.get("/api/reports/daily", params={"day": target.isoformat()}, headers=headers)
    evening = client.get("/api/reports/evening", params={"day": target.isoformat()}, headers=headers)
    assert daily.status_code == 200 and evening.status_code == 200
    daily_payload, evening_payload = daily.json(), evening.json()
    assert {"features", "facts", "decision"}.issubset(daily_payload)
    assert "period" not in daily_payload
    assert evening_payload["period"] == "evening"
    assert evening_payload["sections"]
    assert daily_payload != evening_payload


@pytest.mark.parametrize("period", ["evening", "weekly", "monthly"])
def test_new_report_queries_require_bearer_identity(client, period):
    kind = f"{period}-briefing" if period in {"weekly", "monthly"} else period
    assert client.get(f"/api/reports/{kind}").status_code == 401
    assert client.get(f"/api/intelligence/{period}-briefing").status_code == 404
