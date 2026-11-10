"""Synthetic checks for the two bounded, account-only report fragments."""
from __future__ import annotations

import ast
import re
from copy import deepcopy
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from vitalis.intelligence.chart_contracts import Chart
from vitalis.intelligence.report_microcharts import render_report_microchart
from vitalis.intelligence.report_rendering import validate_report_content


START = datetime.fromisoformat("2026-10-07T00:00:00+08:00")


def _stamp(hours):
    return (START + timedelta(hours=hours)).isoformat()


def _series(points=None, **updates):
    result = {
        "key": "stress:synthetic", "label": "压力", "unit": "score",
        "source": "zepp", "source_scope": "user_summary", "device_id": None,
        "route_id": "zepp:account", "aggregation": "median",
        "qualification": "vendor_reference",
        "points": [{"timestamp": _stamp(hour), "value": value, "sample_count": 1}
                   for hour, value in (points if points is not None else [(0, 18), (1, None), (6, 45), (21, 82)])],
    }
    result.update(updates)
    return result


def _chart(**updates):
    result = {
        "key": "stress", "title": "压力采样", "kind": "scatter", "unit": "score",
        "timezone": "Asia/Shanghai", "period_start": _stamp(0), "period_end": _stamp(24),
        "state": "partial", "series": [_series()], "preview_series": [], "intervals": [],
        "selected_route": "zepp:account", "resolution_seconds": 60,
        "routes": [{"id": "zepp:account", "kind": "account", "source": "zepp",
                    "label": "Zepp 账户汇总", "state": "available"}],
    }
    result.update(updates)
    return result


def _interval(start, end, category, **updates):
    result = {
        "start": _stamp(start), "end": _stamp(end), "category": category,
        "label": f"2026-10-07 主睡眠 · {category}", "source": "zepp",
        "source_scope": "user_fused", "route_id": "zepp:account",
        "qualification": "vendor_estimate",
    }
    result.update(updates)
    return result


def _sleep(**updates):
    result = _chart(key="sleep", title="睡眠分期与午睡", kind="stages", unit="stage",
                    series=[], period_start=_stamp(-2), intervals=[
                        _interval(-1, 0, "deep"), _interval(0, 2, "light"),
                        _interval(2, 2.1, "awake"), _interval(3, 4, "rem"),
                        _interval(4, 6, "unrecognized"),
                    ])
    result.update(updates)
    return result


class _HTML(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)


def _styles(content, marker):
    return [attrs["style"] for _, attrs in _HTML(content).elements if marker in attrs.get("style", "")]


def _points(content):
    # Point tiles use font glyphs, so they need no per-tile dimensions or external CSS.
    return [style for style in _styles(content, "position:absolute") if "top:" in style and "left:" in style
            and "height:" not in style and "font-size:" not in style]


def _percent(style, name):
    return float(re.search(rf"{name}:([\d.]+)%", style).group(1))


def test_models_and_json_are_equivalent_pure_and_provider_safe():
    for payload in (_chart(), _sleep()):
        before = deepcopy(payload)
        model = Chart.model_validate(payload)
        result = render_report_microchart(payload)
        assert result == render_report_microchart(model)
        assert result == render_report_microchart(model.model_dump(mode="json"))
        assert payload == before
        validate_report_content(result, "html")
        parsed = _HTML(result)
        assert {tag for tag, _ in parsed.elements} <= {"div", "p", "span", "strong"}
        assert all(set(attrs) <= {"style"} for _, attrs in parsed.elements)
        assert not any(word in result.lower() for word in ("<svg", "<img", "<script", "title=", "url(", "http:", "https:"))
        assert "max-width:680px" in result and "width:100%" in result
        assert all(float(value) <= 150 for value in re.findall(r"height:([\d.]+)px", result))


def test_sleep_bounds_use_actual_night_not_calendar_day_and_keep_gaps():
    result = render_report_microchart(_sleep())
    stages = _styles(result, "height:18px")
    stages = [s for s in stages if "left:" in s]
    assert len(stages) == 5
    assert _percent(stages[0], "left") == 0
    assert _percent(stages[1], "width") == pytest.approx(2 * _percent(stages[0], "width"), abs=0.03)
    assert _percent(stages[3], "left") > _percent(stages[2], "left") + _percent(stages[2], "width")
    assert "border-top:3px" in stages[2]
    assert "#e8ecef" in stages[-1]
    text = " ".join(_HTML(result).text)
    for label in ("23:00", "06:00", "深睡", "浅睡", "REM", "清醒", "未知", "缺测"):
        assert label in text
    assert "24:00" not in text


