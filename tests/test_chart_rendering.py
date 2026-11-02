"""Synthetic-only checks for pure, source-preserving chart presentation."""
from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from vitalis.intelligence.chart_rendering import (
    render_chart_html,
    render_chart_page,
    render_chart_svg,
)
from vitalis.intelligence.report_rendering import validate_report_content

NS = {"s": "http://www.w3.org/2000/svg"}


def _point(hour, value, **extra):
    return {
        "timestamp": f"2026-10-07T{hour:02d}:00:00Z", "value": value,
        "sample_count": 1, **extra,
    }


def _series(**updates):
    result = {
        "key": "synthetic-heart-rate", "label": "心率", "unit": "bpm",
        "source": "synthetic-watch", "source_scope": "synthetic_samples",
        "qualification": "observed", "device_id": "synthetic-device",
        "points": [_point(0, 60), _point(1, 75), _point(4, 90)],
    }
    result.update(updates)
    return result


def _chart(**updates):
    result = {
        "key": "synthetic-chart", "title": "合成观测", "kind": "line",
        "unit": "bpm", "timezone": "Asia/Shanghai",
        "period_start": "2026-10-07T00:00:00Z",
        "period_end": "2026-10-07T04:00:00Z",
        "series": [_series()], "preview_series": [], "intervals": [],
        "bands": [], "regions": [], "notes": [], "summary": [],
        "state": "available", "resolution_seconds": None, "truncated": False,
    }
    result.update(updates)
    return result


def _bundle(*charts, **updates):
    result = {
        "schema_version": "1.0", "user_id": "synthetic-user",
        "analysis_run_id": "synthetic-run", "as_of": "2026-10-07T05:00:00Z",
        "timezone": "Asia/Shanghai", "period_start": "2026-10-07",
        "period_end": "2026-10-07", "charts": list(charts), "notes": [],
    }
    result.update(updates)
    return result


def _svg(chart, **kwargs):
    return ET.fromstring(render_chart_svg(chart, **kwargs))


def _elements(root, tag, css_class):
    return root.findall(f'.//s:{tag}[@class="{css_class}"]', NS)


def _all_text(root):
    return " ".join(root.itertext())


