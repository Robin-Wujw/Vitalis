"""Strength knowledge, explicit exercise normalization, and session analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from functools import lru_cache
from math import isfinite
from statistics import median
from uuid import uuid4

from vitalis.domain import StrengthSetObservation
from vitalis.domain.strength_knowledge import CANONICAL_FAMILIES

from .contracts import (
    Availability,
    ConfidenceBand,
    ExerciseHypothesis,
    MuscleLoadStatus,
    MuscleRecoveryStatus,
    StrengthAnalysis,
    StrengthExerciseComparison,
    StrengthExerciseInput,
    StrengthExerciseRecord,
    StrengthSessionAnalysis,
)
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .running import RunningAnalyzer, _parse_datetime
from .workout_effort import workout_rpe

PATTERN_LABELS = {
    "squat": "下蹲",
    "hinge": "髋伸",
    "horizontal_push": "水平推",
    "horizontal_pull": "水平拉",
    "vertical_push": "垂直推",
    "vertical_pull": "垂直拉",
    "unilateral_leg": "单腿",
    "core": "核心",
    "carry": "负重行走",
    "isolation": "孤立动作",
    "unknown": "动作模式未知",
}
MUSCLE_LABELS = {
    "chest": "胸部",
    "back": "背部",
    "shoulders": "肩部",
    "biceps": "肱二头肌",
    "triceps": "肱三头肌",
    "quadriceps": "股四头肌",
    "hamstrings": "腘绳肌",
    "glutes": "臀部",
    "calves": "小腿",
    "core": "核心",
}
FOCUS_LABELS = {
    "PUSH": "推类",
    "PULL": "拉类",
    "LEGS": "腿部",
    "UPPER": "上肢",
    "LOWER": "下肢",
    "FULL_BODY": "全身",
    "CHEST": "胸部",
    "BACK": "背部",
    "SHOULDERS": "肩部",
    "ARMS": "手臂",
    "UNKNOWN": "重点未知",
}
SPLIT_LABELS = {
    "FULL_BODY": "全身训练",
    "UPPER_LOWER": "上下肢分化",
    "PUSH_PULL_LEGS": "推、拉、腿三分化",
    "FIVE_DAY": "五分化",
    "UNRESOLVED": "尚未识别训练分化",
}


# Exact reviewed aliases, movement pattern, typical primary/secondary muscles.
# Secondary exposure counts half a set; physical weight is never apportioned.
EXERCISE_KNOWLEDGE = (
    (("reversefly", "反向飞鸟", "蝴蝶机反向飞鸟", "facepull", "面拉"), "horizontal_pull", ("shoulders",), ("back",)),
    (("chestfly", "夹胸", "哑铃飞鸟"), "isolation", ("chest",), ("shoulders",)),
    (("straightarm", "直臂下拉"), "vertical_pull", ("back",), ()),
    (("dip", "双杠臂屈伸"), "vertical_push", ("chest", "triceps"), ("shoulders",)),
    (("benchpress", "bench_press", "卧推", "杠铃卧推", "哑铃卧推", "俯卧撑", "pushup"), "horizontal_push", ("chest",), ("triceps", "shoulders")),
    (("row", "划船"), "horizontal_pull", ("back",), ("biceps",)),
    (("pullup", "pull_up", "引体向上", "高位下拉", "latpulldown"), "vertical_pull", ("back",), ("biceps",)),
    (("overheadpress", "shoulderpress", "推举", "肩推", "推肩"), "vertical_push", ("shoulders",), ("triceps",)),
    (("kettlebellswing", "壶铃甩摆"), "hinge", ("glutes", "hamstrings"), ("back", "core")),
    (("deadlift", "硬拉", "杠铃硬拉"), "hinge", ("glutes", "quadriceps"), ("hamstrings", "back")),
    (("罗马尼亚硬拉", "rdl"), "hinge", ("hamstrings", "glutes"), ("back",)),
    (("hipthrust", "臀推", "杠铃胯部推举"), "hinge", ("glutes",), ("hamstrings",)),
    (("squat", "深蹲", "杠铃深蹲", "腿举", "legpress"), "squat", ("quadriceps", "glutes"), ()),
    (("lunge", "弓步", "分腿蹲", "split squat"), "unilateral_leg", ("quadriceps", "glutes"), ()),
    (("plank", "平板支撑", "deadbug", "死虫", "卷腹"), "core", ("core",), ()),
    (("carry", "农夫行走"), "carry", ("core",), ("shoulders",)),
    (("bicepcurl", "biceps_curl", "二头肌弯举"), "isolation", ("biceps",), ()),
    (("臂屈伸", "肱三头肌下压", "肱三头肌推举", "肱三头肌屈伸"), "isolation", ("triceps",), ()),
    (("calf", "提踵", "负重提踵"), "isolation", ("calves",), ()),
    (("legcurl", "腿弯举", "俯卧弹力绳腿弯举"), "isolation", ("hamstrings",), ()),
    (("lateralraise", "侧平举"), "isolation", ("shoulders",), ()),
)
SECONDARY_MUSCLE_WEIGHT = 0.5


@dataclass(frozen=True)
class _StrengthDose:
    """Ordered set facts used only by the deterministic comparison pass."""

    identity: tuple[str, str]
    exercise_id: str | None
    exercise_name: str
    repetitions: tuple[int | None, ...]
    weights: tuple[float | None, ...]
    set_count: int | None
    weight_unit: str | None
    weight_basis: str | None


def normalize_exercise(
    user_id: str,
    workout_id: str,
    order: int,
    value: StrengthExerciseInput,
    session_focus: str | None = None,
    workout_source: str = "zepp",
    created_at: datetime | None = None,
) -> StrengthExerciseRecord:
    pattern, muscles = classify_exercise(value.exercise_id, value.exercise_name)
    return StrengthExerciseRecord(
        id=uuid4().hex,
        user_id=user_id,
        workout_source=workout_source,
        workout_id=workout_id,
        order=order,
        exercise_name=value.exercise_name.strip(),
        exercise_id=value.exercise_id,
        session_focus=session_focus,
        movement_pattern=pattern,
        movement_pattern_label=PATTERN_LABELS[pattern],
        muscle_groups=list(muscles),
        muscle_group_labels=[MUSCLE_LABELS[item] for item in muscles],
        sets=value.sets,
        repetitions=value.repetitions,
        weight_kg=value.weight_kg,
        weight_unit=value.weight_unit or ("kg" if value.weight_kg is not None else None),
        weight_basis=value.weight_basis,
        rpe=value.rpe,
        rir=value.rir,
        rest_seconds=value.rest_seconds,
        source="user_confirmed",
        confidence=ConfidenceBand.HIGH,
        confidence_label=CONFIDENCE_LABELS[ConfidenceBand.HIGH.value],
        created_at=created_at,
    )


@lru_cache(maxsize=2048)
def _knowledge(exercise_id: str | None, exercise_name: str | None):
    def key(value):
        return ''.join(c for c in (value or '').casefold() if c.isalnum())
    registry = {key(alias): (pattern, primary, secondary)
                for aliases, pattern, primary, secondary in EXERCISE_KNOWLEDGE for alias in aliases}
    registry.update({key(canonical): registry[key(family)] for canonical, family in CANONICAL_FAMILIES.items()})
    by_id, by_name = registry.get(key(exercise_id)), registry.get(key(exercise_name))
    if by_id and by_name and by_id != by_name:
        return 'unknown', (), ()
    return by_id or by_name or ('unknown', (), ())


def classify_exercise(exercise_id: str | None, exercise_name: str | None):
    """Movement pattern and trained muscles, primary muscles first."""
    pattern, primary, secondary = _knowledge(exercise_id, exercise_name)
    return pattern, primary + secondary


def muscle_weights(exercise_id: str | None, exercise_name: str | None) -> dict[str, float]:
    """Per-muscle set equivalents, never fractions of physical load."""
    _, primary, secondary = _knowledge(exercise_id, exercise_name)
    return {**dict.fromkeys(secondary, SECONDARY_MUSCLE_WEIGHT), **dict.fromkeys(primary, 1.0)}


def reported_set_count(data: dict) -> int | None:
    value = data.get("vendor_reported_sets")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


_CAPACITY_UNITS = {"kg", "lb"}
# A bodyweight set records added load, not the effective body mass moved.
_CAPACITY_BASES = {"per_hand", "total", "machine"}
_LB_TO_KG = 0.45359237


def _capacity_mapping(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return {}


def _capacity_number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value)) else None


def _capacity_unit(value, weight_kg=None) -> str | None:
    if weight_kg is not None:
        return "kg"
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return {
        "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg", "公斤": "kg", "千克": "kg",
        "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb", "磅": "lb",
    }.get(normalized)
def estimate_one_rep_max(weight, repetitions, *, unit: str | None, basis: str | None, rir=None) -> float | None:
    from .strength_dose import one_rep_max
    return one_rep_max(weight, repetitions, unit=unit, basis=basis, rir=rir)


def strength_session_capacity(session: dict) -> dict:
    """Compute qualified accounting once in analysis."""
    from .strength_dose import session_capacity
    return session_capacity(_capacity_mapping(session), _knowledge, MUSCLE_LABELS)


def _apply_session_capacity(session: StrengthSessionAnalysis, *, target=None) -> StrengthSessionAnalysis:
    from .contracts import FocusTrainingDose, MuscleTrainingDose, StrengthDoseCoverage
    from .training_evaluation import review_strength
    summary = strength_session_capacity(session.model_dump(mode='python'))
    exercises = []
    for exercise in session.explicit_exercises:
        own = strength_session_capacity({'explicit_exercises': [exercise.model_dump(mode='python')]})['capacity']
        derived = own[0] if own else None
        estimate = derived.get('estimated_1rm') if derived else None
        exercises.append(exercise.model_copy(update={
            'canonical_qualified': _knowledge(exercise.exercise_id, exercise.exercise_name)[0] != 'unknown',
            'volume_value': derived['volume'] if derived else None,
            'volume_unit': 'kg*reps' if derived else None,
            'volume_basis': exercise.weight_basis if derived else None,
            'estimated_1rm_kg': estimate,
            'estimated_1rm_method': 'Epley-actual-reps-1' if estimate else None,
            'estimated_1rm_evidence': ['明确负重口径、实际次数及逐组余力；仅估算'] if estimate else [],
            'personal_record': False, 'personal_record_date': None,
        }))
    volumes = summary['volume_by_basis']
    updated = session.model_copy(update={
        'explicit_exercises': exercises,
        'capacity_volume_kg': next(iter(volumes.values())) if len(volumes) == 1 else None,
        'capacity_unit': 'kg*reps' if len(volumes) == 1 else None,
        'capacity_details': summary['capacity'], 'volume_by_basis': volumes,
        'dose_coverage': StrengthDoseCoverage(**summary['coverage']),
        'focus_doses': [FocusTrainingDose(**x) for x in summary['focus_doses']],
        'muscle_doses': [MuscleTrainingDose(**x) for x in summary['muscle_doses']],
        'reference_muscle_doses': [MuscleTrainingDose(**x) for x in summary['reference_muscle_doses']],
        'limitations': list(dict.fromkeys(session.limitations + summary['limitations'])),
    })
    return updated.model_copy(update={'evaluation': review_strength(updated, target=target)})


def _muscle_load_status(day: date, sessions: list[StrengthSessionAnalysis], *, complete=False) -> list[MuscleLoadStatus]:
    short, long, latest = defaultdict(float), defaultdict(float), {}
    for session in sessions:
        if not day - timedelta(days=27) <= session.date <= day:
            continue
        for dose in session.muscle_doses:
            long[dose.muscle_group] += dose.equivalent_sets
            latest[dose.muscle_group] = max(latest.get(dose.muscle_group, session.date), session.date)
            if session.date >= day - timedelta(days=6):
                short[dose.muscle_group] += dose.equivalent_sets
    output = []
    for muscle, count in sorted(long.items()):
        ratio = short.get(muscle, 0) / (count / 4) if complete and count > 0 else None
        state = 'INSUFFICIENT_DATA' if ratio is None else 'INCREASED' if ratio > 1.3 else 'DECREASED' if ratio < .8 else 'SIMILAR'
        output.append(MuscleLoadStatus(
            muscle_group=muscle, muscle_group_label=MUSCLE_LABELS[muscle],
            short_load=short.get(muscle) if short.get(muscle) is not None or complete else None,
            long_load=count / 4, short_long_ratio=round(ratio, 3) if ratio is not None else None,
            status=state, status_label={'INCREASED':'相对近期增加', 'DECREASED':'相对近期减少', 'SIMILAR':'与近期相近', 'INSUFFICIENT_DATA':'覆盖不足'}[state],
            qualified=complete, freshness_days=(day-latest[muscle]).days,
            limitations=[] if complete else ['窗口或工作组资格不足，仅保留已记录训练接触。'],
        ))
    return output


class StrengthAnalyzer:
    def analyze(self, raw) -> StrengthAnalysis:
        from .strength_dose import aggregate_doses
        from .strength_recommendations import muscle_history, recommend_focus
        start = raw.day - timedelta(days=179)
        workouts = [
            item for item in RunningAnalyzer._available_workouts(raw)
            if start <= self._date(item) <= raw.day
            and str((item.get("data") or {}).get("training_family") or "") == "strength"
        ]
        if not workouts:
            return StrengthAnalysis(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                sessions_7d=0,
                sessions_28d=0,
                explicit_session_coverage=0,
                limitations=["近 180 天没有力量训练记录。"],
            )
        threshold = RunningAnalyzer._lactate_threshold(raw)
        sessions = [
            self._session(raw, workout, threshold)
            for workout in sorted(workouts, key=RunningAnalyzer._workout_sort_key)
        ]
        qualified_sessions = []
        targets = {(w.get('source') or 'zepp', w.get('workout_id')): w.get('training_target') for w in workouts}
        for index, session in enumerate(sessions):
            session = _apply_session_capacity(session, target=targets.get((session.source, session.workout_id)))
            session = session.model_copy(update={
                "comparisons": self._comparisons_for_session(session, sessions[:index]),
            })
            qualified_sessions.append(session)
        sessions = qualified_sessions
        history_sessions = sessions
        recommendation = recommend_focus(raw, history_sessions)
        sessions = [s for s in history_sessions if s.date >= raw.day - timedelta(days=27)]
        muscle_load = _muscle_load_status(raw.day, sessions)
        explicit_count = sum(bool(item.explicit_exercises) for item in sessions)
        split, split_confidence = self._split(sessions)
        next_focus = recommendation.candidates[0] if len(recommendation.candidates) == 1 else None
        limitations = ["力量训练心率区间仅表示心肺负担，不代表负重强度。"]
        if explicit_count < len(sessions):
            limitations.append("肌群训练量包含已记录动作及 Zepp 动作映射估算，无需逐项确认才能统计。")
        if split == "UNRESOLVED":
            limitations.append("动作覆盖不足，尚不能判断三分化、五分化或其他训练结构。")
        return StrengthAnalysis(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            sessions_7d=sum(self._date(item) >= raw.day - timedelta(days=6) for item in workouts),
            sessions_28d=len(sessions),
            history_sessions=len(history_sessions),
            history_start=start,
            focus_history=recommendation.history,
            muscle_history=muscle_history(raw.day, history_sessions),
            next_focus_candidates=recommendation.candidates,
            explicit_session_coverage=round(explicit_count / len(sessions) if sessions else 0, 3),
            detected_split=split,
            detected_split_label=SPLIT_LABELS[split],
            split_confidence=split_confidence,
            split_confidence_label=CONFIDENCE_LABELS[split_confidence.value],
            next_focus=next_focus,
            next_focus_label=next((h.label for h in recommendation.history if h.focus == next_focus), None),
            alternative_focus=recommendation.alternative,
            next_focus_reasons=recommendation.reasons,
            recommendation_status=recommendation.status,
            muscle_doses_7d=aggregate_doses([s for s in sessions if s.date >= raw.day - timedelta(days=6)], 'muscle_doses'),
            reference_muscle_doses_7d=aggregate_doses([s for s in sessions if s.date >= raw.day - timedelta(days=6)], 'reference_muscle_doses'),
            recent_sessions=list(reversed(sessions[-8:])),
            muscle_recovery=self._muscle_recovery(raw.day, sessions),
            muscle_load=muscle_load,
            limitations=limitations,
        )

    def trend_points(self, raw) -> list[dict]:
        """Emit session doses only for explicit, source-qualified actions.

        Ordered observations and user confirmations retain their existing
        precedence. Lap-code observations remain display facts. Volume uses
        sum(repetitions * observed weight) with a known unit and weight basis;
        per-hand, total and machine values are never pooled or multiplied by
        an inferred number of hands.
        """
        output = []
        workouts = [
            item for item in RunningAnalyzer._available_workouts(raw)
            if str((item.get("data") or {}).get("training_family") or "") == "strength"
            and raw.day - timedelta(days=179) <= self._date(item) <= raw.day
        ]
        for workout in sorted(workouts, key=RunningAnalyzer._workout_sort_key):
            session = self._session(raw, workout, None)
            confirmed = any(item.source == "user_confirmed" for item in session.explicit_exercises)
            source_scope = "user_confirmed" if confirmed else "strength_sets"
            for dose in self._session_doses(session):
                repetitions_complete = bool(dose.repetitions) and all(value is not None for value in dose.repetitions)
                weights_complete = bool(dose.weights) and all(value is not None for value in dose.weights)
                volume = (
                    float(sum(reps * weight for reps, weight in zip(dose.repetitions, dose.weights)))
                    if dose.set_count is not None and repetitions_complete and weights_complete
                    and dose.weight_unit is not None and dose.weight_basis in _CAPACITY_BASES
                    else None
                )
                common = {
                    "source": session.source, "source_scope": source_scope,
                    "device_id": workout.get("device_id"), "workout_id": session.workout_id,
                    "day": session.date,
                    "observed_at": workout.get("started_at") or session.date,
                    "qualification": f"explicit_action:{dose.identity[0]}:{dose.identity[1]}",
                    "exercise_id": dose.exercise_id or dose.exercise_name,
                }
                for metric, unit, value, basis in (
                    ("strength_sets", "sets", float(dose.set_count) if dose.set_count is not None else None, None),
                    ("strength_repetitions", "reps", float(sum(dose.repetitions)) if repetitions_complete and dose.set_count is not None else None, None),
                    ("strength_volume", f"{dose.weight_unit or 'unknown'}*reps", volume, dose.weight_basis),
                ):
                    output.append({**common, "metric": metric, "unit": unit, "value": value, "weight_basis": basis})
        return output

    def _session(self, raw, workout: dict, threshold: float | None) -> StrengthSessionAnalysis:
        confirmed = list(workout.get("confirmed_exercises") or [])
        observed_sets = self._observed_sets(workout)
        explicit = (
            self._merge_exercises(confirmed)
            if confirmed
            else self._vendor_exercises(
                raw.user_id, workout, as_of=getattr(raw, "as_of", None)
            )
        )
        patterns = sorted({item.movement_pattern for item in explicit if item.movement_pattern != "unknown"})
        muscles = sorted({muscle for item in explicit for muscle in item.muscle_groups})
        confirmed_focuses = {
            item.session_focus for item in explicit if item.session_focus
        }
        focus = (
            next(iter(confirmed_focuses))
            if len(confirmed_focuses) == 1
            else self._focus(patterns, muscles)
        )
        confidence = ConfidenceBand.HIGH if explicit and focus != "UNKNOWN" else ConfidenceBand.NONE
        heart_rate = [
            item for item in (workout.get("samples") or []) if item.metric == "heart_rate"
        ]
        work_bouts, work_seconds, rest_seconds = self._work_rest(workout, heart_rate)
        hypotheses = []
        if not explicit and work_bouts:
            hypotheses.append(ExerciseHypothesis(
                confidence=ConfidenceBand.LOW,
                confidence_label=CONFIDENCE_LABELS[ConfidenceBand.LOW.value],
                source="heart_rate_structure",
                estimated_work_bouts=work_bouts,
                evidence=[
                    f"心率或圈段结构中识别到约 {work_bouts} 个工作段。",
                    "心率不能区分卧推、深蹲或其他具体动作。",
                ],
            ))
        workout_id = str(workout.get("workout_id") or "")
        feedback = sorted(
            [item for item in raw.feedback_by_workout.get(
                (str(workout.get("source") or "zepp"), workout_id),
                raw.feedback_by_workout.get(workout_id, []),
            ) if not isinstance(getattr(raw, 'as_of', None), datetime) or item.created_at.replace(tzinfo=None) <= raw.as_of.replace(tzinfo=None)],
            key=lambda item: item.created_at,
        )
        latest = feedback[-1] if feedback else None
        effort = workout_rpe(raw, workout)
        total_sets = sum(item.sets or 0 for item in explicit) if any(item.sets for item in explicit) else None
        limitations = []
        if not explicit:
            if not observed_sets:
                limitations.append("缺少逐组动作明细，仅保留已记录训练摘要。")
            mapped_observed = any(
                item.source == "lap_62"
                and item.exercise_name
                and "exercise_name_reference_mapping" in item.limitations
                for item in observed_sets
            )
            unverified_observed = any(
                item.source == "lap_62"
                and not item.exercise_name
                and "exercise_name_unverified" in item.limitations
                for item in observed_sets
            )
            if mapped_observed:
                limitations.append("设备动作名称来自已核验编码对照，肌群组数为估算。")
            if unverified_observed:
                limitations.append("观测组动作名称未确认；仅作为本次事实展示，不用于处方。")
            if not mapped_observed and not unverified_observed and observed_sets:
                limitations.append("观测组动作名称未确认；仅作为本次事实展示，不用于处方。")
            elif not observed_sets and workout.get("detail_available"):
                limitations.append("当前云详情未返回明确动作组；App 中的修正内容尚未在已读取字段中取得。")
        elif focus == "UNKNOWN":
            limitations.append("已记录动作但训练重点未识别；分化未知，不套用全身动作模板。")
        if threshold is None:
            limitations.append("缺少个人乳酸阈心率，未展示本次心率区间。")
        if work_bouts is None:
            limitations.append("心率或圈段结构不足，未估计工作段和休息段。")
        return StrengthSessionAnalysis(
            vendor_reported_sets=reported_set_count(workout.get("data") or {}),
            workout_id=str(workout.get("workout_id") or ""),
            source=str(workout.get("source") or "zepp"),
            date=self._date(workout),
            duration_minutes=max(int((workout.get("data") or {}).get("duration") or 0), 0),
            focus=focus,
            focus_label=FOCUS_LABELS[focus],
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            explicit_exercises=explicit,
            observed_sets=observed_sets,
            started_at=workout.get("started_at"),
            fetched_at=_parse_datetime(workout.get('fetched_at') or (workout.get('data') or {}).get('fetched_at')),
            hypotheses=hypotheses,
            movement_patterns=patterns,
            movement_pattern_labels=[PATTERN_LABELS[item] for item in patterns],
            muscle_groups=muscles,
            muscle_group_labels=[MUSCLE_LABELS[item] for item in muscles],
            total_sets=total_sets,
            estimated_work_bouts=work_bouts,
            median_work_seconds=work_seconds,
            median_rest_seconds=rest_seconds,
            average_heart_rate_bpm=(
                round(sum(item.value for item in heart_rate) / len(heart_rate), 1)
                if heart_rate else self._positive((workout.get("data") or {}).get("heart_rate_avg"))
            ),
            maximum_heart_rate_bpm=(
                max(item.value for item in heart_rate)
                if heart_rate else self._positive((workout.get("data") or {}).get("heart_rate_max"))
            ),
            heart_rate_zones=RunningAnalyzer._zones(heart_rate, threshold),
            session_rpe=effort.value if effort else None,
            muscle_soreness=latest.muscle_soreness if latest else None,
            limitations=limitations,
        )

    @staticmethod
    def _observed_sets(workout: dict) -> list[StrengthSetObservation]:
        detail = workout.get("detail") or {}
        if isinstance(detail, dict):
            items = detail.get("strength_sets") or []
        else:
            items = getattr(detail, "strength_sets", []) or []
        output = []
        for item in items:
            if isinstance(item, StrengthSetObservation):
                output.append(item)
                continue
            if not isinstance(item, dict):
                try:
                    item = item.model_dump(mode="python")
                except AttributeError:
                    continue
            candidate = dict(item)
            # Older raw payloads used -1 as an absent weight marker.
            for key in ("weight_value", "weight_kg"):
                value = candidate.get(key)
                if isinstance(value, (int, float)) and value < 0:
                    candidate[key] = None
            try:
                output.append(StrengthSetObservation.model_validate(candidate))
            except (TypeError, ValueError):
                continue
        return output

    @staticmethod
    def _vendor_exercises(
        user_id: str,
        workout: dict,
        *,
        as_of: datetime | None = None,
    ) -> list[StrengthExerciseRecord]:
        detail = workout.get("detail") or {}
        items = detail.get("strength_sets") or [] if isinstance(detail, dict) else getattr(detail, "strength_sets", []) or []
        output = []
        workout_source = str(workout.get("source") or "zepp")
        workout_id = str(workout.get("workout_id") or "")
        source_time = workout.get("started_at")
        if not isinstance(source_time, datetime):
            source_time = as_of if isinstance(as_of, datetime) else None
        for order, item in enumerate(items, start=1):
            value = item.get if isinstance(item, dict) else getattr(item, "__dict__", {}).get
            if value("source") == "lap_62":
                continue
            name = value("exercise_name")
            exercise_id = value("exercise_id")
            if not name and not exercise_id:
                continue
            pattern, muscles = classify_exercise(exercise_id, name)
            confidence = ConfidenceBand.HIGH if pattern != "unknown" else ConfidenceBand.MODERATE
            stable_id = sha256(
                f"{user_id}|{workout_source}|{workout_id}|vendor|{order}".encode()
            ).hexdigest()[:32]
            repetitions = value("repetitions")
            weight_kg = _capacity_number(value("weight_kg"))
            weight_value = _capacity_number(value("weight_value"))
            if weight_kg is not None and weight_kg < 0:
                weight_kg = None
            if weight_value is not None and weight_value < 0:
                weight_value = None
            weight_unit = value("weight_unit")
            normalized_unit = _capacity_unit(weight_unit, weight_kg)
            if weight_kg is None and weight_value is not None and normalized_unit in _CAPACITY_UNITS:
                weight_kg = weight_value if normalized_unit == "kg" else weight_value * _LB_TO_KG
            if weight_kg is not None:
                weight_unit = "kg"
            rest_seconds = value("rest_seconds")
            output.append(StrengthExerciseRecord(
                id=stable_id,
                user_id=user_id,
                workout_source=workout_source,
                workout_id=workout_id,
                order=order,
                exercise_name=str(name or exercise_id),
                exercise_id=exercise_id,
                session_focus=None,
                movement_pattern=pattern,
                movement_pattern_label=PATTERN_LABELS[pattern],
                muscle_groups=list(muscles),
                muscle_group_labels=[MUSCLE_LABELS[value] for value in muscles],
                sets=1,
                repetitions=repetitions,
                weight_kg=float(weight_kg) if weight_kg is not None else None,
                weight_unit=weight_unit,
                weight_basis=value("weight_basis"),
                rest_seconds=int(rest_seconds) if rest_seconds is not None else None,
                source="vendor_explicit",
                confidence=confidence,
                confidence_label=CONFIDENCE_LABELS[confidence.value],
                created_at=source_time,
            ))
        return StrengthAnalyzer._merge_exercises(output)

    @staticmethod
    def _merge_exercises(exercises: list[StrengthExerciseRecord]) -> list[StrengthExerciseRecord]:
        """Combine only consecutive identical doses; retain A/B/A ordering."""
        merged: list[StrengthExerciseRecord] = []
        previous_dose = None
        for exercise in sorted(exercises, key=lambda item: item.order):
            identity = (exercise.exercise_id or exercise.exercise_name).strip().lower()
            dose = (
                identity,
                exercise.repetitions,
                exercise.weight_kg,
                exercise.weight_unit,
                exercise.weight_basis,
                exercise.rest_seconds,
                exercise.rpe,
                exercise.rir,
            )
            if previous_dose != dose:
                merged.append(exercise)
                previous_dose = dose
                continue
            previous = merged[-1]
            merged[-1] = previous.model_copy(update={
                "sets": (previous.sets + exercise.sets) if previous.sets is not None and exercise.sets is not None else None,
            })
        return merged

    @staticmethod
    def _known_weight_unit(value: str | None, weight_kg: float | None = None) -> str | None:
        if weight_kg is not None:
            return "kg"
        if not value or not str(value).strip():
            return None
        normalized = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        return {
            "kg": "kg",
            "kgs": "kg",
            "kilogram": "kg",
            "kilograms": "kg",
            "公斤": "kg",
            "千克": "kg",
            "lb": "lb",
            "lbs": "lb",
            "pound": "lb",
            "pounds": "lb",
            "磅": "lb",
        }.get(normalized)

    @staticmethod
    def _exercise_identity(
        exercise_id: str | None, exercise_name: str | None
    ) -> tuple[str, str] | None:
        if exercise_id and str(exercise_id).strip():
            return "id", str(exercise_id).strip().casefold()
        if exercise_name and str(exercise_name).strip():
            return "name", str(exercise_name).strip().casefold()
        return None

    @classmethod
    def _session_doses(cls, session: StrengthSessionAnalysis) -> list[_StrengthDose]:
        """Compare each action's full ordered dose once per workout."""
        confirmed = [item for item in session.explicit_exercises if item.source == "user_confirmed"]
        observed = [item for item in session.observed_sets if item.source == "strength_sets"]
        records = sorted(confirmed, key=lambda item: item.order) if confirmed else observed or sorted(session.explicit_exercises, key=lambda item: item.order)
        groups: dict[tuple[str, str], dict] = {}
        for item in records:
            identity = cls._exercise_identity(item.exercise_id, item.exercise_name)
            if identity is None:
                continue
            is_observation = isinstance(item, StrengthSetObservation)
            count = 1 if is_observation else item.sets
            repetitions = [item.repetitions] * (count if count is not None else 1)
            weight = item.weight_kg if item.weight_kg is not None else getattr(item, "weight_value", None)
            unit = cls._known_weight_unit(item.weight_unit, item.weight_kg)
            group = groups.setdefault(identity, {
                "identity": identity, "exercise_id": item.exercise_id,
                "exercise_name": item.exercise_name or item.exercise_id or "",
                "repetitions": [], "weights": [], "set_count": 0,
                "weight_unit": unit, "weight_basis": item.weight_basis,
            })
            group["repetitions"].extend(repetitions)
            group["weights"].extend([weight] * len(repetitions))
            group["set_count"] = group["set_count"] + count if group["set_count"] is not None and count is not None else None
            if group["weight_unit"] != unit:
                group["weight_unit"] = None
            if group["weight_basis"] != item.weight_basis:
                group["weight_basis"] = None
        return [_StrengthDose(**group) for group in groups.values()]

    @classmethod
    def _comparisons_for_session(
        cls,
        session: StrengthSessionAnalysis,
        previous_sessions: list[StrengthSessionAnalysis],
    ) -> list[StrengthExerciseComparison]:
        if not previous_sessions:
            return []
        comparisons = []
        for current in cls._session_doses(session):
            reference_session = None
            reference = None
            for candidate_session in reversed(previous_sessions):
                candidates = cls._session_doses(candidate_session)
                matches = [item for item in candidates if item.identity == current.identity]
                if matches:
                    reference_session = candidate_session
                    reference = matches[-1]
                    break

            common = {
                "exercise_id": current.exercise_id,
                "exercise_name": current.exercise_name,
                "reference_workout_source": reference_session.source if reference_session else None,
                "reference_workout_id": reference_session.workout_id if reference_session else None,
                "reference_workout_date": reference_session.date if reference_session else None,
                "current_repetitions": list(current.repetitions),
                "previous_repetitions": list(reference.repetitions) if reference else [],
                "current_weights": list(current.weights),
                "previous_weights": list(reference.weights) if reference else [],
                "weight_unit": (
                    current.weight_unit
                    if reference is not None and current.weight_unit == reference.weight_unit
                    else None
                ),
                "weight_basis": (
                    current.weight_basis
                    if reference is not None and current.weight_basis == reference.weight_basis
                    else None
                ),
                "set_count": current.set_count,
                "comparable": False,
                "blocked_reason": None,
                "delta_total_repetitions": None,
            }
            if reference is None:
                common["blocked_reason"] = "no_reference_session"
            elif session.source != reference_session.source:
                common["blocked_reason"] = "source_mismatch"
            elif current.set_count is None or reference.set_count is None:
                common["blocked_reason"] = "set_count_unknown"
            elif current.set_count != reference.set_count:
                common["blocked_reason"] = "set_count_mismatch"
            elif (
                len(current.repetitions) != len(reference.repetitions)
                or any(value is None for value in current.repetitions)
                or any(value is None for value in reference.repetitions)
            ):
                common["blocked_reason"] = "repetitions_incomplete"
            elif not current.weight_unit or not reference.weight_unit:
                common["blocked_reason"] = "weight_unit_unknown"
            elif current.weight_unit != reference.weight_unit:
                common["blocked_reason"] = "weight_unit_mismatch"
            elif not current.weight_basis or not reference.weight_basis:
                common["blocked_reason"] = "weight_basis_unknown"
            elif current.weight_basis != reference.weight_basis:
                common["blocked_reason"] = "weight_basis_mismatch"
            elif (
                len(current.weights) != len(reference.weights)
                or any(value is None for value in current.weights)
                or any(value is None for value in reference.weights)
            ):
                common["blocked_reason"] = "weight_incomplete"
            elif current.weights != reference.weights:
                common["blocked_reason"] = "weight_mismatch"
            else:
                common["comparable"] = True
                common["delta_total_repetitions"] = (
                    sum(current.repetitions) - sum(reference.repetitions)
                )
            comparisons.append(StrengthExerciseComparison(**common))
        return comparisons

    def _work_rest(self, workout: dict, heart_rate: list):
        detail = workout.get("detail") or {}
        lap_durations = [
            int(item.get("duration_seconds") or 0)
            for item in detail.get("laps") or []
            if float(item.get("distance_meters") or 0) == 0
            and 10 <= int(item.get("duration_seconds") or 0) <= 300
        ]
        if len(lap_durations) >= 2:
            return len(lap_durations), round(median(lap_durations), 1), None
        bins = RunningAnalyzer._bins(heart_rate, 15)
        if len(bins) < 8:
            return None, None, None
        values = sorted(bins.values())
        low = RunningAnalyzer._percentile(values, 0.30)
        high = RunningAnalyzer._percentile(values, 0.70)
        if high - low < 8:
            return None, None, None
        labels = {
            key: "work" if value >= high else "rest" if value <= low else "transition"
            for key, value in bins.items()
        }
        groups: list[tuple[str, int]] = []
        for key in sorted(labels):
            label = labels[key]
            if label == "transition":
                continue
            if groups and groups[-1][0] == label:
                groups[-1] = (label, groups[-1][1] + 15)
            else:
                groups.append((label, 15))
        work = [duration for label, duration in groups if label == "work" and 15 <= duration <= 180]
        rest = [duration for label, duration in groups if label == "rest" and 15 <= duration <= 600]
        return (
            len(work) or None,
            round(median(work), 1) if work else None,
            round(median(rest), 1) if rest else None,
        )

    @staticmethod
    def _focus(patterns: list[str], muscles: list[str]) -> str:
        upper = {"horizontal_push", "horizontal_pull", "vertical_push", "vertical_pull"}
        lower = {"squat", "hinge", "unilateral_leg"}
        present = set(patterns)
        if present & upper and present & lower:
            return "FULL_BODY"
        if present and present <= lower | {"core"} and present & lower:
            return "LEGS"
        if present and present <= {"horizontal_push", "vertical_push", "isolation"}:
            return "PUSH"
        if present and present <= {"horizontal_pull", "vertical_pull", "isolation"}:
            return "PULL"
        if present and present <= upper | {"core", "isolation"}:
            return "UPPER"
        muscle_set = set(muscles)
        if muscle_set and muscle_set <= {"chest", "triceps"}:
            return "CHEST"
        if muscle_set and muscle_set <= {"back", "biceps"}:
            return "BACK"
        if muscle_set and muscle_set <= {"shoulders"}:
            return "SHOULDERS"
        if muscle_set and muscle_set <= {"biceps", "triceps"}:
            return "ARMS"
        return "UNKNOWN"

    @staticmethod
    def _split(sessions: list[StrengthSessionAnalysis]):
        known = [item.focus for item in sessions if item.focus != "UNKNOWN"]
        values = set(known[-8:])
        if {"CHEST", "BACK", "LEGS", "SHOULDERS", "ARMS"} <= values:
            return "FIVE_DAY", ConfidenceBand.HIGH
        if {"PUSH", "PULL", "LEGS"} <= values:
            return "PUSH_PULL_LEGS", ConfidenceBand.HIGH
        if {"UPPER", "LOWER"} <= values or {"UPPER", "LEGS"} <= values:
            return "UPPER_LOWER", ConfidenceBand.MODERATE
        if known.count("FULL_BODY") >= 2:
            return "FULL_BODY", ConfidenceBand.MODERATE
        return "UNRESOLVED", ConfidenceBand.NONE

    @staticmethod
    def _muscle_recovery(day: date, sessions: list[StrengthSessionAnalysis]):
        latest = {}
        for session in sessions:
            for muscle in session.muscle_groups:
                latest[muscle] = session
        return [
            MuscleRecoveryStatus(
                muscle_group=muscle,
                muscle_group_label=MUSCLE_LABELS[muscle],
                days_since_last_trained=(day - session.date).days,
                last_session_rpe=session.session_rpe,
                latest_soreness=session.muscle_soreness,
            )
            for muscle, session in sorted(latest.items())
        ]

    @staticmethod
    def _date(workout: dict) -> date:
        return workout.get("local_day") or date.min

    @staticmethod
    def _positive(value) -> float | None:
        return float(value) if isinstance(value, (int, float)) and value > 0 else None