def test_sleep_naps_and_user_annotations_are_not_merged_into_night():
    chart = _sleep()
    chart["intervals"].extend([
        _interval(13, 14, "light", label="2026-10-07 午睡 · light"),
        _interval(11, 12, "annotation", source="user", source_scope="timeline_annotation", route_id=None),
    ])
    result = render_report_microchart(chart)
    assert "午睡未展开" in result
    assert "13:00" not in result and "14:00" not in result
    assert len([s for s in _styles(result, "height:18px") if "left:" in s]) == 5


def test_stress_positions_use_elapsed_time_fixed_scale_and_no_false_zero():
    result = render_report_microchart(_chart())
    points = _points(result)
    assert len(points) == 3
    assert [_percent(style, "left") for style in points] == pytest.approx([0, 25, 87.5], abs=0.1)
    assert [_percent(style, "top") for style in points] == pytest.approx([82, 55, 18], abs=0.1)
    text = " ".join(_HTML(result).text)
    for label in ("0–100", "低", "中", "高", "夜间", "午后", "非诊断", "未记录", "中位数"):
        assert label in text
    assert "空白" in text
    assert "0分" not in text
    assert "#c58b61" in result
    assert "gradient" not in result and "border-bottom" not in result


def test_saved_zero_is_a_real_tile_but_null_is_not():
    result = render_report_microchart(_chart(series=[_series([(0, 0), (3, None), (6, 100)])]))
    assert [_percent(style, "top") for style in _points(result)] == [100, 0]


def test_pressure_uses_saved_points_not_preview_when_they_fit():
    chart = _chart(series=[_series([(2, 18), (15, 66)])],
                   preview_series=[_series([(0, 99)], aggregation="median")])
    points = _points(render_report_microchart(chart))
    assert [_percent(style, "top") for style in points] == [82, 34]


def test_pressure_preview_reports_latest_observation_not_bucket_start():
    chart = _chart(
        series=[_series([(index / 15, 30) for index in range(350)], last_observed_at=_stamp(23.5))],
        preview_series=[_series([(0, 20), (12, 40), (22, 50)], last_observed_at=_stamp(23.5))],
    )
    result = render_report_microchart(chart)
    assert "观测截至 23:30" in result
    assert max(_percent(style, "left") for style in _points(result)) > 90


def test_pressure_does_not_claim_latest_observation_after_report_cutoff():
    chart = _chart(period_end=_stamp(12), series=[_series([(1, 20)], last_observed_at=_stamp(23))])
    assert "观测截至 23:00" not in render_report_microchart(chart)


def test_pressure_cutoff_and_outside_points_follow_supplied_window():
    chart = _chart(period_end=_stamp(12), series=[_series([(-1, 90), (0, 10), (6, 40), (13, 90)])])
    result = render_report_microchart(chart)
    assert [_percent(style, "left") for style in _points(result)] == [0, 50]
    assert "12:00" in result and "24:00" not in result


def test_realistic_synthetic_sizes_fit_without_dropping_points_or_stages():
    sleep = _sleep(intervals=[_interval(-1 + index / 5, -1 + (index + 1) / 5,
                                      ("deep", "light", "rem", "awake")[index % 4])
                              for index in range(40)])
    stress = _chart(series=[_series([(index / 5, (index * 17) % 101) for index in range(109)])])
    for payload, count, marker in ((sleep, 40, "height:18px"), (stress, 109, None)):
        result = render_report_microchart(payload)
        assert len(result) < 8000
        assert len(result.encode("utf-16-le")) // 2 < 8000
        geometry = [s for s in _styles(result, marker) if "left:" in s] if marker else _points(result)
        assert len(geometry) == count
        assert "省略" not in result
        validate_report_content(result, "html")


def test_oversized_series_uses_saved_preview_with_honest_resolution_label():
    chart = _chart(series=[_series([(index / 100, index % 100) for index in range(300)])],
                   preview_series=[_series([(0, 21), (12, 64)], aggregation="median")])
    result = render_report_microchart(chart)
    assert len(_points(result)) == 2
    assert "时段概览" in result and "未记录" in result
    assert "60秒/桶" not in result
    assert "中位数" in result


