import json
from datetime import date, datetime, timedelta, timezone

from vitalis.intelligence.contracts import SubjectiveFeedbackInput
from vitalis.intelligence.service import IntelligenceAction, IntelligenceCommand, IntelligenceQuery
from vitalis.models import Workout, WorkoutType
from vitalis.storage import HealthRepository, session_scope


TARGET = date(2026, 8, 28)


def test_agent_context_is_layered_bounded_and_contains_no_full_profiles():
    user_id = "bounded-context-user"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
    IntelligenceCommand().analyze(user_id, TARGET)

    context = IntelligenceQuery().context(user_id, TARGET)
    payload = context.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    assert set(payload) == {
        "schema_version", "user_id", "date", "current", "recent", "trend", "personal"
    }
    assert "facts" not in payload
    assert "baselines" not in payload
    assert len(payload["trend"]) <= 12
    assert len(payload["recent"]["active_events"]) <= 5
    assert len(payload["personal"]["patterns"]) <= 6
    assert len(payload["personal"]["associations"]) <= 6
    assert len(encoded) < 20_000


def test_timeline_projects_typed_summaries_without_raw_samples():
    user_id = "timeline-user"
    workout_id = "timeline-workout"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.delete_for_user(user_id)
        repo.upsert_user(user_id)
        repo.save_workout(Workout(
            user_id=user_id,
            workout_id=workout_id,
            started_at=datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
            type=WorkoutType.RUNNING,
            sport_mode="outdoor_running",
            sport_mode_label="户外跑",
            training_family="aerobic",
            training_family_label="有氧训练",
            duration=40,
            load=60,
        ))
    first = IntelligenceCommand().analyze(user_id, TARGET)
    IntelligenceAction().complete_recommendation(
        user_id, first.recommendation.id, workout_id
    )
    IntelligenceAction().log_feedback(
        user_id,
        SubjectiveFeedbackInput(
            date=TARGET,
            workout_source="zepp",
            workout_id=workout_id,
            recommendation_id=first.recommendation.id,
            session_rpe=6,
        ),
    )
    IntelligenceCommand().analyze(user_id, TARGET)

    timeline = IntelligenceQuery().timeline(
        user_id, TARGET - timedelta(days=6), TARGET
    )
    payload = timeline.model_dump(mode="json")
    item_types = {item["type"] for item in payload["items"]}
    encoded = json.dumps(payload, ensure_ascii=False)

    assert {
        "analysis", "recommendation", "workout", "feedback", "training_response",
        "monthly_summary",
    } <= item_types
    assert len(payload["items"]) <= 100
    assert "metric_samples" not in encoded
    assert "workout_samples" not in encoded
    assert "response_days" not in encoded

    later = IntelligenceQuery().timeline(
        user_id, TARGET - timedelta(days=6), TARGET + timedelta(days=1)
    )
    assert any(item.type == "training_response" for item in later.items)
