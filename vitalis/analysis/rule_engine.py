"""Rule Engine —— 确定性规则层。

只做基于明确规则/阈值的判断，结果可复现、可解释、不依赖 LLM。
示例规则（可扩展）：
- 睡眠 < 6h  -> 恢复下降
- 深睡占比低 -> 睡眠质量差
- 训练负荷高 + 恢复分低 -> 不建议高强度训练
"""

from dataclasses import dataclass, field

from vitalis.models import (
    DailyHealth,
    Decision,
    RecoveryLevel,
    StressLevel,
    TrainingReadiness,
)

# 阈值常量：集中管理，便于调参
SLEEP_HOURS = 60.0  # 分钟 -> 小时换算
MIN_SLEEP_MIN = 360          # 6h
GOOD_SLEEP_MIN = 480         # 8h
DEEP_SLEEP_RATIO_LOW = 0.15  # 深睡占比阈值
LOAD_HIGH = 60               # 训练负荷高阈值
LOAD_VERY_HIGH = 85
HRV_TREND_BAD = -5.0         # HRV 7 天趋势为负且超阈值，视为恢复变差


@dataclass
class RuleEngine:
    """确定性健康规则引擎。依赖注入便于测试与复现。"""

    # 可覆盖默认阈值（测试时用）
    sleep_thresholds: dict = field(
        default_factory=lambda: {
            "good": GOOD_SLEEP_MIN,
            "low": MIN_SLEEP_MIN,
            "deep_ratio": DEEP_SLEEP_RATIO_LOW,
        }
    )

    def evaluate(self, daily: DailyHealth) -> Decision:
        items: list[str] = []
        reasons: dict = {}

        sleep_min = daily.sleep.sleep_duration if daily.sleep else None
        sleep_score = daily.sleep.sleep_score if daily.sleep else None
        load = daily.training.total_load if daily.training else 0
        workout_count = daily.training.workout_count if daily.training else 0
        hrv_trend = daily.hrv_trend_pct

        # ---- 1. 睡眠 ----
        sleep_factor = 20.0 if sleep_min is None else 50.0  # 无数据=未知(低分)，满分 50
        if sleep_min is not None:
            if sleep_min < self.sleep_thresholds["low"]:
                items.append(f"睡眠不足：{sleep_min:.0f} 分钟（< 6h）→ 恢复下降")
                sleep_factor = 12
            elif sleep_min < self.sleep_thresholds["good"]:
                items.append(f"睡眠一般：{sleep_min:.0f} 分钟（6~8h）")
                sleep_factor = 32
            else:
                items.append(f"睡眠充足：{sleep_min:.0f} 分钟（≥ 8h）")
                sleep_factor = 50
            if daily.sleep.deep_sleep and sleep_min:
                deep_ratio = daily.sleep.deep_sleep / sleep_min
                if deep_ratio < self.sleep_thresholds["deep_ratio"]:
                    items.append(f"深睡占比偏低：{deep_ratio:.0%}")
                    sleep_factor = max(sleep_factor - 10, 0)
            reasons["sleep_min"] = sleep_min
            reasons["sleep_score"] = sleep_score
            reasons["sleep_point"] = round(sleep_factor, 1)

        # ---- 2. 训练负荷 ----
        load_factor = 30.0  # 满分 30，负荷适中得满分
        if load >= LOAD_VERY_HIGH:
            items.append(f"训练负荷过高（{load}）→ 需要更多恢复")
            load_factor = 8
        elif load >= LOAD_HIGH:
            items.append(f"训练负荷较高（{load}）")
            load_factor = 22
        elif load > 0:
            items.append(f"今日有训练，负荷 {load}")
            load_factor = 30
        else:
            items.append("今日无训练（休息日）")
            load_factor = 24
        reasons["training_load"] = load
        reasons["workout_count"] = workout_count
        reasons["load_point"] = load_factor

        # ---- 3. HRV 趋势（来自统计引擎） ----
        hrv_factor = 12.0 if hrv_trend is None else 20.0  # 无数据=未知(低分)，满分 20
        if hrv_trend is not None and hrv_trend < HRV_TREND_BAD:
            items.append(f"HRV 7 天趋势下降 {hrv_trend:.0f}% → 恢复能力受损")
            hrv_factor = 6
        elif hrv_trend is not None and hrv_trend > 0:
            hrv_factor = 20
            items.append(f"HRV 趋势向好（{hrv_trend:+.0f}%）")
        reasons["hrv_trend_pct"] = hrv_trend
        reasons["hrv_point"] = hrv_factor

        # ---- 综合得分 0-100 ----
        overall = round(sleep_factor + load_factor + hrv_factor)
        overall = max(0, min(100, overall))

        # ---- 恢复等级 ----
        if overall >= 80:
            level = RecoveryLevel.READY
        elif overall >= 60:
            level = RecoveryLevel.MODERATE
        elif overall >= 40:
            level = RecoveryLevel.LOW
        else:
            level = RecoveryLevel.OVERREACH

        # ---- 压力等级 ----
        stress = self._stress_level(load, sleep_min, workout_count)

        # ---- 训练建议（确定性） ----
        readiness = self._readiness(overall, level)

        return Decision(
            overall_score=overall,
            recovery_level=level,
            stress_level=stress,
            training_readiness=readiness,
            items=items,
            reasons=reasons,
        )

    @staticmethod
    def _stress_level(load: int, sleep_min: int | None, workout_count: int) -> StressLevel:
        high_load = load >= LOAD_HIGH
        poor_sleep = sleep_min is not None and sleep_min < MIN_SLEEP_MIN
        if high_load and poor_sleep:
            return StressLevel.HIGH
        if high_load or poor_sleep:
            return StressLevel.MEDIUM
        return StressLevel.LOW

    @staticmethod
    def _readiness(score: int, level: RecoveryLevel) -> TrainingReadiness:
        if score >= 85:
            return TrainingReadiness.FULL
        if score >= 70:
            return TrainingReadiness.MODERATE
        if score >= 55:
            return TrainingReadiness.EASY
        return TrainingReadiness.NOT_READY
