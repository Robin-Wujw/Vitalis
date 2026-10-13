"""Signed feedback links: one use, expiry, and no access to other users."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import AnalysisJob, SubjectiveFeedback
from vitalis.application.feedback_links import FeedbackLinkError, FeedbackLinkService
from vitalis.bootstrap import _intelligence_uow, get_intelligence_action
from vitalis.intelligence.contracts import SubjectiveFeedbackInput

DAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 22, tzinfo=timezone.utc)


def _service(now=NOW):
    return FeedbackLinkService(
        _intelligence_uow,
        save_feedback=get_intelligence_action()._save_feedback,
        now_factory=lambda: now,
    )


def _user(user_id):
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)


def test_link_writes_feedback_once_for_its_own_user_and_date():
    _user("feedback-link-owner")
    _user("feedback-link-other")
    token = _service().issue("feedback-link-owner", DAY, "evening")
    saved = _service().submit(token, SubjectiveFeedbackInput(feeling_score=4, behavior_tags=["travel"]))
    assert saved.user_id == "feedback-link-owner" and saved.date == DAY
    with pytest.raises(FeedbackLinkError) as used:
        _service().submit(token, SubjectiveFeedbackInput(feeling_score=2))
    assert used.value.state == "used"
    with session_scope() as db:
        rows = list(db.scalars(select(SubjectiveFeedback).where(
            SubjectiveFeedback.user_id.in_(["feedback-link-owner", "feedback-link-other"]),
        )))
        assert [(row.user_id, row.date) for row in rows] == [("feedback-link-owner", DAY)]
        assert db.scalars(select(AnalysisJob).where(
            AnalysisJob.user_id == "feedback-link-owner", AnalysisJob.target_date == DAY,
        )).first() is not None


def test_expired_and_unknown_links_are_refused():
    _user("feedback-link-expiry")
    token = _service().issue("feedback-link-expiry", DAY, "morning")
    late = _service(NOW + timedelta(hours=37))
    with pytest.raises(FeedbackLinkError) as expired:
        late.submit(token, SubjectiveFeedbackInput(feeling_score=3))
    assert expired.value.state == "expired"
    with pytest.raises(FeedbackLinkError) as unknown:
        _service().view("not-a-real-token")
    assert unknown.value.state == "unknown"


def test_feedback_page_round_trip(client, monkeypatch):
    from vitalis.entrypoints.api.routes import feedback as route

    _user("feedback-link-page")
    monkeypatch.setattr(route, "get_feedback_link_service", lambda: _service())
    token = _service().issue("feedback-link-page", DAY, "evening")
    page = client.get(f"/api/feedback/{token}", authenticate=False)
    assert page.status_code == 200 and "今天感觉" in page.text
    assert page.headers["cache-control"].startswith("no-store")
    posted = client.post(
        f"/api/feedback/{token}", authenticate=False,
        data={"feeling_score": "5", "behavior_tags": ["caffeine"]},
    )
    assert posted.status_code == 200 and "已记录" in posted.text
    again = client.post(f"/api/feedback/{token}", authenticate=False, data={"feeling_score": "1"})
    assert again.status_code == 410
