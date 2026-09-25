from contextlib import closing
import json
import shutil
import sqlite3

import pytest

from vitalis.strength_label_refresh import refresh_strength_labels


def _insert(db, row_id, user, source, schema_version, sets):
    payload = {"schema_version": schema_version, "strength_sets": sets, "fetched_at": "2026-09-22T12:00:00Z"}
    db.execute(
        "INSERT INTO workouts (id, user_id, source, detail) VALUES (?, ?, ?, ?)",
        (row_id, user, source, json.dumps(payload, ensure_ascii=False)),
    )


def _database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY, user_id TEXT, source TEXT, detail TEXT)")
        observed = [
            {"source": "lap_62", "vendor_exercise_code": 14, "exercise_name": None,
             "repetitions": 10, "weight_value": 7, "weight_unit": None,
             "limitations": ["exercise_name_unverified", "weight_unit_unverified"]},
            {"source": "lap_62", "vendor_exercise_code": 114, "exercise_name": "",
             "limitations": ["exercise_name_unverified", "weight_unavailable"]},
            {"source": "lap_62", "vendor_exercise_code": 64, "exercise_name": "引体向上",
             "limitations": ["exercise_name_reference_mapping"]},
            {"source": "lap_62", "vendor_exercise_code": 1988, "exercise_name": None,
             "limitations": ["exercise_name_unverified"]},
            {"source": "strength_sets", "vendor_exercise_code": 65, "exercise_name": None,
             "limitations": ["exercise_name_unverified"]},
        ]
        _insert(db, 1, "owner", "zepp", "5.1", observed)
        _insert(db, 2, "other", "zepp", "5.1", observed)
        _insert(db, 3, "owner", "other", "5.1", observed)
        _insert(db, 4, "owner", "zepp", "4.0", observed)
        _insert(db, 5, "owner", "zepp", "5.1", [{
            "source": "lap_62", "vendor_exercise_code": 14,
            "exercise_name": "已确认的个性化名称", "limitations": ["exercise_name_unverified"],
        }])
        _insert(db, 6, "owner", "zepp", "5.1", [{
            "source": "lap_62", "vendor_exercise_code": 14,
            "exercise_name": 123, "limitations": ["exercise_name_unverified"],
        }])
        db.commit()


def _details(path):
    with closing(sqlite3.connect(path)) as db:
        return {row_id: json.loads(detail) for row_id, detail in db.execute(
            "SELECT id, detail FROM workouts ORDER BY id"
        )}


def test_refresh_dry_run_is_read_only_and_requires_verified_codes(tmp_path):
    database = tmp_path / "workouts.db"
    _database(database)
    before = database.read_bytes()

    result = refresh_strength_labels(database, "owner", codes={14, 114, 1770})

    assert result == {
        "mode": "dry_run", "source_coverage_unchanged": True,
        "workouts_to_update": 1, "sets_to_label": 2,
        "codes": [{"code": 14, "sets": 1}, {"code": 114, "sets": 1}],
    }
    assert database.read_bytes() == before
    with pytest.raises(ValueError, match="verified lap labels"):
        refresh_strength_labels(database, "owner", codes={1988})
    with pytest.raises(ValueError, match="backup"):
        refresh_strength_labels(database, "owner", codes={14}, apply=True)
    assert database.read_bytes() == before


def test_refresh_only_missing_lap_labels_is_idempotent(tmp_path):
    database = tmp_path / "workouts.db"
    backup = tmp_path / "before.db"
    _database(database)
    shutil.copyfile(database, backup)
    before = _details(database)

    result = refresh_strength_labels(database, "owner", codes={14, 114, 1770}, apply=True, backup=backup)

    assert result["mode"] == "applied"
    assert result["workouts_to_update"] == 1
    assert result["sets_to_label"] == 2
    after = _details(database)
    sets = after[1]["strength_sets"]
    assert sets[0]["exercise_name"] == "侧平举"
    assert sets[0]["limitations"] == ["exercise_name_reference_mapping", "weight_unit_unverified"]
    assert sets[0]["weight_value"] == 7
    assert sets[0]["weight_unit"] is None
    assert sets[0]["repetitions"] == 10
    assert sets[1]["exercise_name"] == "蝴蝶机反向飞鸟"
    assert sets[1]["limitations"] == ["exercise_name_reference_mapping", "weight_unavailable"]
    assert sets[2:] == before[1]["strength_sets"][2:]
    assert after[1]["fetched_at"] == before[1]["fetched_at"]
    assert {key: after[key] for key in (2, 3, 4, 5, 6)} == {key: before[key] for key in (2, 3, 4, 5, 6)}
    assert _details(backup) == before
    again = refresh_strength_labels(database, "owner", codes={14, 114, 1770}, apply=True, backup=backup)
    assert again["sets_to_label"] == 0
    assert again["workouts_to_update"] == 0


def test_refresh_refuses_backup_without_workout_table(tmp_path):
    database = tmp_path / "workouts.db"
    empty_backup = tmp_path / "empty.db"
    _database(database)
    with closing(sqlite3.connect(empty_backup)):
        pass
    with pytest.raises(ValueError, match="does not contain workouts"):
        refresh_strength_labels(database, "owner", codes={14}, apply=True, backup=empty_backup)


def test_refresh_refuses_unrelated_backup_even_when_healthy(tmp_path):
    database = tmp_path / "workouts.db"
    backup = tmp_path / "different.db"
    _database(database)
    shutil.copyfile(database, backup)
    with closing(sqlite3.connect(backup)) as db:
        db.execute("UPDATE workouts SET detail = ? WHERE id = 1", (json.dumps({"schema_version": "5.1"}),))
        db.commit()
    before = database.read_bytes()

    with pytest.raises(ValueError, match="backup does not match target workout detail"):
        refresh_strength_labels(database, "owner", codes={14}, apply=True, backup=backup)

    assert database.read_bytes() == before
