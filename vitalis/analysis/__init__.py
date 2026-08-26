"""分析引擎（Analysis Engine）。

分三层，严格遵循「LLM 只负责解释，不负责计算」原则：
1. RuleEngine        确定性规则（睡眠不足 -> 恢复下降）
2. StatisticalEngine 趋势统计（7 天 HRV、30 天训练负荷）
3. AIEngine          LLM 只把前两层的结构化结果转成自然语言解释/建议

编排入口：AnalysisPipeline.run(daily, history)
"""
from .ai_engine import AIEngine
from .engine import AnalysisPipeline, AnalysisResult
from .rule_engine import RuleEngine
from .statistical_engine import StatisticalEngine

__all__ = [
    "AIEngine",
    "AnalysisPipeline",
    "AnalysisResult",
    "RuleEngine",
    "StatisticalEngine",
]
