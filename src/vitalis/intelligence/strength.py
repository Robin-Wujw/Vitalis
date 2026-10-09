"""Strength knowledge, explicit exercise normalization, and session analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from statistics import median
from uuid import uuid4

from vitalis.domain import StrengthSetObservation

from .contracts import (
    Availability,
    ConfidenceBand,
    ExerciseHypothesis,
    MuscleRecoveryStatus,
    StrengthAnalysis,
    StrengthExerciseComparison,
    StrengthExerciseInput,
    StrengthExerciseRecord,
    StrengthSessionAnalysis,
)
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .running import RunningAnalyzer


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


EXERCISE_KNOWLEDGE = (
    (("benchpress", "bench_press", "卧推", "哑铃卧推", "俯卧撑", "pushup"), "horizontal_push", ("chest", "triceps", "shoulders")),
    (("row", "划船"), "horizontal_pull", ("back", "biceps")),
    (("pullup", "pull_up", "引体向上", "下拉", "latpulldown"), "vertical_pull", ("back", "biceps")),
    (("overheadpress", "shoulderpress", "推举", "肩推"), "vertical_push", ("shoulders", "triceps")),
    (("deadlift", "硬拉", "罗马尼亚硬拉", "rdl", "hipthrust", "臀推"), "hinge", ("glutes", "hamstrings", "back")),
    (("squat", "深蹲", "腿举", "legpress"), "squat", ("quadriceps", "glutes")),
    (("lunge", "弓步", "分腿蹲", "split squat"), "unilateral_leg", ("quadriceps", "glutes")),
    (("plank", "平板支撑", "deadbug", "死虫", "卷腹"), "core", ("core",)),
    (("carry", "农夫行走"), "carry", ("core", "shoulders")),
    (("curl", "弯举"), "isolation", ("biceps",)),
    (("tricep", "臂屈伸", "下压"), "isolation", ("triceps",)),
    (("calf", "提踵"), "isolation", ("calves",)),
    (("lateralraise", "侧平举"), "isolation", ("shoulders",)),
)


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


def classify_exercise(exercise_id: str | None, exercise_name: str | None):
    text = "".join(
        character for character in f"{exercise_id or ''}{exercise_name or ''}".lower()
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )
    for aliases, pattern, muscles in EXERCISE_KNOWLEDGE:
        if any(alias.replace(" ", "").replace("_", "").lower() in text for alias in aliases):
            return pattern, muscles
    return "unknown", ()


def reported_set_count(data: dict) -> int | None:
    value = data.get("vendor_reported_sets")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class StrengthAnalyzer:
    def analyze(self, raw) -> StrengthAnalysis:
        start = raw.day - timedelta(days=27)
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
                limitations=["近 28 天没有力量训练记录。"],
            )
        threshold = RunningAnalyzer._lactate_threshold(raw)
        sessions = [
            self._session(raw, workout, threshold)
            for workout in sorted(workouts, key=RunningAnalyzer._workout_sort_key)
        ]
        sessions = [
            session.model_copy(update={
                "comparisons": self._comparisons_for_session(session, sessions[:index]),
            })
            for index, session in enumerate(sessions)
        ]
        explicit_count = sum(bool(item.explicit_exercises) for item in sessions)
        split, split_confidence = self._split(sessions)
        next_focus = self._next_focus(split, sessions)
        limitations = ["力量训练心率区间仅表示心肺负担，不代表负重强度。"]
        if explicit_count < len(sessions):
            limitations.append("部分力量训练缺少已确认动作，不能完整计算肌群训练量。")
        if split == "UNRESOLVED":
            limitations.append("动作覆盖不足，尚不能判断三分化、五分化或其他训练结构。")
        return StrengthAnalysis(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            sessions_7d=sum(self._date(item) >= raw.day - timedelta(days=6) for item in workouts),
            sessions_28d=len(workouts),
            explicit_session_coverage=round(explicit_count / len(sessions), 3),
            detected_split=split,
            detected_split_label=SPLIT_LABELS[split],
            split_confidence=split_confidence,
            split_confidence_label=CONFIDENCE_LABELS[split_confidence.value],
            next_focus=next_focus,
            next_focus_label=FOCUS_LABELS.get(next_focus) if next_focus else None,
            recent_sessions=list(reversed(sessions[-8:])),
            muscle_recovery=self._muscle_recovery(raw.day, sessions),
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
                    and dose.weight_unit is not None and dose.weight_basis is not None
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
            raw.feedback_by_workout.get(
                (str(workout.get("source") or "zepp"), workout_id),
                raw.feedback_by_workout.get(workout_id, []),
            ),
            key=lambda item: item.created_at,
        )
        latest = feedback[-1] if feedback else None
        total_sets = sum(item.sets or 0 for item in explicit) if any(item.sets for item in explicit) else None
        limitations = []
        if not explicit:
            limitations.append("没有已确认动作，未推测具体动作或目标肌群。")
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
                limitations.append("观测动作名称中的已映射名称来自已核验编码对照，仅用于展示，不据此处方。")
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
            session_rpe=latest.session_rpe if latest else None,
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
                f"{user_id}|{workout_source}|{workout_id}|vendor|{order}".encode("utf-8")
            ).hexdigest()[:32]
            repetitions = value("repetitions")
            weight_kg = value("weight_kg")
            weight_value = value("weight_value")
            if isinstance(weight_kg, (int, float)) and weight_kg < 0:
                weight_kg = None
            if isinstance(weight_value, (int, float)) and weight_value < 0:
                weight_value = None
            weight_unit = value("weight_unit")
            if weight_kg is None and weight_value is not None and str(weight_unit or "").lower() == "kg":
                weight_kg = weight_value
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
                weight_unit=weight_unit or ("kg" if weight_kg is not None else None),
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
        """Combine identical vendor dose rows while preserving distinct doses."""
        merged: dict[tuple, StrengthExerciseRecord] = {}
        for exercise in exercises:
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
            previous = merged.get(dose)
            if previous is None:
                merged[dose] = exercise
                continue
            merged[dose] = previous.model_copy(update={
                "sets": (previous.sets or 0) + (exercise.sets or 0),
            })
        return sorted(merged.values(), key=lambda item: item.order)

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
    def _next_focus(split: str, sessions: list[StrengthSessionAnalysis]) -> str | None:
        known = [item.focus for item in sessions if item.focus != "UNKNOWN"]
        if not known:
            return None
        rotations = {
            "PUSH_PULL_LEGS": ["PUSH", "PULL", "LEGS"],
            "UPPER_LOWER": ["UPPER", "LOWER"],
            "FIVE_DAY": ["CHEST", "BACK", "LEGS", "SHOULDERS", "ARMS"],
            "FULL_BODY": ["FULL_BODY"],
        }
        rotation = rotations.get(split)
        if not rotation:
            return None
        last = known[-1]
        return rotation[(rotation.index(last) + 1) % len(rotation)] if last in rotation else rotation[0]

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
