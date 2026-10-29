"""Bounded, user-scoped raw workout reads for conversational clients."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.domain import Workout, WorkoutMetricSample, WorkoutType


START = datetime(2026, 8, 1, 8, tzinfo=timezone.utc)
SETS = [
    {
        "source": "lap_62", "order": 1, "vendor_exercise_code": 999999,
        "exercise_id": None, "exercise_name": None, "repetitions": 8,
        "vendor_weight_value": 80, "vendor_weight_unit": 99,
        "weight_value": None, "weight_unit": None, "weight_kg": None,
        "weight_basis": None, "limitations": ["unknown_weight_unit", "unknown_exercise"],
    },
    {
        "source": "strength_sets", "order": 2, "exercise_id": "bench_press",
        "exercise_name": "Synthetic press", "repetitions": 10,
        "vendor_weight_value": 24000, "vendor_weight_unit": 0,
        "weight_value": 24, "weight_unit": "kg", "weight_kg": 24,
        "weight_basis": "total", "limitations": [],
    },
    {
        "source": "strength_sets", "order": 3, "exercise_name": "Synthetic row",
        "repetitions": 12, "weight_value": 20, "weight_unit": "lb",
        "weight_kg": 9.0718474, "weight_basis": "per_hand",
        "limitations": ["reference_mapping_only"],
    },
]


@pytest.fixture
def saved_workouts(issue_token):
    owner = "bounded-workout-owner"
    outsider = "bounded-workout-outsider"
    headers = {"Authorization": "Bearer " + issue_token(owner, scopes=["read"])}
    other_headers = {"Authorization": "Bearer " + issue_token(outsider, scopes=["read"])}
    with session_scope() as db:
        repo = HealthRepository(db)
        for index in range(8):
            workout_id = f"strength-{index}"
            repo.save_workout(Workout(
                user_id=owner, workout_id=workout_id,
                started_at=START + timedelta(days=index), duration=30,
                type=WorkoutType.STRENGTH, training_family="strength",
            ))
            summary = repo.workout(owner, workout_id)
            summary.data = {**summary.data, "strength_sets": []}
            repo.save_workout_detail(owner, workout_id, {
                "schema_version": "5.1", "strength_sets": [] if index == 7 else deepcopy(SETS),
                "laps": [{"private_lap": "not part of strength projection"}],
                "samples": [{"private_sample": "not part of strength projection"}],
            }, samples=[WorkoutMetricSample(
                workout_id=workout_id, timestamp=START + timedelta(days=index),
                metric="heart_rate", value=100, unit="bpm",
            )], fetched_at=START + timedelta(days=9))
        repo.save_workout(Workout(
            user_id=owner, workout_id="run", started_at=START - timedelta(hours=1),
            duration=20, type=WorkoutType.RUNNING, training_family="aerobic",
        ))
    return owner, headers, other_headers


def test_expansion_reads_saved_sets_not_summary_and_is_read_only(client, saved_workouts, monkeypatch):
    owner, headers, _ = saved_workouts

    def forbidden_detail_read(*_args, **_kwargs):
        pytest.fail("List expansion must not fetch individual details or metric samples")

    monkeypatch.setattr(HealthRepository, "workout", forbidden_detail_read)
    monkeypatch.setattr(HealthRepository, "workout_metric_samples", forbidden_detail_read)
    statements = []
    with session_scope() as db:
        engine = db.get_bind()

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get(
            "/api/workouts?from=2026-08-01&to=2026-08-31&limit=20&include_strength_details=true",
            headers=headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert response.json()["user_id"] == owner
    workouts = response.json()["workouts"]
    assert len(workouts) == 9
    assert workouts[0]["workout_id"] == "strength-7"
    assert [row["strength_details"]["state"] for row in workouts] == [
        "missing", *(["available"] * 7), "not_applicable",
    ]
    for row in workouts:
        projection = row["strength_details"]
        assert projection == {
            "state": projection["state"], "source": "zepp",
            "detail_source": "saved_workout_detail",
            "strength_sets": SETS if projection["state"] == "available" else [],
            "fetched_at": "2026-08-10T08:00:00Z" if row["workout_id"] != "run" else None,
            "schema_version": "5.1" if row["workout_id"] != "run" else None,
        }
        assert "detail" not in row and "samples" not in row and "laps" not in row
    assert len([sql for sql in statements if "FROM workouts" in sql]) == 1
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)


def test_summary_shape_is_unchanged_and_expansion_is_optional(client, saved_workouts):
    _, headers, _ = saved_workouts
    base = "/api/workouts?from=2026-08-01&to=2026-08-31&limit=2"
    summary = client.get(base, headers=headers).json()
    expanded = client.get(base + "&include_strength_details=true", headers=headers).json()
    for row in expanded["workouts"]:
        row.pop("strength_details")
    assert summary == expanded
    assert all("strength_details" not in row for row in summary["workouts"])


@pytest.mark.parametrize("limit", ["21", "500", "0", "-1", ""])
def test_expansion_rejects_invalid_limit_with_stable_422(client, saved_workouts, limit):
    _, headers, _ = saved_workouts
    response = client.get(
        "/api/workouts?include_strength_details=true" + ("&limit=" + limit if limit else ""),
        headers=headers,
    )
    assert response.status_code == 422
    error = response.json()
    assert error["state"] == "failed"
    assert error["failure_code"] == "validation_error"
    assert error["retryable"] is False
    assert error["next_action"] == "correct_request"


def test_expanded_and_detail_reads_do_not_leak_other_users(client, saved_workouts):
    owner, _, other_headers = saved_workouts
    path = "/api/workouts?from=2026-08-01&to=2026-08-31&limit=20&include_strength_details=true"
    assert client.get(path, headers=other_headers).json()["workouts"] == []
    assert client.get(
        "/api/workouts/strength-0?source=zepp&include_samples=true", headers=other_headers,
    ).status_code == 404
    assert client.get(path, headers={**other_headers, "X-User-Id": owner}).status_code == 403
    assert client.get(path, authenticate=False).status_code == 401


@pytest.mark.parametrize("suffix", ["", "&include_samples=false"])
def test_detail_default_does_not_query_or_return_samples(client, saved_workouts, monkeypatch, suffix):
    _, headers, _ = saved_workouts

    def forbidden_samples(*_args, **_kwargs):
        pytest.fail("Excluded samples must not be queried or materialized")

    monkeypatch.setattr(HealthRepository, "workout_metric_samples", forbidden_samples)
    response = client.get("/api/workouts/strength-0?source=zepp" + suffix, headers=headers)
    assert response.status_code == 200
    assert "samples" not in response.json()["detail"]
    assert response.json()["detail"]["strength_sets"] == SETS


def test_detail_samples_are_explicit_opt_in(client, saved_workouts):
    _, headers, _ = saved_workouts
    response = client.get(
        "/api/workouts/strength-0?source=zepp&include_samples=true", headers=headers,
    )
    assert response.status_code == 200
    detail = response.json()["detail"]
    assert detail["strength_sets"] == SETS
    assert detail["samples"][0]["metric"] == "heart_rate"
    assert detail["samples"][0]["value"] == 100
