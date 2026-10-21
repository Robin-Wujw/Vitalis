"""Independent physical-volume and muscle-exposure accounting.

Region labels come from Zepp catalog actions 42--58 (body-part selectors),
not the unrelated eight-item menu. Unlabelled overlays remain unknown.
"""

from collections import defaultdict
from math import isfinite

from vitalis.adapters.zepp.catalog import LAP_62_NAMESPACE, load_catalog


REFERENCE_REGIONS = {
    **dict.fromkeys((1, 2, 7, 8), ("core", "腹部")),
    **dict.fromkeys((3, 4, 38, 39), ("shoulders", "肩部")),
    **dict.fromkeys((5, 6), ("chest", "胸部")),
    **dict.fromkeys((10, 11), ("arm_front", "上臂前侧")),
    **dict.fromkeys((22, 23), ("arm_back", "上臂后侧")),
    **dict.fromkeys((12, 13, 24, 25), ("forearms", "前臂")),
    **dict.fromkeys((14, 15), ("thigh_front", "大腿前侧")),
    **dict.fromkeys((28, 29), ("thigh_back", "大腿后侧")),
    **dict.fromkeys((16, 17), ("lower_leg_front", "小腿前侧")),
    **dict.fromkeys((30, 31), ("calves", "小腿后侧")),
    **dict.fromkeys((19, 20, 34, 35), ("back", "背部")),
    **dict.fromkeys((36, 37), ("lower_back", "下背部")),
    **dict.fromkeys((26, 27), ("glutes", "臀部")),
    **dict.fromkeys((40, 41), ("neck", "颈部")),
}


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def one_rep_max(weight, repetitions, *, unit, basis, rir=None):
    w, effort = number(weight), number(rir)
    if w is None or w <= 0 or type(repetitions) is not int or not 2 <= repetitions <= 10:
        return None
    if unit not in {"kg", "lb"} or basis not in {"total", "per_hand", "machine"} or effort is None or not 0 <= effort <= 2:
        return None
    return round(w * (0.45359237 if unit == "lb" else 1) * (1 + repetitions / 30), 1)


def _as_dict(item):
    return item if isinstance(item, dict) else item.model_dump(mode="python")


def _rows(session):
    explicit = [_as_dict(x) for x in session.get("explicit_exercises") or []]
    confirmed = [x for x in explicit if x.get("source") == "user_confirmed"]
    observed = [_as_dict(x) for x in session.get("observed_sets") or []]
    selected = confirmed or observed or explicit
    output, seen = [], set()
    for index, item in enumerate(selected):
        if observed and not confirmed:
            key = (item.get("source"), item.get("order", index), item.get("started_at"))
            if key in seen:
                continue
            seen.add(key)
        count = item.get("sets") if not observed or confirmed else 1
        if isinstance(count, list):
            output.extend({**item, **row} for row in count if isinstance(row, dict))
        elif type(count) is int and count > 0:
            output.extend([item] * count)
    return output


def _reference(item):
    if item.get("source") != "lap_62" or type(item.get("vendor_exercise_code")) is not int:
        return (), (), None, set()
    catalog = load_catalog()
    resolution = catalog.resolve(LAP_62_NAMESPACE, item["vendor_exercise_code"])
    if resolution.status != "verified":
        return (), (), None, set()
    primary = {REFERENCE_REGIONS[r] for r in resolution.primary_regions if r in REFERENCE_REGIONS}
    secondary = {REFERENCE_REGIONS[r] for r in resolution.secondary_regions if r in REFERENCE_REGIONS} - primary
    unknown = (set(resolution.primary_regions) | set(resolution.secondary_regions)) - REFERENCE_REGIONS.keys()
    return primary, secondary, catalog.catalog_revision, unknown


def _doses(grouped, qualification, version, metadata):
    return [
        {"muscle_group": key, "muscle_group_label": label, "side": side,
         "direct_sets": values[0], "indirect_sets": values[1],
         "equivalent_sets": values[0] + 0.5 * values[1],
         "working_sets_known": values[2] == values[0] + values[1],
         "qualification": qualification, "mapping_version": version, **metadata}
        for (key, label, side), values in sorted(grouped.items())
    ]


