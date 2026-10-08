"""Pure projections for user-facing report presentation fields.

The functions here copy already-qualified profile values. They do not calculate
health scores, trends, prescriptions, or inferred exercise progress.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .period_activity import comparison_gate
from .report_formatting import energy_label, metric_label, minutes_text, number, payload_of, unique, value_with_unit


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _date_value(value: Any) -> str | None:
    parsed = _date(value)
    return parsed.isoformat() if parsed else None


def _same_day(value: Any, target: Any) -> bool:
    left, right = _date(value), _date(target)
    return left is not None and left == right


def _canonical(value: Any) -> str:
    return "".join(str(value or "").casefold().split()).replace("_", "")


def _context(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("report_context")
    return context if isinstance(context, dict) else {}


def _as_of(payload: dict[str, Any]) -> Any:
    return _context(payload).get("as_of")


def _reference_period(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read reference dates already resolved by the period engine."""
    context = _context(payload)
    candidates = [
        context.get("reference_period"),
        {
            "start": context.get("reference_period_start") or context.get("reference_start"),
            "end": context.get("reference_period_end") or context.get("reference_end"),
        },
    ]
    for reference in candidates:
        if not isinstance(reference, dict):
            continue
        start, end = reference.get("start"), reference.get("end")
        if _date(start) and _date(end):
            return _date_value(start), _date_value(end)
    return None, None


def _deviation_comparison(
    deviation: Any,
    payload: dict[str, Any],
    *,
    allow: bool = True,
) -> dict[str, Any] | None:
    """Project existing baseline fields without deriving a deviation."""
    if not allow or not isinstance(deviation, dict):
        return None
    reference = _number(deviation.get("baseline_reference"))
    change = _number(deviation.get("percent"))
    if reference is None and change is None:
        return None
    window = _integer(deviation.get("baseline_window_days"))
    label = f"个人参照（近 {window} 日）" if window else "个人参照"
    start, end = _reference_period(payload)
    return {
        "label": label,
        "reference_value": reference,
        "change_percent": change,
        "reference_period_start": start,
        "reference_period_end": end,
    }


def _period_comparison(
    payload: dict[str, Any],
    reference_value: Any,
    change_percent: Any,
    *,
    eligible: bool = True,
) -> dict[str, Any] | None:
    """Project an existing eligible period comparison with explicit dates."""
    reference = _number(reference_value)
    change = _number(change_percent)
    if not eligible or reference is None or change is None:
        return None
    start, end = _reference_period(payload)
    if not start or not end:
        return None
    return {
        "label": "上期",
        "reference_value": reference,
        "change_percent": change,
        "reference_period_start": start,
        "reference_period_end": end,
    }


def _metric(
    key: str,
    label: str,
    value: Any,
    unit: str,
    *,
    digits: int = 0,
    comparison: dict[str, Any] | None = None,
    detail: str | None = None,
    gap: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": _number(value),
        "unit": unit,
        "digits": max(0, min(int(digits), 2)),
        "comparison": comparison,
        "detail": detail,
        "gap": gap,
    }


def _valid_metric(metric: dict[str, Any]) -> bool:
    return metric.get("value") is not None or bool(metric.get("gap"))


def _qualified_hrv(hrv: dict[str, Any]) -> bool:
    value = _number(hrv.get("value_ms"))
    if value is None or value <= 0:
        return False
    status = str(hrv.get("status") or "")
    if status and status not in {"AVAILABLE", "available"}:
        return False
    return (
        hrv.get("corroboration_status") not in {"conflicting", "insufficient"}
        and not bool(hrv.get("corroboration_affects_decision"))
    )


def _strength_sessions(training: dict[str, Any]) -> list[dict[str, Any]]:
    strength = training.get("strength") or {}
    if not isinstance(strength, dict):
        return []
    return [item for item in strength.get("recent_sessions") or [] if isinstance(item, dict)]


