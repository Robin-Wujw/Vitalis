from datetime import date
import json
import sqlite3

import pytest

from vitalis.workout_audit import audit_workouts


def _workout(db, user, started, family, detail=None, *, source="zepp", vendor_type_id=None):
    db.execute(
        "INSERT INTO workouts (user_id, source, started_at, data, detail, detail_synced) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user,
            source,
            started,
            json.dumps({"training_family": family, "vendor_type_id": vendor_type_id}),
            json.dumps(detail) if detail is not None else None,
            detail is not None,
        ),
    )


def test_workout_audit_is_user_scoped_read_only_and_does_not_claim_coverage(tmp_path):
    database = tmp_path / "workouts.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE workouts (user_id TEXT, source TEXT, started_at TEXT, data TEXT, detail TEXT, detail_synced INTEGER)"
        )
        _workout(db, "owner", "2026-09-22 16:30:00", "strength", {
            "schema_version": "5.1",
            "strength_sets": [
                {"source": "lap_62", "vendor_exercise_code": 64, "exercise_name": "引体向上"},
                {"source": "lap_62", "vendor_exercise_code": 1988},
                {"source": "lap_62", "vendor_exercise_code": 1770},
                {"source": "strength_sets", "exercise_id": "private"},
            ],
        }, vendor_type_id=52)
        _workout(db, "owner", "2026-09-23 10:00:00", "strength")
        _workout(db, "owner", "2026-09-24 03:00:00", "strength", {
            "schema_version": "4.0", "strength_sets": [],
        })
        _workout(db, "owner", "2026-09-24 04:00:00", "aerobic")
        _workout(db, "owner", "2026-09-25 04:00:00", "aerobic", {
            "schema_version": "5.1",
            "strength_sets": [{"source": "lap_62", "vendor_exercise_code": 1111}],
        }, vendor_type_id=9)
        _workout(db, "other", "2026-09-22 16:30:00", "strength", {
            "schema_version": "5.1",
            "strength_sets": [{"source": "lap_62", "vendor_exercise_code": 1234}],
        })
    before = database.read_bytes()

    result = audit_workouts(database, "owner")

    assert database.read_bytes() == before
    assert result["source_coverage"] == "NOT_ASSESSED"
    assert result["cloud_workout_types"] == [
        {"code": 9, "workouts": 1}, {"code": 52, "workouts": 1},
    ]
    assert result["totals"]["untyped_zepp_workouts"] == 3
    assert result["totals"]["workouts"] == 5
    assert result["totals"]["strength_workouts"] == 3
    assert result["totals"]["missing_strength_details"] == 1
    assert result["totals"]["unsynced_strength_details"] == 1
    assert result["totals"]["outdated_strength_details"] == 1
    assert result["totals"]["misclassified_strength_details"] == 1
    assert result["totals"]["stale_mapped_lap_sets"] == 1
    assert result["totals"]["observed_lap_sets"] == 4
    assert result["strength_codes"] == [
        {"code": 64, "sets": 1, "named_sets": 1, "mapping_known": True,
         "stale_mapped_sets": 0, "stored_name_status": "CONSISTENT"},
        {"code": 1111, "sets": 1, "named_sets": 0, "mapping_known": False,
         "stale_mapped_sets": 0, "stored_name_status": "MISSING"},
        {"code": 1770, "sets": 1, "named_sets": 0, "mapping_known": True,
         "stale_mapped_sets": 1, "stored_name_status": "MISSING"},
        {"code": 1988, "sets": 1, "named_sets": 0, "mapping_known": False,
         "stale_mapped_sets": 0, "stored_name_status": "MISSING"},
    ]
    assert result["days"][0] == {
        "date": "2026-09-23", "workouts": 2, "strength_workouts": 2,
        "missing_strength_details": 1, "unsynced_strength_details": 1,
        "stale_mapped_lap_sets": 1,
    }
    assert "owner" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_workout_audit_filters_local_dates_and_requires_existing_database(tmp_path):
    database = tmp_path / "workouts.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE workouts (user_id TEXT, source TEXT, started_at TEXT, data TEXT, detail TEXT, detail_synced INTEGER)"
        )
        _workout(db, "owner", "2026-09-22 16:30:00", "strength")
        _workout(db, "owner", "2026-09-23 16:30:00", "strength")

    result = audit_workouts(
        database, "owner", start=date(2026, 9, 23), end=date(2026, 9, 23),
    )
    assert result["totals"]["workouts"] == 1
    assert result["days"][0]["date"] == "2026-09-23"
    with pytest.raises(sqlite3.OperationalError):
        audit_workouts(tmp_path / "missing.db", "owner")
    assert not (tmp_path / "missing.db").exists()