def session_capacity(session, knowledge, muscle_labels):
    rows = _rows(session)
    volume_by_basis, capacity = defaultdict(float), {}
    qualified, references = defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])
    coverage = {"recorded_sets": len(rows), "volume_sets": 0, "mapped_sets": 0,
                "working_sets": 0, "reference_sets": 0, "unmapped_region_sets": 0, "unmapped_region_ids": [],
                "summary_sets": session.get("vendor_reported_sets"), "detail_complete": False}
    reference_version = load_catalog().catalog_revision
    for row in rows:
        side = row.get('side') if row.get('side') in {'left', 'right', 'bilateral'} else 'unknown'
        working = row.get("set_kind") == "working"
        coverage["working_sets"] += working
        primary, secondary, version, unknown = _reference(row)
        if unknown:
            coverage['unmapped_region_sets'] += 1
            coverage['unmapped_region_ids'] = sorted(set(coverage['unmapped_region_ids']) | unknown)
        if primary or secondary:
            coverage["reference_sets"] += 1
        if row.get("set_kind") != "warmup":
            for muscles, position in ((primary, 0), (secondary, 1)):
                for muscle in muscles:
                    references[(*muscle, side)][position] += 1
                    references[(*muscle, side)][2] += working
        _, main, sub = knowledge(row.get("exercise_id"), row.get("exercise_name")) if row.get("source") != "lap_62" else ("unknown", (), ())
        if main or sub:
            coverage["mapped_sets"] += 1
            if row.get("set_kind") != "warmup":
                for muscles, position in ((main, 0), (set(sub) - set(main), 1)):
                    for muscle in muscles:
                        qualified[(muscle, muscle_labels[muscle], side)][position] += 1
                        qualified[(muscle, muscle_labels[muscle], side)][2] += working
        reps = row.get("repetitions")
        w = number(row.get("weight_kg"))
        unit = "kg" if w is not None else row.get("weight_unit")
        w = w if w is not None else number(row.get("weight_value"))
        basis = row.get("weight_basis")
        if type(reps) is not int or reps <= 0 or w is None or w < 0 or unit not in {"kg", "lb"} or basis not in {"per_hand", "total", "machine"}:
            continue
        kg = w * (0.45359237 if unit == "lb" else 1)
        volume = kg * reps
        volume_by_basis[basis] += volume
        coverage["volume_sets"] += 1
        identity = str(row.get("exercise_id") or row.get("exercise_name") or "unknown").casefold()
        key = (identity, basis)
        entry = capacity.setdefault(key, {
            "exercise_id": identity, "exercise_name": row.get("exercise_name") or "动作未明确",
            "weight_unit": "kg", "weight_basis": basis, "volume": 0,
            "volume_unit": "kg*reps", "estimated_1rm": None, "estimated_1rm_unit": "kg",
            "personal_record": False, "muscle_groups": list(main) + list(sub),
        })
        entry["volume"] += volume
        estimate = one_rep_max(kg, reps, unit="kg", basis=basis, rir=row.get("rir")) if main else None
        if estimate is not None:
            entry["estimated_1rm"] = max(entry["estimated_1rm"] or 0, estimate)
    coverage["detail_complete"] = coverage["summary_sets"] is not None and coverage["summary_sets"] == len(rows)
    for item in capacity.values():
        item["volume"] = round(item["volume"], 1)
    limitations = []
    if coverage["volume_sets"] < len(rows):
        limitations.append("部分组缺少明确物理负重、次数或负重口径；容量仅包含合格记录，自重与辅助负重另列。")
    if coverage["working_sets"] < len(rows):
        limitations.append("部分组未标记热身或工作组；肌群组数表示训练接触，不等于有效增肌组。")
    if not rows:
        limitations.append("缺少逐组记录，未估算容量或肌群组数。")
    if coverage['unmapped_region_sets']:
        limitations.append('部分厂商人体图区域尚无已核验文字解释，部位汇总仅覆盖已核验区域。')
    provenance = {'source': session.get('source') or 'unknown',
                  'observed_at': session.get('started_at') or session.get('date'), 'fetched_at': session.get('fetched_at')}
    qualified_origin = {**provenance, 'source_scope': 'explicit_exercise_knowledge'}
    if rows and all(r.get('source') == 'user_confirmed' for r in rows):
        qualified_origin['source'] = 'user'
    return {
        "capacity": list(capacity.values()), "volume_by_basis": {k: round(v, 1) for k, v in volume_by_basis.items()},
        "coverage": coverage, "muscle_doses": _doses(qualified, "qualified", "strength-knowledge-1", qualified_origin),
        "reference_muscle_doses": _doses(references, "vendor_reference", reference_version, {**provenance, 'source_scope': 'catalog_and_workout_detail'}),
        "limitations": limitations,
    }


def aggregate_doses(sessions, field):
    grouped, templates = defaultdict(lambda: [0, 0, 0]), {}
    for session in sessions:
        for dose in getattr(session, field):
            key = (dose.muscle_group, dose.muscle_group_label, dose.side, dose.source)
            templates[key] = dose
            grouped[key][0] += dose.direct_sets
            grouped[key][1] += dose.indirect_sets
            grouped[key][2] += dose.working_sets_known * (dose.direct_sets + dose.indirect_sets)
    return [templates[key].model_copy(update={
        "direct_sets": v[0], "indirect_sets": v[1], "equivalent_sets": v[0] + v[1] * 0.5,
        "working_sets_known": v[2] == v[0] + v[1],
    }) for key, v in sorted(grouped.items())]