def _daily_strength_duration(training: dict[str, Any], target: Any) -> tuple[float | None, int]:
    direct = _number(training.get("strength_duration_minutes"))
    if direct is not None:
        return direct, _integer(training.get("strength_sessions")) or 0
    sessions = [item for item in _strength_sessions(training) if _same_day(item.get("date"), target)]
    durations = [_number(item.get("duration_minutes")) for item in sessions]
    known = [value for value in durations if value is not None and value >= 0]
    return (sum(known) if known and len(known) == len(durations) else None, len(sessions))


def _metric_deviation(metric: Any) -> dict[str, Any] | None:
    if not isinstance(metric, dict):
        return None
    deviation = metric.get("deviation")
    return deviation if isinstance(deviation, dict) else None


def _daily_metrics(payload: dict[str, Any], *, morning: bool) -> list[dict[str, Any]]:
    features = payload.get("features") or {}
    sleep = features.get("sleep") or {}
    hrv = features.get("hrv") or {}
    activity = features.get("activity") or {}
    training = features.get("training") or {}
    target = payload.get("date")
    output: list[dict[str, Any]] = []

    sleep_value = _number(sleep.get("duration_minutes"))
    detail = None
    if sleep_value is not None:
        parts = []
        if sleep.get("bedtime"):
            parts.append(f"入睡 {str(sleep['bedtime'])[:5]}")
        if sleep.get("wake_time"):
            parts.append(f"醒来 {str(sleep['wake_time'])[:5]}")
        detail = "；".join(parts) or None
    output.append(_metric(
        "sleep", "睡眠时长", sleep_value, "min", digits=0,
        comparison=_deviation_comparison(sleep.get("duration_deviation"), payload) if sleep_value is not None else None,
        detail=detail,
        gap=None if sleep_value is not None else "昨晚睡眠记录尚未同步。",
    ))

    if morning:
        rhr = _number(hrv.get("rhr_bpm"))
        if rhr is not None:
            output.append(_metric(
                "rhr", "静息心率", rhr, "bpm", digits=0,
                comparison=_deviation_comparison(hrv.get("rhr_deviation"), payload),
            ))
        if _qualified_hrv(hrv):
            preferred_label = hrv.get("preferred_device_label")
            output.append(_metric(
                "hrv", "HRV", hrv.get("value_ms"), "ms", digits=0,
                comparison=_deviation_comparison(hrv.get("deviation"), payload),
                detail=preferred_label if preferred_label != "Zepp 厂商汇总" else None,
            ))
        if len(output) < 3:
            vitals = features.get("overnight_vitals") or {}
            oxygen = vitals.get("oxygen") or {}
            if oxygen.get("status") == "AVAILABLE" and _number(oxygen.get("median_percent")) is not None:
                output.append(_metric("oxygen", "夜间血氧中位数", oxygen["median_percent"], "%", digits=0))
        return output[:3]

    steps = activity.get("steps") if isinstance(activity, dict) else None
    steps_value = _number(steps.get("value")) if isinstance(steps, dict) else None
    steps_comparison = _deviation_comparison(
        _metric_deviation(steps), payload,
        allow=bool(_context(payload).get("target_day_complete", True)),
    )
    if steps_value is not None:
        output.append(_metric(
            "steps", "步数", steps_value, "steps", digits=0,
            comparison=steps_comparison,
        ))
    strength_duration, session_count = _daily_strength_duration(training, target)
    if strength_duration is not None:
        output.append(_metric(
            "strength_duration", "力量训练时长", strength_duration, "min", digits=0,
            detail=f"{session_count} 场" if session_count else None,
        ))
    return output[:3]


