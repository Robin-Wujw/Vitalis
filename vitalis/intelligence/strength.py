"""Strength knowledge, explicit exercise normalization, and session analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from hashlib import sha256
from statistics import median
from uuid import uuid4

from .contracts import (
    Availability,
    ConfidenceBand,
    ExerciseHypothesis,
    MuscleRecoveryStatus,
    StrengthAnalysis,
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


def normalize_exercise(
    user_id: str,
    workout_id: str,
    order: int,
    value: StrengthExerciseInput,
    session_focus: str | None = None,
    workout_source: str = "zepp",
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
        rpe=value.rpe,
        rir=value.rir,
        rest_seconds=value.rest_seconds,
        source="user_confirmed",
        confidence=ConfidenceBand.HIGH,
        confidence_label=CONFIDENCE_LABELS[ConfidenceBand.HIGH.value],
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


class StrengthAnalyzer:
    def analyze(self, raw) -> StrengthAnalysis:
        start = raw.day - timedelta(days=27)
        workouts = [
            item for item in raw.workouts
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
            for workout in sorted(workouts, key=self._date)
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

    def _session(self, raw, workout: dict, threshold: float | None) -> StrengthSessionAnalysis:
        confirmed = list(workout.get("confirmed_exercises") or [])
        explicit = (
            self._merge_exercises(confirmed)
            if confirmed
            else self._vendor_exercises(raw.user_id, workout)
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
            if workout.get("detail_available"):
                limitations.append("当前云详情未返回明确动作组；App 中的修正内容尚未在已读取字段中取得。")
        elif focus == "UNKNOWN":
            limitations.append("已记录动作但训练重点未识别；分化未知，不套用全身动作模板。")
        if threshold is None:
            limitations.append("缺少个人乳酸阈心率，未展示本次心率区间。")
        if work_bouts is None:
            limitations.append("心率或圈段结构不足，未估计工作段和休息段。")
        return StrengthSessionAnalysis(
            workout_id=str(workout.get("workout_id") or ""),
            source=str(workout.get("source") or "zepp"),
            date=self._date(workout),
            duration_minutes=max(int((workout.get("data") or {}).get("duration") or 0), 0),
            focus=focus,
            focus_label=FOCUS_LABELS[focus],
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            explicit_exercises=explicit,
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
    def _vendor_exercises(user_id: str, workout: dict) -> list[StrengthExerciseRecord]:
        detail = workout.get("detail") or {}
        items = detail.get("strength_sets") or [] if isinstance(detail, dict) else []
        output = []
        workout_source = str(workout.get("source") or "zepp")
        workout_id = str(workout.get("workout_id") or "")
        for order, item in enumerate(items, start=1):
            value = item.get if isinstance(item, dict) else getattr(item, "__dict__", {}).get
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
                repetitions=str(repetitions) if repetitions is not None else None,
                weight_kg=float(weight_kg) if weight_kg is not None else None,
                rest_seconds=int(rest_seconds) if rest_seconds is not None else None,
                source="vendor_explicit",
                confidence=confidence,
                confidence_label=CONFIDENCE_LABELS[confidence.value],
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
