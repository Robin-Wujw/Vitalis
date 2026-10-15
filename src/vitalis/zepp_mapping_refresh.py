"""Preview or refresh verified Zepp display mappings without refetching health data."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json

from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.source_journal import SourceJournalRecord, SourceJournalRepository
from vitalis.adapters.zepp.catalog import verified_lap_labels
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.sport_types import (
    CLOUD_SPORT_NAMESPACE, DEVICE_SPORT_NAMESPACE, resolve_cloud_sport_mode, resolve_sport_mode,
)
from vitalis.domain import WORKOUT_DETAIL_SCHEMA_VERSION
from vitalis.time import local_day


_CLOUD_SOURCES = {
    "zepp_cloud_observed", "zepp_cloud_reference", "zepp_os_cloud_compatible", "zepp_apk_cloud_catalog",
}
_DEVICE_SOURCES = {"public_zepp_enum", "zepp_apk_device_catalog"}


def _namespace(data: dict) -> str | None:
    namespace = data.get("vendor_type_namespace")
    if namespace in {CLOUD_SPORT_NAMESPACE, DEVICE_SPORT_NAMESPACE}:
        return namespace
    source = data.get("recognition_source")
    if source in _CLOUD_SOURCES:
        return CLOUD_SPORT_NAMESPACE
    if source in _DEVICE_SOURCES:
        return DEVICE_SPORT_NAMESPACE
    return None


def _journal_namespaces(db, user_id: str, wanted: set[tuple[str, int]]) -> dict:
    """Resolve old ambiguous IDs from owner-scoped, integrity-checked raw summaries."""
    evidence = defaultdict(set)
    if not wanted:
        return evidence
    journal = SourceJournalRepository(db)
    rows = db.execute(select(SourceJournalRecord.id, SourceJournalRecord.record_id).where(
        SourceJournalRecord.user_id == user_id, SourceJournalRecord.source == "zepp",
        SourceJournalRecord.stream == "workouts",
    ))
    parser = ZeppParser()
    for journal_id, record_id in rows:
        parts = record_id.split(":")
        if len(parts) < 3 or parts[0] != "sport_history":
            continue
        payload = journal.read_payload(journal_id, user_id=user_id)
        if isinstance(payload, bytes):
            payload = json.loads(payload)
        for workout in parser.parse_sport_history(payload, sport_hint=parts[1]):
            key = workout.workout_id, workout.vendor_type_id
            if key in wanted:
                evidence[key].add(workout.vendor_type_namespace)
    return {key: next(iter(values)) for key, values in evidence.items() if len(values) == 1}


def _refresh_detail(original: dict, labels: dict[int, str]) -> tuple[dict, int]:
    detail = deepcopy(original)
    count = 0
    if detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION:
        return detail, count
    for item in detail.get("strength_sets") or []:
        if not isinstance(item, dict) or item.get("source") != "lap_62":
            continue
        code, flags = item.get("vendor_exercise_code"), item.get("limitations")
        if type(code) is not int or code not in labels or not isinstance(flags, list):
            continue
        if "user_confirmed" in flags:
            continue
        name = item.get("exercise_name")
        reference = "exercise_name_reference_mapping" in flags
        blank = (name is None or isinstance(name, str) and not name.strip())
        if not reference and not (blank and "exercise_name_unverified" in flags):
            continue
        updated_flags = [flag for flag in flags if flag != "exercise_name_unverified"]
        if "exercise_name_reference_mapping" not in updated_flags:
            updated_flags.append("exercise_name_reference_mapping")
        if name != labels[code] or flags != updated_flags:
            item["exercise_name"], item["limitations"] = labels[code], updated_flags
            count += 1
    return detail, count


def refresh_zepp_mappings(db, user_id: str, *, apply: bool = False) -> dict:
    """Caller owns the transaction; dry run performs no writes or network calls."""
    repo = HealthRepository(db)
    if not repo.user_exists(user_id):
        raise ValueError("user does not exist")
    rows = list(db.execute(select(orm.Workout).where(
        orm.Workout.user_id == user_id, orm.Workout.source == "zepp",
    ).order_by(orm.Workout.id)).scalars())
    wanted = {
        (row.workout_id, row.data["vendor_type_id"]) for row in rows
        if type((row.data or {}).get("vendor_type_id")) is int and _namespace(row.data) is None
    }
    journal_namespaces = _journal_namespaces(db, user_id, wanted)
    labels, counts, days = verified_lap_labels(), Counter(), set()
    for row in rows:
        data = row.data or {}
        code = data.get("vendor_type_id")
        namespace = _namespace(data) or journal_namespaces.get((row.workout_id, code))
        classification = {}
        if type(code) is int and namespace is None:
            counts["namespace_unresolved"] += 1
        if namespace:
            resolver = resolve_cloud_sport_mode if namespace == CLOUD_SPORT_NAMESPACE else resolve_sport_mode
            mode = resolver(code)
            if mode.recognition_confidence != "NONE":
                classification = {
                    "type": mode.category, "sport_mode": mode.code, "sport_mode_label": mode.label_zh,
                    "training_family": mode.family, "recognition_confidence": mode.recognition_confidence,
                    "recognition_confidence_label": mode.recognition_confidence_label,
                    "recognition_source": mode.recognition_source,
                    "recognition_source_label": mode.recognition_source_label,
                    "vendor_type_namespace": namespace,
                }
            elif type(code) is int:
                counts["sport_codes_unmapped"] += 1
        changed_classification = bool(classification) and any(
            data.get(key) != classification[key]
            for key in ("type", "sport_mode", "sport_mode_label", "training_family")
        )
        detail, changed_sets = _refresh_detail(row.detail or {}, labels)
        if not changed_classification and not changed_sets:
            continue
        counts["workouts_to_update"] += 1
        counts["workouts_to_reclassify"] += int(changed_classification)
        counts["sets_to_label"] += changed_sets
        if row.started_at:
            days.add(local_day(row.started_at))
        if not apply:
            continue
        if changed_classification:
            repo.save_workout_classification(user_id, row.workout_id, classification)
        if changed_sets:
            fetched = detail.get("fetched_at")
            if not isinstance(fetched, str):
                raise ValueError("original detail fetched_at is required")
            repo.save_workout_detail(
                user_id, row.workout_id, detail, samples=None,
                fetched_at=datetime.fromisoformat(fetched.replace("Z", "+00:00")),
                event_type="catalog_refresh",
            )
    if apply and days:
        repo.rebuild_training_days(user_id, days)
    return {
        "mode": "applied" if apply else "dry_run", "source_coverage_unchanged": True,
        **{key: counts[key] for key in (
            "workouts_to_update", "workouts_to_reclassify", "sets_to_label",
            "namespace_unresolved", "sport_codes_unmapped",
        )},
        "affected_activity_days": sorted(day.isoformat() for day in days),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with session_scope() as db:
        result = refresh_zepp_mappings(db, args.user, apply=args.apply)
        if not args.apply:
            db.rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
