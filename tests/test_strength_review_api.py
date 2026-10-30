"""Saved review reads stay user-scoped, bounded, and free of source writes."""

from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence.health_reader import SqlHealthReader
from vitalis.application.health_query import HealthQuery
from vitalis.application.ports import HealthWorkoutRead
from vitalis.domain import Workout, WorkoutType
from vitalis.intelligence.contracts import StrengthExerciseInput
from vitalis.intelligence.strength import normalize_exercise


START = date(2026, 8, 1)
END = date(2026, 8, 28)
STAMP = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
SET = {"source": "lap_62", "order": 1, "exercise_name": "Synthetic press",
       "repetitions": 10, "weight_value": 20, "weight_unit": "kg", "weight_basis": None}


def test_query_requests_limit_plus_one_and_does_not_use_samples():
    class Reader:
        calls = []

        def strength_review_workouts(self, user_id, start, end, limit, *, timezone_name):
            self.calls.append((user_id, start, end, limit, timezone_name))
            return tuple(HealthWorkoutRead(
                source="zepp", workout_id=str(i),
                data={"started_at": f"2026-08-{20-i:02d}T08:00:00Z", "training_family": "strength"},
                detail={"strength_sets": [deepcopy(SET)]}, detail_synced=True,
            ) for i in range(3))

    reader = Reader()
    result = HealthQuery(reader, timezone_name="Asia/Shanghai").strength_review("synthetic", START, END, 2)
    assert reader.calls == [("synthetic", START, END, 3, "Asia/Shanghai")]
    assert result["user_id"] == "synthetic"
    assert result["truncated"] is True
    assert result["limit"] == 2
    assert result["coverage"]["workouts_read"] == 2
    assert len(result["sessions"]) == 2
    assert result["exercise_totals"][0]["nominal_volume"]["value"] == 400


@pytest.mark.parametrize("day", [date.min, date.max])
def test_query_reports_unrepresentable_calendar_boundaries_as_validation(day):
    query = HealthQuery(SqlHealthReader(session_scope), timezone_name="Asia/Shanghai")
    with pytest.raises(ValueError, match="boundary"):
        query.strength_review("synthetic", day, day, 1)


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
def test_query_rejects_invalid_limit_before_reading(limit):
    with pytest.raises(ValueError, match="limit"):
        HealthQuery(object()).strength_review("synthetic", START, END, limit)


def test_query_rejects_inverted_date_window_before_reading():
    with pytest.raises(ValueError, match="start"):
        HealthQuery(object()).strength_review("synthetic", END, START, 10)


def test_query_rejects_more_than_ninety_inclusive_days_before_reading():
    with pytest.raises(ValueError, match="90"):
        HealthQuery(object()).strength_review("synthetic", date(2026, 1, 1), date(2026, 4, 1), 10)


@pytest.fixture
def saved_review_workouts():
    owner, outsider = "strength-review-owner", "strength-review-outsider"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(owner)
        repo.upsert_user(outsider)
        for user, count in ((owner, 6), (outsider, 40)):
            repo.save_workout(Workout(
                user_id=user, workout_id="same-id", source="zepp", started_at=STAMP,
                duration=30, type=WorkoutType.STRENGTH, training_family="strength",
            ))
            repo.save_workout_detail(user, "same-id", {"schema_version": "5.1", "strength_sets": [deepcopy(SET)]},
                                     fetched_at=STAMP)
            confirmation = normalize_exercise(user, "same-id", 1, StrengthExerciseInput(
                exercise_name="Confirmed synthetic press", sets=2, repetitions=count,
                weight_kg=30, weight_basis="per_hand",
            ), created_at=STAMP)
            repo.replace_strength_exercises(user, "same-id", [confirmation])
        repo.save_workout(Workout(
            user_id=owner, workout_id="same-id", source="other", started_at=STAMP,
            duration=30, type=WorkoutType.STRENGTH, training_family="strength",
        ))
        repo.save_workout_detail(owner, "same-id", {"schema_version": "5.1", "strength_sets": [deepcopy(SET)]},
                                 source="other", fetched_at=STAMP)
    return owner, outsider


def test_saved_confirmations_are_scoped_and_review_executes_only_reads(saved_review_workouts, monkeypatch):
    owner, outsider = saved_review_workouts

    def forbidden(*_args, **_kwargs):
        pytest.fail("Review must not query samples, load individual workouts, or write source data")

    for method in ("workout_metric_samples", "workout_metric_samples_for_workouts", "workout", "save_workout", "save_workout_detail"):
        monkeypatch.setattr(HealthRepository, method, forbidden)
    with session_scope() as db:
        engine = db.get_bind()
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        query = HealthQuery(SqlHealthReader(), timezone_name="UTC")
        result = query.strength_review(owner, START, END, 10)
        other = query.strength_review(outsider, START, END, 10)
        empty = query.strength_review("strength-review-absent", START, END, 10)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    sessions = {s["source"]: s for s in result["sessions"]}
    assert sessions["zepp"]["record_source"] == "user_confirmed"
    assert sessions["zepp"]["exercises"][0]["nominal_volume"]["value"] == 360
    assert sessions["other"]["record_source"] == "saved_workout_detail"
    assert sessions["other"]["exercises"][0]["nominal_volume"]["value"] == 200
    assert len(other["sessions"]) == 1
    assert other["sessions"][0]["exercises"][0]["nominal_volume"]["value"] == 2400
    assert empty["sessions"] == []
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert not any("workout_metric_samples" in sql for sql in statements)
    assert len([sql for sql in statements if "FROM workouts" in sql]) == 3


def test_reader_uses_explicit_timezone_and_stable_limit_ties():
    owner = "strength-review-timezone"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(owner)
        for workout_id in ("z", "b", "a"):
            repo.save_workout(Workout(
                user_id=owner, workout_id=workout_id, started_at=datetime(2026, 8, 19, 23, 30, tzinfo=timezone.utc),
                duration=30, type=WorkoutType.STRENGTH, training_family="strength",
            ))
    query = HealthQuery(SqlHealthReader(), timezone_name="Asia/Shanghai")
    result = query.strength_review(owner, date(2026, 8, 20), date(2026, 8, 20), 2)
    assert result["truncated"] is True
    assert {row["workout_id"] for row in result["sessions"]} == {"a", "b"}
    assert {row["date"] for row in result["sessions"]} == {"2026-08-20"}
    assert HealthQuery(SqlHealthReader(), timezone_name="UTC").strength_review(
        owner, date(2026, 8, 20), date(2026, 8, 20), 2,
    )["sessions"] == []