def test_saved_preview_range_keeps_high_stress_extremes_visible():
    overview=_series([(2,30),(8,40)],aggregation='median')
    overview['points'][0].update(minimum=10,maximum=90)
    chart=_chart(series=[_series([(i/100,30) for i in range(300)])],preview_series=[overview])
    result=render_report_microchart(chart)
    assert '观测范围' in result and '#c58b61' in result
    assert any('top:10%' in style and 'height:80%' in style for style in _styles(result,'border-left'))


def test_oversized_geometry_is_omitted_without_resampling():
    chart = _chart(series=[_series([(index / 100, index % 100) for index in range(300)])])
    result = render_report_microchart(chart)
    assert not _points(result)
    assert "未展开" in result and len(result) < 8000
    sleep = _sleep(intervals=[_interval(index / 100, (index + 1) / 100, "light") for index in range(300)])
    result = render_report_microchart(sleep)
    assert "未展开" in result and "position:absolute" not in result


@pytest.mark.parametrize("payload", [
    _chart(state="unavailable"), _chart(series=[]),
    _chart(series=[_series([(0, None), (3, None)])]), _sleep(intervals=[]),
])
def test_empty_and_unavailable_are_graceful_without_fake_measurements(payload):
    result = render_report_microchart(payload)
    assert "暂无" in result and "position:absolute" not in result
    assert "缺失不补零" in result
    validate_report_content(result, "html")


def test_missing_account_never_uses_present_series():
    chart = _chart()
    chart["routes"][0]["state"] = "missing"
    assert "暂无" in render_report_microchart(chart)
    assert not _points(render_report_microchart(chart))


@pytest.mark.parametrize("change", ["all", "device", "mixed_route", "mixed_device", "mixed_scope", "two_series"])
def test_unfiltered_or_ambiguous_sources_are_rejected(change):
    chart = _chart()
    if change == "all":
        chart["selected_route"] = "all"
    elif change == "device":
        chart["selected_route"] = "zepp:device:synthetic"
    elif change == "mixed_route":
        chart["series"].append(_series(route_id="zepp:device:synthetic"))
    elif change == "mixed_device":
        chart = _sleep()
        chart["intervals"][0]["device_id"] = "synthetic-watch"
        chart["intervals"][1]["device_id"] = "synthetic-ring"
    elif change == "mixed_scope":
        chart["series"][0]["source_scope"] = "device"
    else:
        chart["series"].append(_series(key="other-account-series"))
    with pytest.raises(ValueError, match="account|route|source|series"):
        render_report_microchart(chart)


def test_sleep_overlaps_are_not_silently_painted_as_one_stage():
    chart = _sleep(intervals=[_interval(0, 2, "deep"), _interval(1, 3, "light")])
    with pytest.raises(ValueError, match="overlap"):
        render_report_microchart(chart)


@pytest.mark.parametrize("value", [True, "60", float("nan"), float("inf"), -1, 101])
def test_invalid_pressure_values_are_rejected_not_clamped(value):
    with pytest.raises(ValueError):
        render_report_microchart(_chart(series=[_series([(0, value)])]))


@pytest.mark.parametrize("updates", [
    {"timezone": "invalid/zone"}, {"period_start": "2026-10-07T00:00:00"},
    {"period_end": _stamp(-1)}, {"key": "heart_rate"}, {"resolution_seconds": 0},
])
def test_invalid_time_metric_or_resolution_fails_closed(updates):
    with pytest.raises(ValueError):
        render_report_microchart(_chart(**updates))


def test_payload_text_cannot_inject_markup_or_grow_fragment():
    attack = '\"><script>alert(1)</script><img src=x onerror=evil()>' * 500
    chart = _chart(title=attack, notes=[attack], summary=[attack])
    chart["series"][0]["label"] = attack
    chart["routes"][0]["label"] = attack
    result = render_report_microchart(chart)
    validate_report_content(result, "html")
    assert len(result) < 8000 and "<script" not in result and "evil" not in result


def test_partial_and_truncated_records_keep_visible_qualification():
    result = render_report_microchart(_chart(truncated=True))
    assert "截断" in result
    assert "厂商估计" in render_report_microchart(_sleep())


def test_renderer_has_no_io_or_health_statistics_dependencies():
    path = Path(__file__).resolve().parents[1] / "src/vitalis/intelligence/report_microcharts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {node.module.split(".")[0] for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    imports |= {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert imports <= {"__future__", "collections", "datetime", "math", "typing", "zoneinfo"}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not calls & {"open", "median", "mean", "percentile", "quantile", "sum", "eval", "exec"}
