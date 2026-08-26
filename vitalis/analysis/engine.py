"""分析流水线：编排 Rule -> Statistical -> AI 三层。"""

from dataclasses import dataclass, field
from datetime import date

from vitalis.models import DailyHealth, Decision

from .ai_engine import AIEngine
from .rule_engine import RuleEngine
from .statistical_engine import StatisticalEngine


@dataclass
class AnalysisResult:
    """一次分析的全部产出：结构化决策 + 统计特征 + 自然语言解释。"""

    decision: Decision
    stats: dict = field(default_factory=dict)
    explanation: str = ""
    llm_used: bool = False

    def to_dict(self) -> dict:
        return {
            "overall_score": self.decision.overall_score,
            "recovery_level": self.decision.recovery_level,
            "stress_level": self.decision.stress_level,
            "training_readiness": self.decision.training_readiness,
            "hrv_trend_pct": self.stats.get("hrv_trend_pct"),
            "load_30d": self.stats.get("load_30d"),
            "sleep_avg_7d_min": self.stats.get("sleep_avg_7d_min"),
            "matched_rules": self.decision.items,
            "explanation": self.explanation,
            "llm_used": self.llm_used,
        }


class AnalysisPipeline:
    """三层引擎编排。

    用法：
        pipeline = AnalysisPipeline()
        result = pipeline.run(target_daily, history_dailies)
    """

    def __init__(self, rule_engine: RuleEngine | None = None,
                 statistical_engine: StatisticalEngine | None = None,
                 ai_engine: AIEngine | None = None):
        self.rule = rule_engine or RuleEngine()
        self.statistical = statistical_engine or StatisticalEngine()
        self.ai = ai_engine or AIEngine()

    def run(self, target: DailyHealth, history: list[DailyHealth]) -> AnalysisResult:
        """执行分析。

        args:
            target: 要分析的目标日（如今天）
            history: 历史日快照（含 target 之前的若干天）
        """
        stats = self.statistical.compute(history, target)

        # 把统计特征回填到 target，供 RuleEngine 消费
        target.hrv_trend_pct = stats.get("hrv_trend_pct")
        target.hrv = (target.hrv or None)

        decision = self.rule.evaluate(target)

        explanation = self.ai.explain(decision, stats)
        return AnalysisResult(
            decision=decision,
            stats=stats,
            explanation=explanation,
            llm_used=self.ai.enabled,
        )