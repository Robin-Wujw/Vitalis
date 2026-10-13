"""A stale stream is re-fetched once, then alerted once if it stays stale."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.models import DeliveryAlertEvent
from vitalis.application.data_freshness import freshness_actions

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
LAST = "2026-10-13T06:00:00Z"


def _row(stream="sleep", age_hours=30.0, freshness="stale"):
    return {
        "stream": stream, "freshness": freshness, "last_sample_at": LAST,
        "expected_interval_seconds": 86_400, "age_seconds": age_hours * 3600,
    }


def test_stale_stream_is_refetched_before_any_alert():
    actions = freshness_actions([_row()], {}, NOW)
    assert [(kind, key) for kind, key, _ in actions] == [("refetch", f"freshness_refetch:sleep@{LAST}")]


def test_alert_waits_for_grace_and_settle_then_fires_once():
    refetched = {f"freshness_refetch:sleep@{LAST}": NOW - timedelta(minutes=20)}
    assert freshness_actions([_row(age_hours=31)], refetched, NOW) == []
    refetched = {f"freshness_refetch:sleep@{LAST}": NOW - timedelta(hours=2)}
    assert freshness_actions([_row(age_hours=29)], refetched, NOW) == []
    actions = freshness_actions([_row(age_hours=31)], refetched, NOW)
    assert [kind for kind, _, _ in actions] == ["alert"]
    done = {**refetched, f"stream_stale:sleep@{LAST}": NOW}
    assert freshness_actions([_row(age_hours=40)], done, NOW) == []


def test_event_driven_and_fresh_streams_are_ignored():
    rows = [_row("hrv"), _row("heart_rate/minute_endpoint"), _row(freshness="fresh")]
    assert freshness_actions(rows, {}, NOW) == []


def test_freshness_job_refetches_once_and_stores_one_alert(monkeypatch):
    from vitalis.scheduler import jobs

    user_id = "freshness-job-owner"
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)
    queued = []
    rows = {"value": [_row(age_hours=26)]}

    class Query:
        def data_status(self, _user_id):
            return {"streams": rows["value"]}

    monkeypatch.setattr(jobs, "_get_authorized_users", lambda: {user_id})
    monkeypatch.setattr(jobs, "_sync_user", lambda user, days, label: queued.append(label) or "attempt-1")
    monkeypatch.setattr(jobs, "dispatcher_job", lambda: 0)
    monkeypatch.setattr("vitalis.bootstrap.get_health_query", lambda: Query())

    assert jobs.freshness_job(NOW) == 1
    assert jobs.freshness_job(NOW + timedelta(minutes=30)) == 0
    rows["value"] = [_row(age_hours=32)]
    assert jobs.freshness_job(NOW + timedelta(hours=6)) == 1
    assert jobs.freshness_job(NOW + timedelta(hours=7)) == 0
    assert queued == ["freshness"]
    with session_scope() as db:
        events = {
            row.reason: row.status
            for row in db.scalars(select(DeliveryAlertEvent).where(DeliveryAlertEvent.user_id == user_id))
        }
    assert events == {"stream_refetch_queued": "suppressed", "stream_stale": "pending"}
