"""Read-only chart use cases over detached observations or saved reports."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Protocol

from vitalis.intelligence.chart_contracts import ChartBundle
from vitalis.intelligence.charts import build_chart_bundle
from vitalis.intelligence.profile import RawDailyProfile


class ChartReader(Protocol):
    def load(self, user_id: str, start: date, end: date, *, as_of: datetime,
             timezone_name: str, intraday: bool) -> RawDailyProfile: ...


class ChartQuery:
    def __init__(self, reader: ChartReader, *, timezone_name: str,
                 now_factory: Callable[[], datetime] | None = None):
        self.reader = reader
        self.timezone_name = timezone_name
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def query(self, user_id: str, start: date, end: date, *, mode: str = "daily") -> ChartBundle:
        if start > end or (end-start).days > 365:
            raise ValueError("charts cover at most 366 inclusive local dates")
        if mode not in {"intraday", "daily"}:
            raise ValueError("unsupported chart mode")
        if mode == "intraday" and (end-start).days > 6:
            raise ValueError("intraday charts cover at most seven inclusive local dates")
        raw = self.reader.load(user_id, start, end, as_of=self.now_factory(),
            timezone_name=self.timezone_name, intraday=mode == "intraday")
        return build_chart_bundle(raw, start, end, period="daily" if mode == "intraday" else "range",
                                  include_intraday=mode == "intraday")


def report_chart_bundle(report, kind: str) -> ChartBundle:
    """No recalculation: expose exactly the chart facts saved by analysis."""
    raw = report.model_dump(mode="python") if hasattr(report, "model_dump") else dict(report)
    context = raw.get("report_context") or {}
    start = raw.get("period_start") or raw.get("date")
    end = raw.get("period_end") or raw.get("date")
    return ChartBundle(user_id=raw["user_id"], analysis_run_id=raw.get("analysis_run_id"),
        as_of=raw.get("as_of") or context.get("as_of") or raw["generated_at"],
        timezone=context.get("timezone") or "UTC", period_start=start, period_end=end,
        charts=raw.get("charts") or [], notes=["与这份报告使用同一分析快照；读取不会同步或重算。"])
