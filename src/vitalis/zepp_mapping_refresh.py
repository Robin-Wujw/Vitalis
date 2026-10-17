"""Preview or refresh verified Zepp display mappings without refetching health data."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import json

from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.persistence.source_journal import (
    RAW_SCHEMA_VERSION, JournalError, SourceJournalRecord, SourceJournalRepository,
)
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
_WEIGHT_FIELDS = (
    "weight_value", "weight_unit", "weight_kg", "weight_basis",
    "vendor_weight_value", "vendor_weight_unit",
)
_WEIGHT_FLAGS = {
    "weight_unit_unverified", "weight_unavailable", "bodyweight_vendor_reference",
    "non_mass_weight_encoding",
}


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


def _matching_weight_observation(saved: dict, parsed: dict) -> bool:
    if saved.get("repetitions") != parsed.get("repetitions"):
        return False
    if any(saved.get(key) is not None for key in ("vendor_weight_value", "vendor_weight_unit")):
        return all(saved.get(key) == parsed.get(key) for key in ("vendor_weight_value", "vendor_weight_unit"))
    # Before weight semantics were verified, lap[21] was retained only within
    # this range. Match that historical observation, never a newly scaled value.
    raw_weight = parsed.get("vendor_weight_value")
    previous_weight = raw_weight if raw_weight is not None and 0 <= raw_weight <= 2000 else None
    return saved.get("weight_value") == previous_weight


def _refresh_strength_weights(db, row, detail: dict, source_mode: str, counts: Counter) -> int:
    if detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION:
        return 0
    saved_sets = [item for item in detail.get("strength_sets") or []
                  if isinstance(item, dict) and item.get("source") == "lap_62"]
    if not saved_sets:
        return 0
    counts["strength_weight_workouts_checked"] += 1
    if source_mode not in {"real", "mock", "replay"} or not row.vendor_source:
        counts["strength_weight_raw_missing"] += 1
        return 0
    raw_id = db.scalar(select(SourceJournalRecord.id).where(
        SourceJournalRecord.user_id == row.user_id,
        SourceJournalRecord.source == "zepp",
        SourceJournalRecord.source_mode == source_mode,
        SourceJournalRecord.stream == "workout_detail",
        SourceJournalRecord.record_id == f"workout_detail:{row.workout_id}:{row.vendor_source}",
        SourceJournalRecord.capability == "verified",
        SourceJournalRecord.incomplete.is_(False),
        SourceJournalRecord.raw_schema_version == RAW_SCHEMA_VERSION,
    ).order_by(SourceJournalRecord.fetched_at.desc(), SourceJournalRecord.id.desc()).limit(1))
    if raw_id is None:
        counts["strength_weight_raw_missing"] += 1
        return 0
    try:
        payload = SourceJournalRepository(db).read_payload(raw_id, user_id=row.user_id)
        if isinstance(payload, bytes):
            payload = json.loads(payload)
    except (JournalError, ValueError, UnicodeError):
        counts["strength_weight_raw_invalid"] += 1
        return 0
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else payload
    if not isinstance(data, dict) or str(data.get("trackid")) != row.workout_id or not isinstance(data.get("lap"), str):
        counts["strength_weight_raw_invalid"] += 1
        return 0
    if ZeppParser._strength_sets(data.get("strengthSets")):
        counts["strength_weight_explicit_preferred"] += 1
        return 0
    parsed_sets = {
        (item.order, item.vendor_exercise_code): item.model_dump(mode="json")
        for item in ZeppParser._strength_lap_sets(data["lap"])
    }
    changed = 0
    for saved in saved_sets:
        flags = saved.get("limitations") or []
        if not isinstance(flags, list) or "user_confirmed" in flags:
            counts["strength_weight_sets_protected"] += 1
            continue
        parsed = parsed_sets.get((saved.get("order"), saved.get("vendor_exercise_code")))
        if parsed is None or not _matching_weight_observation(saved, parsed):
            counts["strength_weight_sets_unmatched"] += 1
            continue
        values = {key: parsed.get(key) for key in _WEIGHT_FIELDS}
        updated_flags = [flag for flag in flags if flag not in _WEIGHT_FLAGS]
        updated_flags.extend(flag for flag in parsed.get("limitations", []) if flag in _WEIGHT_FLAGS)
        if all(saved.get(key) == value for key, value in values.items()) and flags == updated_flags:
            continue
        saved.update(values, limitations=updated_flags)
        changed += 1
    return changed


def refresh_zepp_mappings(db, user_id: str, *, apply: bool = False, strength_weights: bool = False) -> dict:
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
    confirmed_workouts = set(db.scalars(select(orm.StrengthExercise.workout_id).where(
        orm.StrengthExercise.user_id == user_id,
        orm.StrengthExercise.workout_source == "zepp",
        orm.StrengthExercise.source == "user_confirmed",
    ))) if strength_weights else set()
    source_mode = repo.source_mode(user_id) if strength_weights else "unknown"
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
        changed_weights = 0
        if strength_weights and classification.get("training_family", data.get("training_family")) == "strength":
            if row.workout_id in confirmed_workouts:
                counts["strength_weight_workouts_protected"] += 1
            else:
                changed_weights = _refresh_strength_weights(db, row, detail, source_mode, counts)
        if not changed_classification and not changed_sets and not changed_weights:
            continue
        counts["workouts_to_update"] += 1
        counts["workouts_to_reclassify"] += int(changed_classification)
        counts["sets_to_label"] += changed_sets
        counts["sets_to_refresh_weights"] += changed_weights
        if row.started_at:
            days.add(local_day(row.started_at))
        if not apply:
            continue
        if changed_classification:
            repo.save_workout_classification(user_id, row.workout_id, classification)
        if changed_sets or changed_weights:
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
            "sets_to_refresh_weights", "strength_weight_workouts_checked",
            "strength_weight_raw_missing", "strength_weight_raw_invalid",
            "strength_weight_sets_unmatched", "strength_weight_sets_protected",
            "strength_weight_workouts_protected",
            "strength_weight_explicit_preferred",
        )},
        "affected_activity_days": sorted(day.isoformat() for day in days),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--strength-weights", action="store_true", help="Refresh saved strength weights from matching encrypted source records")
    args = parser.parse_args()
    with session_scope() as db:
        result = refresh_zepp_mappings(db, args.user, apply=args.apply, strength_weights=args.strength_weights)
        if not args.apply:
            db.rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
