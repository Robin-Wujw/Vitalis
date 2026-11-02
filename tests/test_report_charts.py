"""Reports render saved charts without querying or recalculating observations."""
from datetime import datetime, timezone

from vitalis.intelligence.chart_contracts import Chart, ChartPoint, ChartSeries
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.report_rendering import render_report, validate_report_content
from tests.test_report_content import synthetic_daily_fixture


def chart():
    series = ChartSeries(key="hybrid_charge:a", label="Zepp Charge", unit="score", source="zepp",
        source_scope="device", qualification="vendor_reference", points=[
            ChartPoint(timestamp=datetime(2026,9,5,h,tzinfo=timezone.utc),value=value,sample_count=1)
            for h,value in ((0,80),(2,70),(4,60))])
    return Chart(key="charge",title="Zepp BioCharge",kind="line",unit="score",timezone="Asia/Shanghai",
        period_start=datetime(2026,9,4,16,tzinfo=timezone.utc),period_end=datetime(2026,9,5,16,tzinfo=timezone.utc),
        series=[series],preview_series=[series],summary=["合成数据；缺测保留空白"],state="partial")


def test_evening_chart_survives_projection_and_inline_html_contract():
    daily = synthetic_daily_fixture()
    daily["charts"] = [chart().model_dump(mode="json")]
    report = EveningBriefingEngine().build(daily)
    assert len(report.charts) == 1
    html = render_report(report,"html").content
    markdown = render_report(report,"markdown").content
    assert "Zepp BioCharge" not in html
    assert "Zepp BioCharge" not in markdown
    assert "记录曲线" not in html
    assert "记录曲线" not in markdown
    assert "<svg" not in html
    validate_report_content(html,"html")


def test_saved_report_chart_html_api(client, monkeypatch):
    from vitalis.entrypoints.api.routes import charts
    report = EveningBriefingEngine().build({**synthetic_daily_fixture(),"charts":[chart().model_dump(mode="json")]})
    monkeypatch.setattr(charts,"_report",lambda *_args:report)
    response = client.get("/api/reports/evening/charts.html?day=2026-09-05",headers={"X-User-Id":"chart-html-user"})
    assert response.status_code == 200
    assert "<svg" in response.text
    assert response.headers["cache-control"] == "private, no-store"
