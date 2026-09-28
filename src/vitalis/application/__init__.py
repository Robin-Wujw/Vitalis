"""Application services coordinating durable requests."""

from .aggregation import RangeSummaryQuery
from .ports import RangeSummaryReader

__all__ = [
    "RangeSummaryQuery",
    "RangeSummaryReader",
    "AnalysisDataset",
    "AnalysisPolicy",
    "AnalysisRequest",
    "AnalysisTrace",
    "analyze",
    "analyze_with_trace",
]


def __getattr__(name):
    if name in __all__[2:]:
        from .analysis import (
            AnalysisDataset,
            AnalysisPolicy,
            AnalysisRequest,
            AnalysisTrace,
            analyze,
            analyze_with_trace,
        )
        return {
            "AnalysisDataset": AnalysisDataset,
            "AnalysisPolicy": AnalysisPolicy,
            "AnalysisRequest": AnalysisRequest,
            "AnalysisTrace": AnalysisTrace,
            "analyze": analyze,
            "analyze_with_trace": analyze_with_trace,
        }[name]
    raise AttributeError(name)
