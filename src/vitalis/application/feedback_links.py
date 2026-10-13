"""One-time signed links that let a pushed report collect feedback.

PushPlus stays one-way: opening a report or ignoring the link writes nothing.
Feedback exists only when the user submits the linked form, which writes one
explicit feedback record for that report's date and queues its re-analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import secrets
from typing import Callable

from vitalis.application.ports import IntelligenceUowFactory
from vitalis.intelligence.contracts import SubjectiveFeedback, SubjectiveFeedbackInput

LINK_LIFETIME = timedelta(hours=36)
LINK_PERIODS = {"morning", "evening"}


class FeedbackLinkError(ValueError):
    """The link is unknown, expired or already used."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


@dataclass(frozen=True)
class FeedbackLinkView:
    target_date: date
    period: str
    workouts: tuple[dict, ...]
    plan_completed: bool | None


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def plan_completed(daily_payload: dict) -> bool | None:
    """Whether the day's primary planned session type was recorded as a workout.

    ``None`` when there was no plan or no training record to judge by; the
    feedback page never asks the user to fill this in.
    """
    plan = ((daily_payload.get("decision") or {}).get("action_plan") or {})
    primary = plan.get("primary_session") or {}
    kind = primary.get("session_type")
    if kind not in {"RUNNING", "STRENGTH"}:
        return None
    families = {"RUNNING": {"running"}, "STRENGTH": {"strength"}}[kind]
    day = daily_payload.get("date")
    workouts = [
        item for item in (((daily_payload.get("features") or {}).get("training") or {}).get("recent_workouts") or [])
        if isinstance(item, dict) and str(item.get("date")) == str(day)
    ]
    if not workouts:
        return False
    return any(str(item.get("type") or "").lower() in families for item in workouts)


class FeedbackLinkService:
    def __init__(
        self,
        uow_factory: IntelligenceUowFactory,
        *,
        save_feedback: Callable[[object, str, SubjectiveFeedbackInput], SubjectiveFeedback],
        now_factory: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._save_feedback = save_feedback
        self._now_factory = now_factory

    def issue(self, user_id: str, day: date, period: str) -> str:
        if period not in LINK_PERIODS:
            raise ValueError("feedback links exist only for morning and evening reports")
        token = secrets.token_urlsafe(32)
        now = self._now_factory()
        with self._uow_factory() as uow:
            uow.repository.create_feedback_link(
                user_id, token_digest(token), day, period,
                expires_at=now + LINK_LIFETIME, now=now,
            )
            uow.commit()
        return token

    def _valid(self, repo, token: str):
        link = repo.feedback_link(token_digest(token)) if token else None
        if link is None:
            raise FeedbackLinkError("unknown")
        if link.used_at is not None:
            raise FeedbackLinkError("used")
        now = self._now_factory().astimezone(timezone.utc).replace(tzinfo=None)
        if link.expires_at <= now:
            raise FeedbackLinkError("expired")
        return link

    def view(self, token: str) -> FeedbackLinkView:
        with self._uow_factory() as uow:
            repo = uow.repository
            link = self._valid(repo, token)
            snapshot = repo.latest_analysis_snapshot(link.user_id, "daily", link.target_date)
            payload = snapshot.payload if snapshot is not None else {}
            return FeedbackLinkView(
                target_date=link.target_date,
                period=link.period,
                workouts=tuple(
                    {
                        "workout_id": item.get("workout_id"), "source": item.get("source") or "zepp",
                        "label": item.get("type_label") or item.get("sport_mode_label") or "训练",
                    }
                    for item in (((payload.get("features") or {}).get("training") or {}).get("recent_workouts") or [])
                    if isinstance(item, dict) and item.get("workout_id")
                    and str(item.get("date")) == link.target_date.isoformat()
                ),
                plan_completed=plan_completed(payload) if payload else None,
            )

    def submit(self, token: str, feedback: SubjectiveFeedbackInput) -> SubjectiveFeedback:
        """Use the link once and write the user's feedback in the same transaction."""
        with self._uow_factory() as uow:
            repo = uow.repository
            link = self._valid(repo, token)
            if not repo.claim_feedback_link(link.id, now=self._now_factory()):
                raise FeedbackLinkError("used")
            result = self._save_feedback(
                repo, link.user_id, feedback.model_copy(update={"date": link.target_date}),
            )
            uow.commit()
            return result