def _period_activity_metric(activity: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in activity.get("metrics") or []:
        if isinstance(item, dict) and item.get("metric") == name:
            return item
    return None


def _period_days(payload: dict[str, Any], period: str) -> int:
    start, end = _date(payload.get("period_start")), _date(payload.get("period_end"))
    if start and end:
        return (end - start).days + 1
    return 7 if period == "weekly" else 28


def _reference_days(payload: dict[str, Any]) -> int:
    start, end = _reference_period(payload)
    if start and end:
        return (_date(end) - _date(start)).days + 1
    return 0


def _gate(days: int, payload: dict[str, Any]) -> int:
    if days < 1:
        return 0
    return comparison_gate(days, _context(payload).get("period_mode"))


def _period_sleep_metric(payload: dict[str, Any], facts: dict[str, Any], period: str) -> dict[str, Any]:
    sleep = facts.get("sleep") or {}
    value = _number(sleep.get("average_minutes"))
    previous = _number(sleep.get("previous_average_minutes"))
    change = _number(sleep.get("change_percent"))
    current_days = _integer(sleep.get("available_days"))
    period_days = _period_days(payload, period)
    current_gate, previous_gate = _gate(period_days, payload), _gate(_reference_days(payload), payload)
    eligible = (
        current_days is not None and current_days >= current_gate
        and _integer(sleep.get("previous_available_days")) is not None
        and previous_gate > 0
        and _integer(sleep.get("previous_available_days")) >= previous_gate
    )
    gap = None
    if value is None:
        gap = "本期没有可用睡眠平均值。"
    elif current_days is not None and current_days < current_gate:
        gap = f"本期睡眠仅有 {current_days}/{period_days} 晚，未达到趋势比较门槛。"
    return _metric(
        "sleep_average", "平均睡眠", value, "min", digits=0,
        comparison=_period_comparison(payload, previous, change, eligible=eligible),
        gap=gap,
    )


def _period_metrics(payload: dict[str, Any], period: str) -> list[dict[str, Any]]:
    facts = payload.get("facts") or {}
    activity = facts.get("activity") or {}
    training = facts.get("training") or {}
    output = [_period_sleep_metric(payload, facts, period)]
    current_gate = _gate(_period_days(payload, period), payload)
    previous_gate = _gate(_reference_days(payload), payload)

    steps_item = _period_activity_metric(activity, "steps")
    if steps_item is not None:
        steps_value = _number(steps_item.get("average"))
        previous = _number(steps_item.get("previous_average"))
        change = _number(steps_item.get("change_percent"))
        complete = _integer(steps_item.get("complete_days"))
        previous_complete = _integer(steps_item.get("previous_complete_days"))
        limitations = " ".join(str(item) for item in steps_item.get("limitations") or [])
        eligible = (
            complete is not None and complete >= current_gate
            and previous_complete is not None and previous_gate > 0
            and previous_complete >= previous_gate
            and "来源不同" not in limitations
        )
        output.append(_metric(
            "steps_average", "平均步数", steps_value, "steps/day", digits=0,
            comparison=_period_comparison(payload, previous, change, eligible=eligible),
            gap=None if steps_value is not None else "本期没有可用步数平均值。",
        ))
    else:
        steps_value = _number(activity.get("average_steps"))
        if steps_value is not None:
            available = _integer(activity.get("available_days"))
            previous_available = _integer(activity.get("previous_available_days"))
            eligible = (
                available is not None and available >= current_gate
                and previous_available is not None and previous_gate > 0
                and previous_available >= previous_gate
            )
            output.append(_metric(
                "steps_average", "平均步数", steps_value, "steps/day", digits=0,
                comparison=_period_comparison(
                    payload, activity.get("previous_average_steps"),
                    activity.get("steps_change_percent"), eligible=eligible,
                ),
            ))
        else:
            active_item = _period_activity_metric(activity, "active_minutes")
            active_value = _number(active_item.get("average")) if active_item else _number(activity.get("active_minutes"))
            if active_value is not None:
                complete = _integer(active_item.get("complete_days")) if active_item else None
                previous_complete = _integer(active_item.get("previous_complete_days")) if active_item else None
                eligible = (
                    complete is not None and complete >= current_gate
                    and previous_complete is not None and previous_gate > 0
                    and previous_complete >= previous_gate
                )
                output.append(_metric(
                    "activity_average", "平均活动时长", active_value, "min", digits=0,
                    comparison=_period_comparison(
                        payload,
                        active_item.get("previous_average") if active_item else None,
                        active_item.get("change_percent") if active_item else None,
                        eligible=eligible,
                    ),
                ))

    strength_sessions = _number(training.get("strength_sessions"))
    if strength_sessions is not None:
        eligible = training.get("strength_sessions_comparison_eligible") is True
        output.append(_metric(
            "strength_frequency", "力量训练频次", strength_sessions, "sessions", digits=0,
            comparison=_period_comparison(
                payload, training.get("previous_strength_sessions"),
                training.get("strength_sessions_change_percent"), eligible=eligible,
            ),
        ))
    return [item for item in output if _valid_metric(item)][:3]


def _workout_key(item: dict[str, Any]) -> tuple[str, str] | None:
    source, workout_id = item.get("source"), item.get("workout_id")
    if source in (None, "") or workout_id in (None, ""):
        return None
    return str(source), str(workout_id)


def _workout_title(item: dict[str, Any]) -> str:
    return str(item.get("sport_mode_label") or item.get("type_label") or item.get("title") or "训练")


def _pace_text(value: Any) -> str | None:
    numeric = _number(value)
    if numeric is None or numeric < 0:
        return None
    total = int(numeric)
    return f"{total // 60}:{total % 60:02d}/公里"


def _running_facts(session: dict[str, Any]) -> list[str]:
    facts = []
    for key, label, unit, digits in (("moving_duration_minutes", "实际运动", "分钟", 1), ("distance_km", "距离", "公里", 2)):
        shown = number(session.get(key), digits)
        if shown is not None:
            facts.append(f"{label} {shown} {unit}")
    pace = _pace_text(session.get("average_pace_seconds_per_km"))
    if pace:
        facts.append(f"平均配速 {pace}")
    for key, label in (("average_heart_rate_bpm", "平均心率"), ("maximum_heart_rate_bpm", "最高心率")):
        shown = number(session.get(key), 0)
        if shown is not None:
            facts.append(f"{label} {shown} 次/分钟")
    return facts


def _generic_facts(workout: dict[str, Any]) -> list[str]:
    facts = []
    shown = number(workout.get("distance_km"), 2)
    if shown is not None and workout.get("training_family") != "strength":
        facts.append(f"距离 {shown} 公里")
    for key, label in (("heart_rate_avg_bpm", "平均心率"), ("heart_rate_max_bpm", "最高心率")):
        shown = number(workout.get(key), 0)
        if shown is not None:
            facts.append(f"{label} {shown} 次/分钟")
    calories = number(workout.get("calories_kcal"))
    if calories is not None:
        facts.append(f"本次训练估算热量 {calories} 千卡")
    return facts


def _set_from_item(item: dict[str, Any], order: int) -> dict[str, Any]:
    repetitions = _integer(item.get("repetitions"))
    weight_value = _number(item.get("weight_value"))
    weight_unit = item.get("weight_unit")
    if weight_value is None:
        weight_value = _number(item.get("weight_kg"))
        weight_unit = "kg" if weight_value is not None else None
    return {
        "order": _integer(item.get("order")) or order,
        "repetitions": repetitions,
        "weight_value": weight_value,
        "weight_unit": str(weight_unit) if weight_unit not in (None, "") else None,
        "weight_basis": item.get("weight_basis"),
        "duration_seconds": _integer(item.get("duration_seconds")),
        "rest_seconds": _integer(item.get("rest_seconds")),
    }


def _exercise_name(item: dict[str, Any]) -> str:
    value = item.get("exercise_name") or item.get("exercise_id")
    if value not in (None, ""):
        return str(value)
    code = item.get("vendor_exercise_code")
    return f"动作代码 {code}（名称未确认）" if code is not None else "动作名称未确认"


def _exercise_identity(item: dict[str, Any]) -> str:
    return _canonical(item.get("exercise_id") or item.get("exercise_name") or item.get("vendor_exercise_code"))


def _expanded_sets(item: dict[str, Any]) -> list[dict[str, Any]]:
    explicit_sets = item.get("sets")
    if isinstance(explicit_sets, list):
        return [_set_from_item(row, index) for index, row in enumerate(explicit_sets, 1) if isinstance(row, dict)]
    count = _integer(explicit_sets) or 1
    return [_set_from_item(item, index) for index in range(1, max(count, 1) + 1)]


def _exercise_comparison(group: dict[str, Any], comparisons: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for item in comparisons:
        if not isinstance(item, dict) or item.get("comparable") is not True:
            continue
        identity = _canonical(item.get("exercise_id") or item.get("exercise_name") or item.get("name"))
        if identity and identity != group.get("_identity"):
            continue
        delta = _number(item.get("delta_total_repetitions"))
        reference_date = _date_value(item.get("reference_workout_date"))
        if delta in (None, 0) or not reference_date:
            continue
        count = item.get("set_count")
        if count is not None and count != len(group["sets"]):
            continue
        repetitions = item.get("current_repetitions")
        if repetitions is not None and repetitions != [row.get("repetitions") for row in group["sets"]]:
            continue
        direction = "增加" if delta > 0 else "减少"
        return f"同样重量和组数下，总次数{direction} {number(abs(delta), 0)} 次。", reference_date
    return None, None


def _exercise_groups(items: list[dict[str, Any]], comparisons: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = _exercise_identity(item)
        if groups and groups[-1]["_identity"] == identity:
            groups[-1]["sets"].extend(_expanded_sets(item))
        else:
            groups.append({"_identity": identity, "name": _exercise_name(item), "exercise_id": item.get("exercise_id"), "sets": _expanded_sets(item)})
    output = []
    for group in groups:
        comparison, reference_date = _exercise_comparison(group, comparisons or [])
        output.append({"name": group["name"], "exercise_id": group.get("exercise_id"), "set_count": len(group["sets"]), "sets": group["sets"], "comparison": comparison, "reference_date": reference_date})
    return output


def _confirmed_and_vendor(exercises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmed = [item for item in exercises if item.get("source") == "user_confirmed"]
    if not confirmed:
        return exercises
    identities = {_exercise_identity(item) for item in confirmed}
    return [item for item in exercises if item.get("source") == "user_confirmed" or _exercise_identity(item) not in identities]


def _strength_exercises(session: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = [item for item in session.get("explicit_exercises") or [] if isinstance(item, dict)]
    observed = [item for item in session.get("observed_sets") or [] if isinstance(item, dict)]
    if any(item.get("source") == "user_confirmed" for item in explicit) or not observed:
        return _exercise_groups(_confirmed_and_vendor(explicit), session.get("comparisons"))
    return _exercise_groups(observed, session.get("comparisons"))


def _strength_facts(session: dict[str, Any]) -> list[str]:
    facts = []
    sets = _integer(session.get("vendor_reported_sets"))
    if sets is not None:
        facts.append(f"设备记录组数 {sets} 组")
    for key, label in (("average_heart_rate_bpm", "平均心率"), ("maximum_heart_rate_bpm", "最高心率")):
        shown = number(session.get(key), 0)
        if shown is not None:
            facts.append(f"{label} {shown} 次/分钟")
    return facts


def _workout_projection(item: dict[str, Any], *, facts: list[str] | None = None, exercises: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    workout_date = _date_value(item.get("date"))
    if workout_date is None:
        return None
    return {"title": _workout_title(item), "date": workout_date, "started_at": item.get("started_at"), "duration_minutes": _number(item.get("duration_minutes")), "facts": unique(facts or []), "exercises": exercises or []}


def _training_projection(payload: dict[str, Any], *, include_observed_day: Any = None) -> list[dict[str, Any]]:
    training = (payload.get("features") or {}).get("training") or {}
    target = include_observed_day if include_observed_day is not None else payload.get("date")
    generic = [item for item in training.get("recent_workouts") or [] if isinstance(item, dict) and _same_day(item.get("date"), target)]
    running = [item for item in ((training.get("running") or {}).get("recent_sessions") or []) if isinstance(item, dict) and _same_day(item.get("date"), target)]
    strength = [item for item in ((training.get("strength") or {}).get("recent_sessions") or []) if isinstance(item, dict) and _same_day(item.get("date"), target)]
    specialists = running + strength
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in specialists:
        key = _workout_key(item)
        if key is not None:
            by_key.setdefault(key, []).append(item)
    output: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for workout in generic:
        matched = None
        key = _workout_key(workout)
        if key is not None and len(by_key.get(key, [])) == 1:
            matched = by_key[key][0]
            consumed.add(id(matched))
        facts = _generic_facts(workout)
        exercises: list[dict[str, Any]] = []
        if matched is not None:
            if matched in running:
                facts.extend(_running_facts(matched))
            else:
                facts.extend(_strength_facts(matched))
                exercises = _strength_exercises(matched)
        projected = _workout_projection(workout, facts=facts, exercises=exercises)
        if projected is not None:
            output.append(projected)
    for item in specialists:
        if id(item) in consumed:
            continue
        projected = dict(item)
        if item in running:
            projected["title"] = (item.get("classification_label") or "跑步专项") if item.get("confidence") in {"MODERATE", "HIGH"} else "跑步课型暂不确定"
            workout = _workout_projection(projected, facts=_running_facts(item))
        else:
            projected["title"] = item.get("focus_label") or "力量训练"
            workout = _workout_projection(projected, facts=_strength_facts(item), exercises=_strength_exercises(item))
        if workout is not None:
            output.append(workout)
    return output


def _strength_change_texts(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    findings, suggestions = [], []
    target = payload.get("date")
    training = (payload.get("features") or {}).get("training") or {}
    for session in _strength_sessions(training):
        if not _same_day(session.get("date"), target):
            continue
        for comparison in session.get("comparisons") or []:
            if not isinstance(comparison, dict) or comparison.get("comparable") is not True:
                continue
            delta = _number(comparison.get("delta_total_repetitions"))
            refdate = _date_value(comparison.get("reference_workout_date"))
            name = comparison.get("exercise_name") or comparison.get("exercise_id")
            if delta in (None, 0) or not refdate or not name:
                continue
            direction = "增加" if delta > 0 else "减少"
            amount = number(abs(delta), 0)
            findings.append(f"同样重量和组数下，{name}总次数较{refdate}{direction}{amount}次")
    if findings:
        suggestions.append("下次训练重点观察同样重量下的组次记录")
    return unique(findings), unique(suggestions)


def _findings(payload: dict[str, Any]) -> list[str]:
    return unique([str(item) for item in (payload.get("inferences") or {}).get("key_changes") or [] if item])[:4]


def _suggestions(payload: dict[str, Any]) -> list[str]:
    output = []
    for item in (payload.get("actions") or {}).get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("code") or "") in {"WEEKLY_INSUFFICIENT_DATA", "MONTHLY_INSUFFICIENT_DATA"}:
            continue
        action = item.get("action")
        if action:
            output.append(f"{item.get('title')}：{action}" if item.get("title") else str(action))
    return unique(output)[:2]


def _period_alerts(payload: dict[str, Any]) -> list[str]:
    return unique([str(item["summary"]) for item in (payload.get("inferences") or {}).get("events") or [] if isinstance(item, dict) and str(item.get("severity") or "").upper() in {"HIGH", "CRITICAL"} and item.get("summary")])[:3]


def _daily_alerts(payload: dict[str, Any], *, facts_only: bool) -> list[str]:
    if facts_only:
        metadata = _context(payload).get("delivery_metadata") or {}
        return ["本次同步未完整完成，仅使用已保存的数据。"] if metadata.get("sync_degraded") else []
    return unique([str(item["summary"]) for item in payload.get("events") or [] if isinstance(item, dict) and item.get("lifecycle") != "RESOLVED" and item.get("summary")])[:3]


def _range_text(value: Any, unit: str) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    low, high = _number(value[0]), _number(value[1])
    if low is None or high is None:
        return None
    low_text, high_text = number(low), number(high)
    return f"{low_text if low_text == high_text else low_text + '–' + high_text} {unit}"


def _verified_safety_event(payload: dict[str, Any]) -> bool:
    safety_types = {"PAIN_OR_INJURY", "INJURY", "SAFETY_GATE", "SAFETY_EVENT"}
    return any(isinstance(event, dict) and event.get("lifecycle") != "RESOLVED" and str(event.get("type") or "").upper() in safety_types for event in payload.get("events") or [])


def _plan_suggestion(session: dict[str, Any], *, include_safety: bool) -> str | None:
    title = session.get("title") or session.get("session_type_label")
    if not title:
        return None
    parts = [str(title)]
    duration = _range_text(session.get("total_duration_minutes"), "分钟")
    if duration:
        parts.append(duration)
    if session.get("intensity_label"):
        parts.append(str(session["intensity_label"]))
    if include_safety:
        parts.extend(str(stop) for stop in session.get("stop_conditions") or [] if stop)
    return " · ".join(parts)


def _daily_suggestions(payload: dict[str, Any]) -> list[str]:
    decision = payload.get("decision") or {}
    if decision.get("action") == "INSUFFICIENT_DATA":
        return []
    plan = decision.get("action_plan") or {}
    include_safety = plan.get("safety_status") == "LIMITED" or _verified_safety_event(payload)
    output = []
    if (_context(payload).get("delivery_metadata") or {}).get("retrospective"):
        return []
    for key in ("primary_session", "optional_session"):
        session = plan.get(key)
        if isinstance(session, dict):
            suggestion = _plan_suggestion(session, include_safety=include_safety)
            if suggestion:
                prefix = "主要安排" if key == "primary_session" else "可选安排"
                if key == "optional_session" and plan.get("session_relationship") == "ALTERNATIVE":
                    prefix = "替代选择（与主要安排二选一）"
                output.append(f"{prefix}：{suggestion}")
    return unique(output)[:2]


def _recovery_conflict_finding(payload: dict[str, Any]) -> list[str]:
    hrv = (payload.get("features") or {}).get("hrv") or {}
    if hrv.get("corroboration_affects_decision") or hrv.get("corroboration_status") == "conflicting":
        return ["HRV 记录存在来源分歧，分别保留，不合并比较。"]
    return []


def _morning_findings(payload: dict[str, Any]) -> list[str]:
    decision = payload.get("decision") or {}
    return unique(_recovery_conflict_finding(payload) + [str(item) for item in decision.get("driver_labels") or [] if item])[:3]


def _pressure_display_section(payload: dict[str, Any]) -> dict[str, Any] | None:
    activity = (payload.get("features") or {}).get("activity") or {}
    facts = []
    labels = {"stress": "平均压力评分", "stress_min": "最低压力评分", "stress_max": "最高压力评分", "stress_relaxed_pct": "放松区间", "stress_normal_pct": "正常区间", "stress_medium_pct": "中等压力区间", "stress_high_pct": "高压力区间"}
    for item in activity.get("stress_summary") or []:
        if not isinstance(item, dict) or item.get("metric") not in labels:
            continue
        shown = number(item.get("value"))
        if shown is not None:
            suffix = "%" if str(item.get("metric")).endswith("_pct") else ""
            facts.append(f"{labels[item['metric']]} {shown}{suffix}")
    heart = activity.get("heart_rate") or {}
    if isinstance(heart, dict):
        average = number(heart.get("average"))
        if average is not None:
            facts.append(f"心率记录均值 {average} 次/分钟")
    if not facts:
        return None
    return {"key": "display_signals", "title": "日内记录", "facts": facts, "interpretation": [], "limitations": [], "display": True}


def daily_presentation(payload: Any, *, morning: bool = False, facts_only: bool = False) -> dict[str, Any]:
    raw = payload_of(payload)
    metrics = _daily_metrics(raw, morning=morning)
    if morning:
        decision = {} if facts_only else raw.get("decision") or {}
        action_label = decision.get("action_label") or "今天的安排"
        headline = "已记录昨夜事实，今天暂不生成训练安排" if facts_only or decision.get("action") == "INSUFFICIENT_DATA" else f"今天安排：{action_label}"
        target = _date(raw.get("date"))
        training = _training_projection(raw, include_observed_day=target - timedelta(days=1)) if facts_only and target else []
        return {"headline": headline, "as_of": _as_of(raw), "metrics": metrics, "findings": [] if facts_only else _morning_findings(raw), "training": training, "suggestions": [] if facts_only else _daily_suggestions(raw), "alerts": _daily_alerts(raw, facts_only=facts_only)}
    findings, suggestions = _strength_change_texts(raw)
    training = _training_projection(raw)
    headline = "今日训练与活动回顾" if training else "今日活动回顾"
    if findings:
        for session in _strength_sessions((raw.get("features") or {}).get("training") or {}):
            if not _same_day(session.get("date"), raw.get("date")):
                continue
            change = next((item for item in session.get("comparisons") or [] if item.get("comparable") and item.get("delta_total_repetitions")), None)
            if change:
                name = change.get("exercise_name") or change.get("exercise_id") or "力量训练"
                headline = f"{name}总次数{'增加' if change['delta_total_repetitions'] > 0 else '减少'}"
                break
    return {"headline": headline, "as_of": _as_of(raw), "metrics": metrics, "findings": unique(findings + _recovery_conflict_finding(raw)), "training": training, "suggestions": suggestions, "alerts": _daily_alerts(raw, facts_only=False)}


def period_presentation(payload: Any, period: str) -> dict[str, Any]:
    raw = payload_of(payload)
    return {"headline": "本周睡眠与训练结构" if period == "weekly" else "本月睡眠与训练结构", "as_of": _as_of(raw), "metrics": _period_metrics(raw, period), "findings": _findings(raw), "training": [], "suggestions": _suggestions(raw), "alerts": _period_alerts(raw)}


def internal_sections(sections: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**dict(section), "display": False} for section in sections]


def daily_display_sections(payload: Any, *, facts_only: bool = False) -> list[dict[str, Any]]:
    """Select measured, user-useful facts for the public report body."""
    raw = payload_of(payload)
    features = raw.get("features") or {}
    sections: list[dict[str, Any]] = []
    # Key metrics already carry sleep, steps, and their comparisons.
    hrv = features.get("hrv") or {}
    recovery_facts = []
    if _qualified_hrv(hrv):
        recovery_facts.append(f"{metric_label(hrv.get('preferred_metric'))} {number(hrv.get('value_ms'))} 毫秒")
    if _number(hrv.get("rhr_bpm")) is not None:
        recovery_facts.append(f"{metric_label(hrv.get('rhr_metric') or 'resting_hr')} {number(hrv['rhr_bpm'])} 次/分钟")
    vitals = features.get("overnight_vitals") or {}
    oxygen = vitals.get("oxygen") or {}
    if _number(oxygen.get("median_percent")) is not None:
        recovery_facts.append(f"夜间血氧中位数 {number(oxygen['median_percent'])}%")
    if recovery_facts:
        sections.append({"key": "display_recovery", "title": "昨夜恢复背景", "facts": recovery_facts, "interpretation": [], "limitations": [], "display": True})
    activity = features.get("activity") or {}
    activity_facts = []
    for key, label, unit, digits in (("distance_km", "活动距离", "公里", 2), ("active_minutes", "活动时长", "分钟", 0)):
        item = activity.get(key) or {}
        value = _number(item.get("value")) if isinstance(item, dict) else None
        if value is not None:
            activity_facts.append(f"{label} {number(value, digits)} {unit}")
    for item in activity.get("energy") or []:
        if item.get("role") == "workout":
            continue
        shown = value_with_unit(item.get("value"), item.get("unit"))
        if shown:
            activity_facts.append(f"{energy_label(item.get('role'))} {shown}")
    if activity_facts:
        sections.append({"key": "display_activity", "title": "日常活动与能量", "facts": unique(activity_facts), "interpretation": [], "limitations": [], "display": True})
    pressure = _pressure_display_section(raw)
    if pressure:
        sections.append(pressure)
    if facts_only:
        return sections
    return sections


__all__ = ["daily_presentation", "period_presentation", "internal_sections", "daily_display_sections"]
