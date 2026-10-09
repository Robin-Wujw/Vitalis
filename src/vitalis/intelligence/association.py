"""Registered, observational personal associations with explicit date pairing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from statistics import median

from .association_statistics import (
    DEFAULT_BLOCK_DAYS,
    DEFAULT_PERMUTATIONS,
    benjamini_hochberg,
    block_permutation_p_value,
    spearman_coefficient,
    stable_seed,
)
from .contracts import Availability, ConfidenceBand, PersonalAssociation, PersonalAssociationProfile
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS
from .profile import RawDailyProfile
from .trend import METRIC_LABELS, stream_daily_values


WINDOWS = (60, 90)
MINIMUM_PAIRED_DAYS = {60: 30, 90: 45}
MINIMUM_COVERAGE = 0.5
FAMILY_ID = "personal_association_v2_60_90_all_streams"


@dataclass(frozen=True)
class _Hypothesis:
    id: str
    predictor: str
    outcome: str
    predictor_day: str
    outcome_day: str
    lag: int
    pairing: str
    rationale: str


# Four domain questions, with explicitly registered measurement variants.
# The variants and lags are fixed before observing coefficients or p-values.
HYPOTHESES = (
    *(
        _Hypothesis(
            f"sleep_to_next_calendar_{metric}", "sleep_duration", metric,
            "sleep_day", "calendar_day", 1,
            "sleep_day_d_to_calendar_day_d_plus_1",
            "保留既有睡眠日 d 与下一日历日 HRV 汇总的假设；日内 HRV 不冒充同夜睡眠测量，不搜索其它滞后。",
        )
        for metric in ("hrv_rmssd", "hrv_sdnn")
    ),
    _Hypothesis(
        "sleep_to_next_calendar_rhr", "sleep_duration", "resting_hr",
        "sleep_day", "calendar_day", 1,
        "sleep_day_d_to_calendar_day_d_plus_1",
        "保留既有睡眠日 d 与下一日历日静息心率汇总的假设；两者不是同夜观测。",
    ),
    *(
        _Hypothesis(
            f"same_sleep_{metric}", "sleep_duration", metric,
            "sleep_day", "sleep_day", 0, "same_sleep_day",
            "睡眠时长和睡眠 HRV/RHR 都按醒来日期归属，比较同一夜的汇总；共同来源和共同睡眠背景仍可能混杂。",
        )
        for metric in ("sleep_hrv", "sleep_rhr")
    ),
    *(
        _Hypothesis(
            f"training_to_next_{metric}", "training_load", metric,
            "activity_day", "calendar_day", 1,
            "activity_day_d_to_calendar_day_d_plus_1",
            "比较训练活动日 d 的已观测日负荷与次日日历 HRV/RHR；次日重叠训练按预登记规则排除。",
        )
        for metric in ("hrv_rmssd", "hrv_sdnn", "resting_hr")
    ),
    *(
        _Hypothesis(
            f"training_to_next_sleep_{metric}", "training_load", metric,
            "activity_day", "sleep_day", 1,
            "activity_day_d_to_sleep_day_d_plus_1",
            "训练活动日 d 配对随后一夜、醒于 d+1 的睡眠汇总；日负荷不是单次训练的因果剂量。",
        )
        for metric in ("sleep_hrv", "sleep_rhr", "sleep_duration")
    ),
    _Hypothesis(
        "activity_to_next_sleep_duration", "steps", "sleep_duration",
        "activity_day", "sleep_day", 1,
        "activity_day_d_to_sleep_day_d_plus_1",
        "活动日 d 的步数只配对随后一夜、醒于 d+1 的睡眠；d 当天已醒来的睡眠不属于活动后的结果。",
    ),
)


class PersonalAssociationEngine:
    def build(
        self,
        analysis_run_id: str,
        raw: RawDailyProfile,
        *,
        generated_at=None,
    ) -> PersonalAssociationProfile:
        streams = {
            metric: stream_daily_values(
                raw.series.get(metric, []), metric=metric,
                as_of=raw.as_of, target_day=raw.day,
            )
            for metric in {metric for item in HYPOTHESES for metric in (item.predictor, item.outcome)}
        }
        training_days, verified_days = _training_context(raw)
        associations = []
        for hypothesis in HYPOTHESES:
            predictors = streams[hypothesis.predictor] or {_missing_identity(hypothesis.predictor): {}}
            outcomes = streams[hypothesis.outcome] or {_missing_identity(hypothesis.outcome): {}}
            for predictor_identity, predictor_daily in sorted(predictors.items(), key=_stream_sort_key):
                for outcome_identity, outcome_daily in sorted(outcomes.items(), key=_stream_sort_key):
                    for window_days in WINDOWS:
                        associations.append(self._evaluate(
                            raw, hypothesis, predictor_identity, predictor_daily,
                            outcome_identity, outcome_daily, window_days,
                            training_days, verified_days,
                        ))

        q_values = benjamini_hochberg([item.p_value for item in associations])
        tested = sum(item.p_value is not None for item in associations)
        family_size = len(associations)
        finalized = []
        for item, q_value in zip(associations, q_values):
            significant = q_value is not None and q_value <= 0.05
            confidence = _confidence(item, significant)
            limitations = list(item.limitations)
            if item.p_value is not None and not significant:
                limitations.append("未通过同一家族 Benjamini–Hochberg q≤0.05 门槛；不把关联幅度称为已确认关系。")
            finalized.append(item.model_copy(update={
                "q_value": q_value,
                "fdr_significant": significant,
                "family_size": family_size,
                "tested_count": tested,
                "confidence": confidence,
                "confidence_label": CONFIDENCE_LABELS[confidence.value],
                "limitations": limitations,
            }))
        limitations = [
            "个人关联只描述历史观测，不代表因果；confidence 是证据资格等级，不是因果概率。",
            "4 类问题的 12 个测量变体和 60/90 天窗口预先登记；所有来源组合和窗口在同一 BH 家族校正，未测项计入分母。",
            "缺失不补零、不插值，不合并来源、设备或单位；已知训练混杂排除后另报有效样本和覆盖。",
            "七日块置换仅近似保留短期序列依赖；未控制共同长期趋势、季节性、疾病、饮食等未观测混杂，BH 不保证任意依赖下的 FDR。",
        ]
        if not any(item.status == Availability.AVAILABLE for item in finalized):
            limitations.append("当前没有达到样本量、覆盖、变异度与统计方法门槛的个人关联。")
        return PersonalAssociationProfile(
            analysis_run_id=analysis_run_id, user_id=raw.user_id, date=raw.day,
            generated_at=generated_at if generated_at is not None else raw.as_of,
            period_start=raw.day - timedelta(days=89), period_end=raw.day, as_of=raw.as_of,
            family_id=FAMILY_ID, family_size=family_size, tested_count=tested,
            associations=finalized, limitations=limitations,
        )

    @staticmethod
    def _evaluate(
        raw: RawDailyProfile,
        hypothesis: _Hypothesis,
        predictor_identity: tuple,
        predictor_daily: dict[date, float],
        outcome_identity: tuple,
        outcome_daily: dict[date, float],
        window_days: int,
        training_days: set[date],
        verified_days: set[date],
    ) -> PersonalAssociation:
        start = raw.day - timedelta(days=window_days - 1)
        pairs = [
            (day, value, outcome_daily[day + timedelta(days=hypothesis.lag)])
            for day, value in sorted(predictor_daily.items())
            if start <= day <= raw.day - timedelta(days=hypothesis.lag)
            and day + timedelta(days=hypothesis.lag) in outcome_daily
        ]
        expected = window_days - hypothesis.lag
        minimum = MINIMUM_PAIRED_DAYS[window_days]
        clean = []
        confounded = 0
        unknown = 0
        for day, predictor, outcome in pairs:
            context_day = _confounding_day(hypothesis, day)
            is_confounded = context_day in training_days
            confounded += int(is_confounded)
            unknown += int(context_day not in verified_days and not is_confounded)
            if not is_confounded:
                clean.append((day, predictor, outcome))
        xs = [item[1] for item in clean]
        ys = [item[2] for item in clean]
        coverage = len(pairs) / expected
        analyzed_coverage = len(clean) / expected
        gate_reasons = []
        limitations = [
            "该结果为观测性关联，不用于证明因果。",
            "混杂规则只排除已记录的训练日期；不能证明其它混杂不存在。",
        ]
        if not predictor_daily or not outcome_daily:
            gate_reasons.append("missing_stream")
        if not _compatible_provenance(predictor_identity, outcome_identity):
            gate_reasons.append("incompatible_provenance")
            limitations.append("来源或设备身份不兼容，未运行跨流关联检验。")
        if len(clean) < minimum:
            gate_reasons.append("insufficient_sample_count")
            limitations.append(f"排除已知混杂后的有效配对 {len(clean)} 天，低于最低 {minimum} 天。")
        if analyzed_coverage < MINIMUM_COVERAGE:
            gate_reasons.append("insufficient_coverage")
            limitations.append("排除已知混杂后的有效配对覆盖率低于 50%。")
        if not _has_meaningful_variation(xs) or not _has_meaningful_variation(ys):
            gate_reasons.append("insufficient_variation")
            limitations.append("至少一个变量的有效变化不足，无法稳定计算等级相关。")
        if confounded:
            limitations.append(f"{confounded} 个配对存在预登记日期上的其它训练，已从系数及 p 值计算中排除。")
        if unknown:
            limitations.append(f"{unknown} 个配对的训练覆盖未知，不把未记录的训练日当休息日。")
        identity = "|".join(str(value or "") for value in (
            hypothesis.id, *predictor_identity, *outcome_identity, window_days,
        ))
        seed = stable_seed(FAMILY_ID, raw.user_id, raw.day.isoformat(), identity)
        coefficient = spearman_coefficient(xs, ys) if not gate_reasons else None
        p_value = (
            block_permutation_p_value(xs, ys, [item[0] for item in clean], seed=seed)
            if coefficient is not None else None
        )
        if coefficient is not None and p_value is None:
            gate_reasons.append("insufficient_calendar_blocks")
            limitations.append("有效配对未跨至少六个七日历日块，未输出推断统计。")
            coefficient = None
        status = Availability.AVAILABLE if coefficient is not None else Availability.INSUFFICIENT_DATA
        if coefficient is None:
            direction, direction_label = "INSUFFICIENT_DATA", "数据不足"
            strength, strength_label = "INSUFFICIENT_DATA", "数据不足"
            summary = f"{METRIC_LABELS[hypothesis.predictor]}与{METRIC_LABELS[hypothesis.outcome]}的预登记配对数据不足。"
        else:
            direction, direction_label = _direction(coefficient)
            strength, strength_label = _strength(coefficient)
            summary = (
                f"过去 {window_days} 天，{METRIC_LABELS[hypothesis.predictor]}与"
                f"{METRIC_LABELS[hypothesis.outcome]}观察到{strength_label}{direction_label}关联"
                f"（ρ={coefficient:.3f}；有效配对 {len(clean)}/{expected}）。"
            )
        return PersonalAssociation(
            id=f"association-{sha256(identity.encode()).hexdigest()[:20]}",
            status=status, status_label=AVAILABILITY_LABELS[status.value],
            predictor_metric=hypothesis.predictor, predictor_metric_label=METRIC_LABELS[hypothesis.predictor],
            predictor_source=predictor_identity[0], predictor_source_scope=predictor_identity[1],
            predictor_device_id=predictor_identity[2], predictor_unit=predictor_identity[3],
            outcome_metric=hypothesis.outcome, outcome_metric_label=METRIC_LABELS[hypothesis.outcome],
            outcome_source=outcome_identity[0], outcome_source_scope=outcome_identity[1],
            outcome_device_id=outcome_identity[2], outcome_unit=outcome_identity[3],
            lag_days=hypothesis.lag, window_days=window_days,
            predictor_day_semantics=hypothesis.predictor_day, outcome_day_semantics=hypothesis.outcome_day,
            pairing_rule=hypothesis.pairing, hypothesis_id=hypothesis.id, hypothesis_rationale=hypothesis.rationale,
            period_start=start, period_end=raw.day, as_of=raw.as_of,
            expected_days=window_days, expected_pair_days=expected,
            paired_days=len(pairs), analyzed_pair_days=len(clean), sample_count=len(clean),
            minimum_paired_days=minimum, coverage_ratio=round(coverage, 4),
            analyzed_coverage_ratio=round(analyzed_coverage, 4),
            coefficient=round(coefficient, 4) if coefficient is not None else None,
            p_value=p_value, p_value_method="seven_day_block_permutation",
            permutation_count=DEFAULT_PERMUTATIONS if p_value is not None else 0,
            permutation_block_days=DEFAULT_BLOCK_DAYS, permutation_seed=seed,
            family_id=FAMILY_ID, direction=direction, direction_label=direction_label,
            strength=strength, strength_label=strength_label,
            confidence=ConfidenceBand.NONE, confidence_label=CONFIDENCE_LABELS[ConfidenceBand.NONE.value],
            predictor_median=round(float(median(xs)), 3) if xs else None,
            outcome_median=round(float(median(ys)), 3) if ys else None,
            confounded_pair_days=confounded, confounded_ratio=round(confounded / len(pairs), 4) if pairs else 0,
            confounding_unknown_pair_days=unknown,
            gate_reasons=gate_reasons, summary=summary, limitations=limitations,
        )


def _stream_sort_key(item):
    return tuple(value or "" for value in item[0])


def _missing_identity(metric: str) -> tuple[str, str, None, str]:
    unit = "ms" if "hrv" in metric else "bpm" if "hr" in metric else "min" if metric == "sleep_duration" else "steps" if metric == "steps" else "load"
    return "unknown", "unknown", None, unit


def _compatible_provenance(predictor: tuple, outcome: tuple) -> bool:
    sources_match = predictor[0] == outcome[0] and predictor[0] not in {"", "unknown"}
    canonical_training = predictor[0] == "canonical_workouts" and predictor[2] is None
    devices_match = predictor[2] is None or outcome[2] is None or predictor[2] == outcome[2]
    return (sources_match or canonical_training) and devices_match


def _training_context(raw) -> tuple[set[date], set[date]]:
    training_days = {
        item["local_day"] for item in raw.workouts
        if type(item.get("local_day")) is date and item["local_day"] <= raw.day
    }
    training_days.update(
        day for day, record in raw.training_by_day.items()
        if type(day) is date and day <= raw.day
        and (record.get("workout_count", 0) or record.get("total_duration", 0))
    )
    verified = set()
    for value in (getattr(raw, "training_history_coverage", None) or {}).get("verified_days", []):
        try:
            verified.add(value if type(value) is date else date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    return training_days, verified


def _confounding_day(hypothesis: _Hypothesis, predictor_day: date) -> date:
    if hypothesis.predictor == "steps":
        return predictor_day
    if hypothesis.outcome_day == "sleep_day" and hypothesis.lag == 0:
        return predictor_day - timedelta(days=1)
    return predictor_day + timedelta(days=hypothesis.lag)


def _has_meaningful_variation(values: list[float]) -> bool:
    if len(set(values)) < 3:
        return False
    center = median(values)
    return median(abs(value - center) for value in values) > 0


def _direction(coefficient: float) -> tuple[str, str]:
    if abs(coefficient) < 0.05:
        return "NEUTRAL", "近中性"
    return ("POSITIVE", "正向") if coefficient > 0 else ("NEGATIVE", "负向")


def _strength(coefficient: float) -> tuple[str, str]:
    magnitude = abs(coefficient)
    if magnitude < 0.2:
        return "WEAK", "弱"
    if magnitude < 0.4:
        return "MODEST", "较弱"
    if magnitude < 0.6:
        return "MODERATE", "中等"
    return "STRONG", "较强"


def _confidence(item: PersonalAssociation, significant: bool) -> ConfidenceBand:
    if item.status != Availability.AVAILABLE:
        return ConfidenceBand.NONE
    if not significant or item.confounded_ratio >= 0.5:
        return ConfidenceBand.LOW
    unknown_ratio = item.confounding_unknown_pair_days / item.paired_days if item.paired_days else 1
    if item.analyzed_coverage_ratio >= 0.8 and item.confounded_ratio < 0.25 and unknown_ratio < 0.25:
        return ConfidenceBand.HIGH
    if item.analyzed_coverage_ratio >= 0.6 and item.confounded_ratio < 0.5:
        return ConfidenceBand.MODERATE
    return ConfidenceBand.LOW