class _HTML(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def test_x_positions_use_elapsed_time_not_point_indices():
    root = _svg(_chart())
    points = _elements(root, "circle", "point")
    x0, x1, x4 = [float(item.attrib["cx"]) for item in points]
    assert (x1 - x0) / (x4 - x0) == pytest.approx(0.25, abs=0.001)
    text = _all_text(root)
    assert "2026-10-07" in text and "08:00" in text and "12:00" in text
    assert "Asia/Shanghai" in text
    ticks = [float(item.text) for item in _elements(root, "text", "y-tick")]
    assert min(ticks) <= 60 and max(ticks) >= 90
    assert not set(ticks).issubset({0, 1, 2, 3, 4})


def test_null_breaks_line_without_becoming_a_zero_point():
    series = _series(points=[_point(0, 60), _point(1, 65), _point(2, None), _point(3, 75), _point(4, 80)])
    root = _svg(_chart(series=[series]))
    assert len(_elements(root, "circle", "point")) == 4
    paths = _elements(root, "polyline", "series-line")
    assert len(paths) == 2
    assert all(len(path.attrib["points"].split()) == 2 for path in paths)
    assert "缺失" in _all_text(root)
    assert not re.search(r"(?<![\d.])0 bpm", _all_text(root))


def test_known_resolution_does_not_bridge_unobserved_time_gaps():
    root = _svg(_chart(resolution_seconds=3600))
    paths = _elements(root, "polyline", "series-line")
    assert len(paths) == 1
    assert len(paths[0].attrib["points"].split()) == 2
    assert len(_elements(root, "circle", "point")) == 3


@pytest.mark.parametrize("kind", ["scatter", "bars"])
def test_sparse_stress_is_not_rendered_as_a_continuous_curve(kind):
    series = _series(label="压力稀疏记录", unit="score", points=[_point(0, 18), _point(4, 77)])
    root = _svg(_chart(kind=kind, unit="score", series=[series]))
    assert not _elements(root, "polyline", "series-line")
    assert not root.findall(".//s:path[@class='series-line']", NS)
    if kind == "scatter":
        assert len(_elements(root, "circle", "point")) == 2
    else:
        assert len(_elements(root, "rect", "bar")) == 2
        ticks = [float(item.text) for item in _elements(root, "text", "y-tick")]
        assert min(ticks) <= 0


def test_sources_are_independent_and_different_units_have_distinct_scales():
    series = [
        _series(),
        _series(key="other", label="另一设备", source="synthetic-ring", points=[_point(0, 62), _point(4, 70)]),
        _series(key="sleep", label="睡眠", source="synthetic-diary", unit="min", points=[_point(0, 420), _point(4, 470)]),
    ]
    root = _svg(_chart(series=series))
    lines = _elements(root, "polyline", "series-line")
    assert len(lines) == 3
    assert len({item.attrib["stroke"] for item in lines}) == 3
    assert len(_elements(root, "g", "unit-panel")) == 2
    text = _all_text(root)
    for label in ("bpm", "min", "synthetic-watch", "synthetic-ring", "synthetic-diary"):
        assert label in text
    for circle in _elements(root, "circle", "point"):
        title = circle.find("s:title", NS).text
        assert "来源" in title and "样本数" in title and "2026-10-07" in title
    assert "90 bpm" in text and "470 min" in text


def test_pydantic_chart_and_bundle_contracts_are_accepted_without_adapter_logic():
    from vitalis.intelligence.chart_contracts import Chart, ChartBundle

    chart = Chart.model_validate(_chart(preview_series=[_series()]))
    bundle = ChartBundle.model_validate(_bundle(chart.model_dump(mode="json")))
    assert render_chart_svg(chart) == render_chart_svg(chart.model_dump(mode="json"))
    validate_report_content(render_chart_html(chart), "html")
    assert "<svg" in render_chart_page(bundle)


def test_tooltips_retain_sample_count_raw_range_qualification_and_source():
    root = _svg(_chart(series=[_series(points=[_point(1, 63.125, sample_count=3, minimum=60, maximum=66)])]))
    title = _elements(root, "circle", "point")[0].find("s:title", NS).text
    for value in ("63.125", "3", "60", "66", "observed", "synthetic_samples", "synthetic-device", "+08:00"):
        assert value in title


def test_bands_stop_at_null_and_do_not_invent_missing_bounds():
    band = {
        "key": "synthetic-band", "label": "已保存范围", "source": "synthetic-watch",
        "qualification": "observed", "points": [
            {"timestamp": _point(hour, None)["timestamp"], "lower": lower, "upper": upper}
            for hour, lower, upper in [(0, 50, 70), (1, 55, 75), (2, None, None), (3, 65, 85), (4, 70, 90)]
        ],
    }
    root = _svg(_chart(bands=[band]))
    assert len(_elements(root, "polygon", "band")) == 2
    assert "已保存范围" in _all_text(root)
    ranges = _elements(root, "line", "band-point")
    assert len(ranges) == 4
    assert "50—70 bpm" in ranges[0].find("s:title", NS).text
    assert "2026-10-07 08:00" in ranges[0].find("s:title", NS).text


def test_explicit_reference_range_and_median_are_labelled_not_recomputed():
    root = _svg(_chart(reference={"lower": 52, "upper": 81, "median": 68, "label": "合成个人参照"}))
    assert len(_elements(root, "rect", "reference-band")) == 1
    assert len(_elements(root, "line", "reference-median")) == 1
    assert "合成个人参照" in _all_text(root)
    assert "68" in _all_text(root)


def test_sleep_stages_are_layered_coloured_intervals_with_real_duration():
    intervals = [
        {"start": _point(start, None)["timestamp"], "end": _point(end, None)["timestamp"],
         "category": category, "label": label, "source": "synthetic-watch",
         "source_scope": "sleep_stage", "qualification": "device_estimated"}
        for start, end, category, label in [(0, 1, "deep", "深睡"), (1, 3, "light", "浅睡"), (3, 4, "unknown", "未知")]
    ]
    root = _svg(_chart(kind="stages", unit="stage", series=[], intervals=intervals))
    blocks = _elements(root, "rect", "stage-interval")
    assert len(blocks) == 3
    assert float(blocks[1].attrib["width"]) == pytest.approx(2 * float(blocks[0].attrib["width"]), abs=0.01)
    assert len({item.attrib["y"] for item in blocks}) == 3
    assert len({item.attrib["fill"] for item in blocks}) == 3
    assert "未知" in _all_text(root) and "device_estimated" in _all_text(root)


def test_empty_and_all_null_charts_explain_absence_without_zero_measurements():
    for chart in (_chart(series=[], state="unavailable"), _chart(series=[_series(points=[_point(0, None)])], state="partial")):
        root = _svg(chart)
        assert not _elements(root, "circle", "point")
        assert "无可绘制的数值" in _all_text(root)
        assert not re.search(r"(?<![\d.])0 bpm", _all_text(root))


def test_body_map_keeps_unknown_grey_and_separates_main_auxiliary_counts():
    regions = [
        {"key": "chest", "label": "胸部", "direct_sets": 3, "indirect_sets": 2,
         "observed_sets": 4, "qualification": "estimated", "source": "synthetic-watch", "side": "bilateral"},
        {"key": "biceps", "label": "肱二头肌", "direct_sets": None, "indirect_sets": None,
         "observed_sets": None, "qualification": "unknown", "source": "synthetic-watch", "side": "bilateral"},
        {"key": "unmapped", "label": "未映射部位", "direct_sets": 8, "indirect_sets": None,
         "observed_sets": 8, "qualification": "estimated", "source": "synthetic-watch", "side": "unknown"},
    ]
    root = _svg(_chart(kind="body_map", unit="sets", series=[], regions=regions))
    text = _all_text(root)
    for expected in ("正面", "背面", "主要 3", "辅助 2", "已记录参与 4", "未知", "非实测肌肉受力", "未映射部位"):
        assert expected in text
    paths = _elements(root, "path", "body-region")
    assert paths
    chest = [p for p in paths if p.attrib["data-region"] == "chest"]
    biceps = [p for p in paths if p.attrib["data-region"] == "biceps"]
    assert chest and biceps and chest[0].attrib["fill"] != biceps[0].attrib["fill"]
    assert not any(p.attrib["data-region"] == "unmapped" for p in paths)
    assert "主要 0" not in text


def test_body_map_does_not_compute_observed_sets_from_other_counts_or_mix_sources():
    regions = [
        {"key": "chest", "label": "胸部", "direct_sets": 5, "indirect_sets": 3,
         "observed_sets": None, "qualification": "estimated", "source": source, "side": "bilateral"}
        for source in ("synthetic-A", "synthetic-B")
    ]
    root = _svg(_chart(kind="body_map", unit="sets", series=[], regions=regions))
    assert len(_elements(root, "g", "body-source")) == 2
    text = _all_text(root)
    assert "synthetic-A" in text and "synthetic-B" in text
    assert "已记录参与 8" not in text
    assert "已记录参与 未知" in text


def test_body_map_preserves_coarse_vendor_regions_and_separate_qualifications():
    names = {"arm_front": "上臂前侧", "arm_back": "上臂后侧", "thigh_front": "大腿前侧", "thigh_back": "大腿后侧", "lower_leg_front": "小腿前侧", "neck": "颈部"}
    regions = [{"key": key, "label": label, "direct_sets": 2, "indirect_sets": 1, "observed_sets": 3,
                "source": "synthetic-watch", "qualification": "vendor_anatomy_reference", "side": "unknown"}
               for key, label in names.items()]
    regions.append({"key": "biceps", "label": "肱二头肌", "direct_sets": 1, "indirect_sets": 0, "observed_sets": 1,
                    "source": "synthetic-watch", "qualification": "recorded_action_estimate", "side": "left"})
    root = _svg(_chart(kind="body_map", unit="sets", series=[], regions=regions))
    assert len(_elements(root, "g", "body-source")) == 2
    for key, label in names.items():
        paths = [node for node in _elements(root, "path", "body-region") if node.attrib["data-region"] == key]
        assert paths and any(node.attrib["fill"] != "#dce3e8" for node in paths)
        assert label in _all_text(root)
    assert "侧别未知" in _all_text(root)
    assert "双侧示意不表示已确认左右训练" in _all_text(root)


def test_same_qualification_coarse_and_specific_regions_are_not_painted_over_each_other():
    regions = [{"key": key, "label": label, "direct_sets": 2, "indirect_sets": 1, "observed_sets": value,
                "source": "synthetic-watch", "qualification": "estimated", "side": "bilateral"}
               for key, label, value in (("arm_front", "上臂前侧", 3), ("biceps", "肱二头肌", 8))]
    root = _svg(_chart(kind="body_map", unit="sets", series=[], regions=regions))
    assert len(_elements(root, "g", "body-source")) == 2
    for key in ("arm_front", "biceps"):
        assert any(node.attrib["data-region"] == key and node.attrib["fill"] != "#dce3e8" for node in _elements(root, "path", "body-region"))


def test_svg_and_page_escape_all_user_text_and_ignore_arbitrary_colours():
    attack = '\"><script>alert(1)</script><img src=x onerror=evil()> & \\n'
    chart = _chart(title=attack, notes=[attack], summary=[attack])
    chart["series"][0].update(label=attack, source=attack, unit=attack, color="url(https://example.invalid/x)")
    root = _svg(chart)
    assert not root.findall(".//s:script", NS) and not root.findall(".//s:image", NS)
    svg = render_chart_svg(chart)
    assert "url(" not in svg and "<script>" not in svg
    page = render_chart_page(_bundle(chart, notes=[attack]))
    parsed = _HTML(page)
    scripts = [attrs for tag, attrs in parsed.elements if tag == "script"]
    assert len(scripts) == 1 and "src" not in scripts[0]
    assert not any(tag in {"img", "iframe", "object", "embed", "link"} for tag, _ in parsed.elements)
    assert not any(name.startswith("on") for _, attrs in parsed.elements for name in attrs)
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    assert attack not in script and "alert(" not in script and "innerHTML" not in script
    assert "Content-Security-Policy" in page


def test_preview_uses_only_preaggregated_values_and_conservative_elements():
    preview = _series(points=[_point(0, 12.25, sample_count=5), _point(1, None, sample_count=0), _point(4, 24.5, sample_count=7)])
    chart = _chart(preview_series=[preview], notes=["微图每桶为已保存均值，不代表连续观测。"])
    rendered = render_chart_html(chart)
    validate_report_content(rendered, "html")
    parsed = _HTML(rendered)
    assert {tag for tag, _ in parsed.elements} <= {"div", "span", "p"}
    assert all(set(attrs) <= {"style", "lang"} for _, attrs in parsed.elements)
    text = " ".join(parsed.text)
    assert "均值" in text and "12.25" in text and "24.5" in text
    assert "90 bpm" not in text and "60 bpm" not in text
    assert "缺失" in text
    assert "<svg" not in rendered and "<script" not in rendered and "url(" not in rendered


def test_push_text_keeps_only_endpoints_without_audit_fields_or_all_point_values():
    points = [_point(0, 111.125), _point(1, 222.25), _point(2, 333.375), _point(4, 444.5)]
    chart = _chart(preview_series=[_series(points=points)], summary=["要点1", "要点2", "不在推送的要点3"], notes=["缺失说明", "不在推送的明细说明"])
    text = " ".join(_HTML(render_chart_html(chart)).text)
    assert "111.125" in text and "444.5" in text
    assert "222.25" not in text and "333.375" not in text
    for audit in ("synthetic_samples", "synthetic-device", "来源 ", "observed", "不在推送"):
        assert audit not in text


def test_preview_time_positions_do_not_space_sparse_points_evenly():
    chart = _chart(preview_series=[_series()])
    parsed = _HTML(render_chart_html(chart))
    styles = [attrs["style"] for tag, attrs in parsed.elements if tag == "span" and "position:absolute" in attrs.get("style", "")]
    positions = [float(re.search(r"left:([\d.]+)%", style).group(1)) for style in styles]
    assert positions == pytest.approx([0, 25, 100])


def test_preview_does_not_downsample_when_analysis_did_not_supply_buckets():
    text = " ".join(_HTML(render_chart_html(_chart())).text)
    assert "未提供" in text and "微图" in text
    assert "90" not in text
    start = datetime(2026, 10, 7, tzinfo=UTC)
    points = [{"timestamp": (start + timedelta(minutes=i)).isoformat(), "value": i, "sample_count": 1} for i in range(33)]
    text = " ".join(_HTML(render_chart_html(_chart(preview_series=[_series(points=points)]))).text)
    assert "32" in text and "省略" in text


def test_missing_preview_has_no_invented_zero_to_one_measurement_scale():
    text = " ".join(_HTML(render_chart_html(_chart(preview_series=[_series(points=[_point(0, None, sample_count=0)])]))).text)
    assert "无可绘制的数值" in text
    assert "纵轴 0—1" not in text


def test_preview_escapes_text_and_preserves_safe_push_contract():
    attack = '未确认 <a href="javascript:evil()">部位</a> \\n'
    chart = _chart(title=attack, preview_series=[_series(label=attack)], notes=[attack])
    rendered = render_chart_html(chart)
    validate_report_content(rendered, "html")
    assert {tag for tag, _ in _HTML(rendered).elements} <= {"div", "span", "p"}


def test_page_is_self_contained_and_has_deterministic_chart_filter():
    bundle = _bundle(_chart(), _chart(key="scatter", kind="scatter", title="离散压力"))
    first = render_chart_page(bundle)
    assert first == render_chart_page(deepcopy(bundle))
    parsed = _HTML(first)
    assert len([1 for tag, _ in parsed.elements if tag == "svg"]) == 2
    assert any(tag == "select" for tag, _ in parsed.elements)
    assert "addEventListener" in first and "hidden" in first
    assert "fetch(" not in first and "https://" not in first


def test_page_initially_shows_only_first_chart_without_all_option():
    page = render_chart_page(_bundle(_chart(), _chart(title="第二张")))
    parsed = _HTML(page)
    cards = [attrs for tag, attrs in parsed.elements if tag == "section" and "data-chart-index" in attrs]
    assert len(cards) == 2
    assert "hidden" not in cards[0] and "hidden" in cards[1]
    options = [attrs for tag, attrs in parsed.elements if tag == "option"]
    assert [item["value"] for item in options] == ["0", "1"]
    assert "selected" in options[0]
    assert "selector.value !== 'all'" not in page


def test_page_collapses_notes_but_keeps_safety_notices_and_unit_panels_visible():
    chart = _chart(
        notes=["合成来源解释"], summary=["已保存摘要"], state="partial", truncated=True,
        series=[_series(), _series(unit="min", source="synthetic-diary")],
    )
    page = render_chart_page(_bundle(chart, notes=["合成报告说明"]))
    parsed = _HTML(page)
    details = [attrs for tag, attrs in parsed.elements if tag == "details"]
    assert len(details) == 2 and all("open" not in attrs for attrs in details)
    assert "来源与说明" in page and "合成来源解释" in page and "合成报告说明" in page
    root = ET.fromstring(re.search(r"<svg.*?</svg>", page, re.DOTALL).group())
    text = _all_text(root)
    assert "合成来源解释" not in text
    assert "缺失不补零" in text and "截断" in text
    assert len(_elements(root, "g", "unit-panel")) == 2
    assert "synthetic-watch" in text and "synthetic-diary" in text
    assert "已保存摘要" in page


def test_page_uses_compact_plot_without_clipping_standalone_svg():
    page = render_chart_page(_bundle(_chart()))
    root = ET.fromstring(re.search(r"<svg.*?</svg>", page, re.DOTALL).group())
    assert float(root.attrib["height"]) < 420
    assert "background:#faf8f4" in page
    assert "min-width:420px" not in page
    assert "max-height" not in page
    assert float(_svg(_chart()).attrib["height"]) >= 420


def test_empty_page_has_disabled_selector_and_explicit_missing_state():
    page = render_chart_page(_bundle())
    parsed = _HTML(page)
    assert "暂无可展示图表，缺失不补零。" in page
    assert any(tag == "select" and "disabled" in attrs for tag, attrs in parsed.elements)
    assert not any(tag == "svg" for tag, _ in parsed.elements)


def test_renderers_are_pure_and_accept_model_dump_without_importing_model_dependencies():
    chart = _chart(preview_series=[_series()])
    before = deepcopy(chart)

    class Model:
        def model_dump(self, *, mode):
            assert mode == "json"
            return deepcopy(chart)

    for render in (render_chart_svg, render_chart_html):
        assert render(chart) == render(Model())
    render_chart_page(_bundle(chart))
    assert chart == before
    path = Path(__file__).resolve().parents[1] / "src/vitalis/intelligence/chart_rendering.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not imports.intersection({"sqlalchemy", "httpx", "requests", "PIL", "pydantic", "vitalis"})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "75"])
def test_nonfinite_or_non_numeric_measurements_are_rejected(bad):
    with pytest.raises(ValueError, match="finite|numeric"):
        render_chart_svg(_chart(series=[_series(points=[_point(0, bad)])]))


@pytest.mark.parametrize("updates", [
    {"period_start": "2026-10-07T00:00:00"},
    {"period_end": "2026-10-06T00:00:00Z"},
    {"timezone": "not-a-timezone"},
    {"kind": "javascript:evil()"},
])
def test_invalid_time_or_kind_contracts_fail_closed(updates):
    with pytest.raises(ValueError):
        render_chart_svg(_chart(**updates))


def test_reversed_band_or_interval_rejected_instead_of_silently_inventing_shape():
    with pytest.raises(ValueError):
        render_chart_svg(_chart(bands=[{"key": "bad", "label": "bad", "source": "synthetic", "qualification": "unknown", "points": [{"timestamp": _point(0, None)["timestamp"], "lower": 90, "upper": 50}]}]))
    with pytest.raises(ValueError):
        render_chart_svg(_chart(kind="stages", intervals=[{"start": _point(2, None)["timestamp"], "end": _point(1, None)["timestamp"], "category": "deep", "label": "bad", "source": "synthetic", "source_scope": "sleep", "qualification": "estimated"}]))


def test_dst_axis_uses_instants_and_local_time_with_offsets_in_tooltips():
    chart = _chart(
        timezone="America/New_York", period_start="2026-11-01T05:00:00Z", period_end="2026-11-01T07:00:00Z",
        series=[_series(points=[{"timestamp": instant, "value": 60 + index, "sample_count": 1} for index, instant in enumerate(("2026-11-01T05:00:00Z", "2026-11-01T06:00:00Z", "2026-11-01T07:00:00Z"))])],
    )
    root = _svg(chart)
    circles = _elements(root, "circle", "point")
    xs = [float(item.attrib["cx"]) for item in circles]
    assert xs[1] - xs[0] == pytest.approx(xs[2] - xs[1], abs=0.01)
    titles = [item.find("s:title", NS).text for item in circles]
    assert "-04:00" in titles[0] and "-05:00" in titles[1]


def test_dimensions_and_truncation_notice_are_explicit():
    root = _svg(_chart(truncated=True), width=640, height=360)
    assert root.attrib["viewBox"].split()[2] == "640"
    assert "截断" in _all_text(root)
    with pytest.raises(ValueError):
        render_chart_svg(_chart(), width=True)
