"""Late training is compared to the report actually sent, never a clock guess."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.bootstrap import get_intelligence_command
from vitalis.domain.models import Workout
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.report_rendering import render_report


DAY = date(2026, 10, 17)
EVENING = datetime(2026, 10, 17, 15, 5, tzinfo=timezone.utc)
MORNING = datetime(2026, 10, 18, 1, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    from vitalis.adapters.persistence import database

    engine = database._create_engine("sqlite:///:memory:")
    database.init_db(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False,
    ))
    try:
        yield
    finally:
        engine.dispose()


def test_morning_compares_late_training_to_sent_evening_even_after_reanalysis():
    user = f"late-training-{uuid4().hex}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        repo.bind_source_mode(user, "mock")
    sent = get_intelligence_command(now_factory=lambda: EVENING).analyze(user, DAY)
    with session_scope() as db:
        repo = HealthRepository(db)
        delivery = repo.enqueue_notification_delivery(user, sent.run.id, "evening", DAY)
        delivery.status = "accepted"
        delivery.provider_id = "synthetic-provider"
        repo.analysis_run(user, sent.run.id).config_digest = "previous-release-rules"
        repo.save_workout(Workout(
            user_id=user, workout_id="late-strength", source="zepp",
            started_at=EVENING - timedelta(minutes=35),
            ended_at=EVENING + timedelta(minutes=15), duration=50,
            type="strength", training_family="strength", sport_mode_label="力量训练",
        ))
    revised = get_intelligence_command(now_factory=lambda: MORNING).analyze(user, DAY)
    result = get_intelligence_command(now_factory=lambda: MORNING).analyze(user, DAY + timedelta(days=1))
    updates = result.daily.report_context["late_training_updates"]
    assert updates["previous_analysis_run_id"] == sent.run.id != revised.run.id
    assert updates["date"] == DAY.isoformat()
    assert updates["items"][0]["change"] == "added"
    assert updates["items"][0]["workout"]["duration_minutes"] == 50
    for facts_only in (False, True):
        briefing = MorningBriefingEngine().build_payload(
            result.daily.model_dump(mode="json"), {"facts_only": facts_only},
        )
        for target in ("markdown", "html"):
            content = render_report(briefing, target).content
            assert "较昨晚晚报补入力量训练 50 分钟，已纳入本次分析" in content
    with session_scope() as db:
        delivery = HealthRepository(db).notification_delivery_for_key(user, DAY, "evening")
        assert delivery.status == "accepted" and delivery.analysis_run_id == sent.run.id


def _workout(key="one", **overrides):
    return {"workout_id": key, "source": "zepp", "date": DAY.isoformat(),
            "started_at": "2026-10-17T14:30:00Z", "duration_minutes": 50,
            "sport_mode_label": "力量训练", "vendor_load": None, **overrides}


def _comparison(previous, current, **overrides):
    from vitalis.intelligence.training_updates import compare_evening_training

    baseline = {"user_id": "owner", "date": DAY.isoformat(), "analysis_run_id": "sent",
                "as_of": EVENING.isoformat(), "workouts": previous, **overrides}
    return compare_evening_training(
        user_id="owner", day=DAY + timedelta(days=1), as_of=MORNING,
        workouts=current, previous=baseline,
    )


def test_training_comparison_keeps_source_identity_and_measured_changes_only():
    original = _workout()
    assert _comparison([original], [deepcopy(original)]) is None
    revised = _comparison([original], [_workout(duration_minutes=60)])
    assert revised["items"][0]["change"] == "updated"
    assert revised["items"][0]["previous"]["duration_minutes"] == 50
    assert _comparison([original], [_workout(source="other")])["items"][0]["change"] == "added"
    assert _comparison([original], [_workout(recognition_confidence="HIGH")]) is None
    assert _comparison([], [_workout(date="2026-10-18")]) is None
    assert _comparison([], [_workout(workout_id="")]) is None


def test_updated_training_notice_preserves_new_value_without_claiming_a_new_session():
    from tests.test_report_content import synthetic_daily_fixture

    payload = synthetic_daily_fixture("complete")
    payload["date"] = "2026-10-18"
    payload["features"]["training"]["recent_workouts"] = [_workout(duration_minutes=60)]
    payload["report_context"]["late_training_updates"] = _comparison(
        [_workout()], [_workout(duration_minutes=60)],
    )
    text = render_report(MorningBriefingEngine().build_payload(payload)).content
    assert "较昨晚晚报更新力量训练 60 分钟" in text
    assert "补入力量训练" not in text


@pytest.mark.parametrize("override", [
    {"user_id": "someone-else"}, {"date": "2026-10-16"},
    {"as_of": "2026-10-19T00:00:00Z"}, {"as_of": None},
])
def test_unqualified_previous_report_never_claims_late_training(override):
    assert _comparison([], [_workout()], **override) is None


@pytest.mark.parametrize("status", ["pending", "failed", "uncertain", "deferred"])
def test_unsent_or_uncertain_report_is_not_a_sent_training_baseline(status):
    user = f"unsent-training-{uuid4().hex}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
    result = get_intelligence_command(now_factory=lambda: EVENING).analyze(user, DAY)
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.enqueue_notification_delivery(user, result.run.id, "evening", DAY).status = status
        db.flush()
        assert repo.sent_evening_training(user, DAY, as_of=MORNING) is None


def test_evening_states_scope_and_keeps_next_day_advice_provisional():
    from tests.test_report_content import synthetic_daily_fixture

    payload = synthetic_daily_fixture("complete")
    payload["features"]["training"]["strength"]["recent_sessions"] = [{
        "date": payload["date"], "comparisons": [{
            "comparable": True, "delta_total_repetitions": 2,
            "reference_workout_date": "2026-09-01", "exercise_name": "深蹲",
        }],
    }]
    report = EveningBriefingEngine().build_payload(payload)
    assert report["suggestions"]
    for target in ("markdown", "html"):
        content = render_report(report, target).content
        assert "未同步或之后的训练尚未计入" in content
        assert "明日安排暂定" in content
    facts = EveningBriefingEngine().build_payload(payload, {"facts_only": True})
    assert facts["suggestions"] == []
