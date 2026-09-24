"""Read-only inventory of stored workouts and strength-detail gaps.

This does not establish vendor source coverage; that requires the sync ledger.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from vitalis.connectors.zepp.parser import ZEPP_STRENGTH_LAP_LABELS
from vitalis.models import WORKOUT_DETAIL_SCHEMA_VERSION


def _json_object(value: str | None) -> dict | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _local_date(value: str | None, zone: ZoneInfo) -> date | None:
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def audit_workouts(
    database: Path,
    user_id: str,
    *,
    timezone_name: str = "Asia/Shanghai",
    start: date | None = None,
    end: date | None = None,
) -> dict:
    if not user_id:
        raise ValueError("user_id is required")
    if start and end and start > end:
        raise ValueError("start must not be after end")
    zone = ZoneInfo(timezone_name)
    database_uri = database.resolve().as_uri() + "?mode=ro"
    totals = Counter()
    days: dict[str, Counter] = defaultdict(Counter)
    codes: Counter[int] = Counter()
    cloud_types: Counter[int] = Counter()
    named_codes: Counter[int] = Counter()
    stale_mapped_codes: Counter[int] = Counter()
    names_by_code: dict[int, set[str]] = defaultdict(set)
    invalid_codes = 0

    with closing(sqlite3.connect(database_uri, uri=True)) as db:
        rows = db.execute(
            "SELECT source, started_at, data, detail, detail_synced FROM workouts WHERE user_id = ? ORDER BY started_at",
            (user_id,),
        )
        for source, started_at, data_json, detail_json, detail_synced in rows:
            day = _local_date(started_at, zone)
            if day is None:
                totals["undated_workouts"] += 1
                if start or end:
                    continue
            elif (start and day < start) or (end and day > end):
                continue

            summary = _json_object(data_json)
            detail = _json_object(detail_json)
            totals["workouts"] += 1
            daily = days[day.isoformat()] if day else None
            if daily is not None:
                daily["workouts"] += 1
            if summary is None:
                totals["invalid_summaries"] += 1
            if not detail_synced:
                totals["unsynced_workout_details"] += 1
            if detail_json is None:
                totals["missing_workout_details"] += 1
            elif detail is None:
                totals["invalid_workout_details"] += 1
            elif detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION:
                totals["outdated_workout_details"] += 1
            if source == "zepp" and summary is not None:
                vendor_type = summary.get("vendor_type_id")
                if isinstance(vendor_type, int) and not isinstance(vendor_type, bool):
                    cloud_types[vendor_type] += 1
                else:
                    totals["untyped_zepp_workouts"] += 1
            family = (summary or {}).get("training_family")
            strength_workout = family == "strength"
            if strength_workout:
                totals["strength_workouts"] += 1
                if daily is not None:
                    daily["strength_workouts"] += 1
                if not detail_synced:
                    totals["unsynced_strength_details"] += 1
                    if daily is not None:
                        daily["unsynced_strength_details"] += 1
            if detail_json is None:
                if strength_workout:
                    totals["missing_strength_details"] += 1
                    if daily is not None:
                        daily["missing_strength_details"] += 1
                continue
            if detail is None:
                if strength_workout:
                    totals["invalid_strength_details"] += 1
                    if daily is not None:
                        daily["invalid_strength_details"] += 1
                continue
            if strength_workout and detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION:
                totals["outdated_strength_details"] += 1
                if daily is not None:
                    daily["outdated_strength_details"] += 1

            sets = detail.get("strength_sets")
            if not isinstance(sets, list):
                if strength_workout:
                    totals["missing_strength_sets"] += 1
                continue
            if strength_workout and not sets:
                totals["empty_strength_sets"] += 1
            lap_sets = [
                item for item in sets
                if isinstance(item, dict) and item.get("source") == "lap_62"
            ]
            if lap_sets and not strength_workout:
                totals["misclassified_strength_details"] += 1
                if daily is not None:
                    daily["misclassified_strength_details"] += 1
            for observation in lap_sets:
                totals["observed_lap_sets"] += 1
                code = observation.get("vendor_exercise_code")
                if isinstance(code, bool) or not isinstance(code, int) or code < 1:
                    invalid_codes += 1
                    continue
                codes[code] += 1
                name = observation.get("exercise_name")
                if isinstance(name, str) and name.strip():
                    named_codes[code] += 1
                    names_by_code[code].add(name.strip())
                expected_name = ZEPP_STRENGTH_LAP_LABELS.get(code)
                if expected_name and name != expected_name:
                    totals["stale_mapped_lap_sets"] += 1
                    stale_mapped_codes[code] += 1
                    if daily is not None:
                        daily["stale_mapped_lap_sets"] += 1

    return {
        "source_coverage": "NOT_ASSESSED",
        "window": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "timezone": timezone_name,
        },
        "totals": {
            key: totals[key] for key in (
                "workouts", "undated_workouts", "invalid_summaries",
                "missing_workout_details", "unsynced_workout_details",
                "invalid_workout_details", "outdated_workout_details",
                "untyped_zepp_workouts", "strength_workouts", "missing_strength_details",
                "unsynced_strength_details", "invalid_strength_details", "outdated_strength_details",
                "missing_strength_sets", "empty_strength_sets", "observed_lap_sets",
                "stale_mapped_lap_sets", "misclassified_strength_details",
            )
        } | {"invalid_lap_codes": invalid_codes},
        "days": [
            {"date": day, **counts}
            for day, counts in sorted(days.items())
        ],
        "cloud_workout_types": [
            {"code": code, "workouts": count}
            for code, count in sorted(cloud_types.items())
        ],
        "strength_codes": [
            {
                "code": code,
                "sets": count,
                "named_sets": named_codes[code],
                "mapping_known": code in ZEPP_STRENGTH_LAP_LABELS,
                "stale_mapped_sets": stale_mapped_codes[code],
                "stored_name_status": (
                    "CONFLICT" if len(names_by_code[code]) > 1 else
                    "CONSISTENT" if named_codes[code] == count else
                    "PARTIAL" if named_codes[code] else "MISSING"
                ),
            }
            for code, count in sorted(codes.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读盘点已保存的运动与力量明细")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--timezone", default=os.getenv("VITALIS_TIMEZONE", "Asia/Shanghai"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    result = audit_workouts(
        args.database, args.user, timezone_name=args.timezone,
        start=args.start, end=args.end,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
