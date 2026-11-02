"""Authenticated chart data and self-contained visual report views."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from vitalis.application.charts import report_chart_bundle
from vitalis.bootstrap import get_chart_query, get_intelligence_query
from vitalis.entrypoints.api.deps import require_user_id
from vitalis.intelligence.chart_contracts import ChartBundle

router = APIRouter(tags=["charts"])
Kind = Literal["daily", "morning", "evening", "weekly", "monthly"]
_PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


@router.get("/charts", response_model=ChartBundle, operation_id="get_charts")
def charts(
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    mode: Literal["intraday", "daily"] = "daily",
    user_id: str = Depends(require_user_id),
) -> ChartBundle:
    """Read stored observations as qualified charts; no synchronization or jobs."""
    try:
        return get_chart_query().query(user_id, from_date, to_date, mode=mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="图表范围或数据预算不符合要求") from exc


def _report(user_id: str, kind: str, day: date | None):
    query = get_intelligence_query()
    method = {"daily":query.daily,"morning":query.morning_briefing,"evening":query.evening_briefing,
              "weekly":query.weekly,"monthly":query.monthly}[kind]
    result = method(user_id,day)
    if result is None:
        raise HTTPException(status_code=404,detail="指定日期尚未生成报告")
    return result


@router.get("/reports/{kind}/charts", response_model=ChartBundle, operation_id="get_report_charts")
def report_charts(kind: Kind, day: date | None = None, user_id: str = Depends(require_user_id)) -> ChartBundle:
    return report_chart_bundle(_report(user_id,kind,day),kind)


@router.get("/reports/{kind}/charts.html", response_class=HTMLResponse, operation_id="get_report_chart_page")
def report_chart_page(kind: Kind, day: date | None = None, user_id: str = Depends(require_user_id)):
    from vitalis.intelligence.chart_rendering import render_chart_page
    return HTMLResponse(render_chart_page(report_chart_bundle(_report(user_id,kind,day),kind)), headers=_PRIVATE)


@router.get("/reports/{kind}/charts/{chart_key}.svg", operation_id="get_report_chart_svg")
def report_chart_svg(kind: Kind, chart_key: str, day: date | None = None, user_id: str = Depends(require_user_id)):
    from vitalis.intelligence.chart_rendering import render_chart_svg
    bundle = report_chart_bundle(_report(user_id,kind,day),kind)
    chart = next((c for c in bundle.charts if c.key == chart_key), None)
    if chart is None:
        raise HTTPException(status_code=404, detail="该报告没有指定图表")
    return Response(render_chart_svg(chart), media_type="image/svg+xml", headers=_PRIVATE)
