"""Synthetic coverage for the bounded strength-only lap layout."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from vitalis.connectors.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.connectors.zepp.parser import ZEPP_STRENGTH_LAP_LABELS, ZeppParser
from vitalis.connectors.zepp.sync_manager import SyncManager
from vitalis.models import User, Workout, WorkoutDetail, WORKOUT_DETAIL_SCHEMA_VERSION
from vitalis.storage import HealthRepository, session_scope


START = datetime(2026, 8, 12, 5, tzinfo=timezone.utc)


def lap_row(reps="9", weight="17.5", code="801", columns=62):
    fields = [""] * 62
    fields[0], fields[1], fields[2] = "1", "30", "-1"
    fields[21], fields[22], fields[28] = str(weight), str(reps), str(code)
    if columns > 62:
        fields.extend([""] * (columns - 62))
    return ",".join(fields[:columns])


def detail_for(lap, *, family="strength", explicit=None):
    return ZeppParser.parse_workout_detail({"data": {
        "trackid": int(START.timestamp()), "lap": lap,
        "strengthSets": [] if explicit is None else explicit,
    }}, training_family=family)


def test_strength_lap_keeps_order_codes_and_unconfirmed_weight_units():
    detail = detail_for(";".join([
        lap_row(code="801"), lap_row(reps="11", weight="22", code="802"),
        lap_row(code="801"),
    ]))
    assert detail.schema_version == WORKOUT_DETAIL_SCHEMA_VERSION
    assert detail.laps == []
    sets = detail.strength_sets
    assert [item.order for item in sets] == [1, 2, 3]
    assert [item.vendor_exercise_code for item in sets] == [801, 802, 801]
    assert [item.repetitions for item in sets] == [9, 11, 9]
    assert [item.weight_value for item in sets] == [17.5, 22, 17.5]
    assert all(item.source == "lap_62" for item in sets)
    assert all(item.exercise_id is None and item.exercise_name is None for item in sets)
    assert all(item.weight_kg is None and item.weight_unit is None for item in sets)
    assert all("weight_unit_unverified" in item.limitations for item in sets)
    assert WorkoutDetail.model_validate_json(detail.model_dump_json()).strength_sets == sets


@pytest.mark.parametrize("code, expected_name", list(ZEPP_STRENGTH_LAP_LABELS.items()))
def test_strength_lap_maps_only_verified_display_codes(code, expected_name):
    observed, = detail_for(lap_row(code=str(code))).strength_sets
    assert observed.vendor_exercise_code == code
    assert observed.exercise_name == expected_name
    assert observed.exercise_id is None
    assert observed.source == "lap_62"
    assert "exercise_name_reference_mapping" in observed.limitations
    assert "exercise_name_unverified" not in observed.limitations


@pytest.mark.parametrize("family", [None, "aerobic", "unknown", "Strength"])
def test_strength_lap_requires_explicit_strength_family(family):
    assert detail_for(lap_row(), family=family).strength_sets == []


@pytest.mark.parametrize("columns", [3, 61, 63])
def test_strength_lap_does_not_guess_other_layouts(columns):
    assert detail_for(lap_row(columns=columns)).strength_sets == []


def test_strength_lap_retains_original_positions_when_bad_rows_are_skipped():
    detail = detail_for(";".join([
        lap_row(), "bad,row", "", lap_row(reps="13", code="803"),
    ]))
    assert [item.order for item in detail.strength_sets] == [1, 4]


@pytest.mark.parametrize("value", ["", "bad", "NaN", "Infinity", "-Infinity", "-1", "0", "9.25", "1001", "True"])
def test_strength_lap_invalid_repetitions_do_not_erase_other_fields(value):
    observed, = detail_for(lap_row(reps=value)).strength_sets
    assert observed.repetitions is None
    assert observed.weight_value == 17.5
    assert observed.vendor_exercise_code == 801


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-1", "0", "9.25", "True", "2147483648"])
def test_strength_lap_invalid_codes_remain_unidentified(value):
    observed, = detail_for(lap_row(code=value)).strength_sets
    assert observed.vendor_exercise_code is None
    assert observed.exercise_name is None
    assert observed.repetitions == 9


@pytest.mark.parametrize("value", ["", "bad", "NaN", "Infinity", "-Infinity", "-1", "2001", "True"])
def test_strength_lap_missing_weight_never_becomes_zero_or_body_weight(value):
    observed, = detail_for(lap_row(weight=value)).strength_sets
    assert observed.weight_value is None
    assert observed.weight_kg is None
    assert observed.weight_unit is None
    assert "weight_unavailable" in observed.limitations


def test_strength_lap_preserves_explicit_zero_without_inventing_unit():
    observed, = detail_for(lap_row(weight="0")).strength_sets
    assert observed.weight_value == 0
    assert observed.weight_kg is None
    assert observed.weight_unit is None


def test_strength_lap_skips_rows_without_any_valid_observation():
    assert detail_for(lap_row(reps="NaN", weight="-1", code="-1")).strength_sets == []


@pytest.mark.parametrize("explicit", [[], "[]", [{}], [{"reps": "NaN", "weightKg": -1}], [{"reps": True, "weightKg": True}]])
def test_empty_or_invalid_strength_sets_allow_lap_fallback(explicit):
    observed, = detail_for(lap_row(), explicit=explicit).strength_sets
    assert observed.source == "lap_62"


@pytest.mark.parametrize("explicit", [
    [{"exerciseName": "synthetic exercise", "reps": 7}],
    [{}, {"reps": 7}],
    '[{"reps": 7, "weightKg": 12}]',
])
def test_valid_strength_sets_take_precedence_without_merging_lap(explicit):
    observed, = detail_for(lap_row() + ";" + lap_row(), explicit=explicit).strength_sets
    assert observed.source == "strength_sets"
    assert observed.repetitions == 7
    assert observed.vendor_exercise_code is None


def test_explicit_strength_sets_are_not_rewritten_by_lap_display_mapping():
    detail = detail_for(lap_row(code="64"), explicit=[{"exerciseName": "用户确认动作", "reps": 7}])
    observed, = detail.strength_sets
    assert observed.source == "strength_sets"
    assert observed.exercise_name == "用户确认动作"
    assert observed.vendor_exercise_code is None
    assert "exercise_name_reference_mapping" not in observed.limitations


def test_explicit_strength_set_units_remain_distinct():
    detail = detail_for(lap_row(), explicit=[
        {"reps": 7, "weightKg": 12},
        {"reps": 7, "weight": 12, "weightUnit": "lb"},
        {"reps": 7, "weight": 12},
    ])
    assert [item.weight_value for item in detail.strength_sets] == [12, 12, 12]
    assert [item.weight_kg for item in detail.strength_sets] == [12, None, None]
    assert [item.weight_unit for item in detail.strength_sets] == ["kg", "lb", None]


@pytest.mark.parametrize("family, expected_count", [("strength", 1), ("aerobic", 0), (None, 0)])
def test_sync_uses_stored_family_and_persists_ordered_observations(family, expected_count, client):
    user = User(id=f"lap-sync-{family}")
    workout_id = str(int(START.timestamp()))
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user.id)
        repo.save_workout(Workout(
            user_id=user.id, workout_id=workout_id, started_at=START,
            duration=5, training_family=family or "unknown", vendor_source="synthetic-source",
        ))
        if family is None:
            row = repo.workout(user.id, workout_id)
            row.data = {key: value for key, value in row.data.items() if key != "training_family"}
        record = FetchedRecord(raw=RawRecord(
            stream="workout_detail", source_key=f"workout_detail:{workout_id}:synthetic-source",
            start_utc=START, end_utc=START + timedelta(minutes=5),
            payload={"data": {"trackid": int(START.timestamp()), "lap": lap_row()}},
        ))
        assert SyncManager(SimpleNamespace())._write_stream(record, repo, user) == 1
    with session_scope() as db:
        saved = HealthRepository(db).workout(user.id, workout_id)
        detail = WorkoutDetail.model_validate(saved.detail)
        assert saved.detail_synced
        assert len(detail.strength_sets) == expected_count
        if expected_count:
            assert detail.strength_sets[0].vendor_exercise_code == 801
            assert detail.strength_sets[0].order == 1
            assert detail.strength_sets[0].weight_kg is None
    if expected_count:
        headers = {"X-User-Id": user.id}
        analysis = client.post("/api/v1/intelligence/analyze", params={"day": START.date().isoformat()}, headers=headers)
        assert analysis.status_code == 201
        daily = analysis.json()["daily"]
        session = daily["features"]["training"]["strength"]["recent_sessions"][0]
        assert session["observed_sets"][0]["vendor_exercise_code"] == 801
        assert session["explicit_exercises"] == []
        assert session["total_sets"] is None
        assert session["focus"] == "UNKNOWN"
        briefing = client.get("/api/v1/intelligence/evening-briefing", params={"day": START.date().isoformat()}, headers=headers)
        assert briefing.status_code == 200
        section = next(item for item in briefing.json()["sections"] if item["key"] == "training")
        assert any("第 1 组：动作代码 801" in item for item in section["facts"])
        assert any("17.5（单位未确认）" in item for item in section["facts"])


@pytest.mark.parametrize("previous_schema", ["4.0", "5.0"])
def test_fresh_previous_schema_still_enters_detail_backlog(previous_schema):
    user = f"lap-schema-backlog-{previous_schema}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user)
        for name, schema in (("old", previous_schema), ("current", WORKOUT_DETAIL_SCHEMA_VERSION)):
            repo.save_workout(Workout(
                user_id=user, workout_id=name, started_at=START, duration=5,
                training_family="strength", vendor_source="synthetic-source",
            ))
            repo.save_workout_detail(user, name, {"schema_version": schema}, fetched_at=START)
        pending = repo.pending_workout_details(user, START, START + timedelta(days=1), limit=1)
        assert [item.workout_id for item in pending] == ["old"]
