"""Refresh verified display labels in already-normalized Zepp strength laps.

This never fetches vendor data or treats lap names as prescription evidence.
"""

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from vitalis.connectors.zepp.parser import ZEPP_STRENGTH_LAP_LABELS
from vitalis.models import WORKOUT_DETAIL_SCHEMA_VERSION


_LEGACY_DISPLAY_NAMES = {1770: "坐姿杠铃颈前推肩"}


def _connection(database: Path, *, writable: bool) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"
    return sqlite3.connect(database.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=5)


def _changes(
    db: sqlite3.Connection, user_id: str, allowed_codes: set[int],
) -> tuple[list[tuple[int, str, str]], Counter, Counter]:
    changes = []
    counts = Counter()
    kinds = Counter()
    rows = db.execute(
        "SELECT id, detail FROM workouts WHERE user_id = ? AND source = ? AND detail IS NOT NULL",
        (user_id, "zepp"),
    )
    for workout_row_id, original in rows:
        try:
            detail = json.loads(original)
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION:
            continue
        observations = detail.get("strength_sets")
        if not isinstance(observations, list):
            continue
        changed = False
        for item in observations:
            if not isinstance(item, dict) or item.get("source") != "lap_62":
                continue
            code = item.get("vendor_exercise_code")
            if isinstance(code, bool) or not isinstance(code, int) or code not in allowed_codes:
                continue
            name = item.get("exercise_name")
            limitations = item.get("limitations")
            if not isinstance(limitations, list):
                continue
            if (
                code in _LEGACY_DISPLAY_NAMES
                and name == _LEGACY_DISPLAY_NAMES[code]
                and "exercise_name_reference_mapping" in limitations
            ):
                item["exercise_name"] = ZEPP_STRENGTH_LAP_LABELS[code]
                kinds["corrected_alias"] += 1
            elif (
                (name is None or isinstance(name, str) and not name.strip())
                and "exercise_name_unverified" in limitations
            ):
                item["exercise_name"] = ZEPP_STRENGTH_LAP_LABELS[code]
                item["limitations"] = [
                    "exercise_name_reference_mapping" if flag == "exercise_name_unverified" else flag
                    for flag in limitations
                ]
                kinds["filled_blank"] += 1
            else:
                continue
            counts[code] += 1
            changed = True
        if changed:
            changes.append((workout_row_id, original, json.dumps(detail, ensure_ascii=False)))
    return changes, counts, kinds


def _validate_backup(
    database: Path, backup: Path | None, user_id: str,
    changes: list[tuple[int, str, str]],
) -> None:
    if backup is None or not backup.is_file() or backup.resolve() == database.resolve():
        raise ValueError("apply requires a separate existing backup")
    with closing(_connection(backup, writable=False)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("backup integrity check failed")
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "workouts"),
        ).fetchone() is None:
            raise ValueError("backup does not contain workouts")
        for row_id, previous, _ in changes:
            original = db.execute(
                "SELECT user_id, source, detail FROM workouts WHERE id = ?", (row_id,),
            ).fetchone()
            if original != (user_id, "zepp", previous):
                raise ValueError("backup does not match target workout detail")


def refresh_strength_labels(
    database: Path,
    user_id: str,
    *,
    codes: set[int] | None = None,
    apply: bool = False,
    backup: Path | None = None,
) -> dict:
    if not user_id:
        raise ValueError("user_id is required")
    allowed_codes = set(ZEPP_STRENGTH_LAP_LABELS) if codes is None else set(codes)
    if not allowed_codes or allowed_codes - ZEPP_STRENGTH_LAP_LABELS.keys():
        raise ValueError("codes must be a nonempty subset of verified lap labels")
    preview_changes = []
    if apply:
        with closing(_connection(database, writable=False)) as db:
            preview_changes, _, _ = _changes(db, user_id, allowed_codes)
        _validate_backup(database, backup, user_id, preview_changes)

    with closing(_connection(database, writable=apply)) as db:
        if apply:
            db.execute("BEGIN IMMEDIATE")
        try:
            changes, counts, kinds = _changes(db, user_id, allowed_codes)
            if apply:
                if {(row_id, previous) for row_id, previous, _ in changes} != {
                    (row_id, previous) for row_id, previous, _ in preview_changes
                }:
                    raise RuntimeError("workout detail changed during backup validation")
                for row_id, previous, updated in changes:
                    result = db.execute(
                        "UPDATE workouts SET detail = ? WHERE id = ? AND detail = ? AND user_id = ? AND source = ?",
                        (updated, row_id, previous, user_id, "zepp"),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("workout detail changed during label refresh")
                db.commit()
        except Exception:
            if apply:
                db.rollback()
            raise
    return {
        "mode": "applied" if apply else "dry_run",
        "source_coverage_unchanged": True,
        "workouts_to_update": len(changes),
        "sets_to_label": sum(counts.values()),
        "sets_from_blank": kinds["filled_blank"],
        "sets_corrected_alias": kinds["corrected_alias"],
        "codes": [{"code": code, "sets": count} for code, count in sorted(counts.items())],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只刷新已有 Zepp 力量组的已核验显示名")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--code", action="append", type=int, dest="codes")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    result = refresh_strength_labels(
        args.database, args.user,
        codes=set(args.codes) if args.codes else None,
        apply=args.apply, backup=args.backup,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
