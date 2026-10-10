"""Cross-channel report content is qualified data, not an HTML/Markdown snapshot."""
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path

from markdown import markdown
import pytest

from vitalis.intelligence.contracts import ReportBriefing
from vitalis.intelligence.report_rendering import (
    RenderedReport, markdown_html_length, provider_text_length, render_report, validate_report_content,
)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []
        self.tags = []

    def handle_data(self, data):
        self.values.append(data)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def _text(content):
    reader = _Text()
    reader.feed(content)
    return " ".join(reader.values), reader.tags


def _briefing(**updates):
    data = {
        "period": "evening", "analysis_run_id": "synthetic-run", "user_id": "synthetic-user",
        "date": "2026-10-07", "period_start": "2026-10-07", "period_end": "2026-10-07",
        "headline": "已记录训练，今晚优先保持睡眠安排",
        "as_of": "2026-10-07T13:20:00+00:00", "report_context": {"timezone": "Asia/Shanghai"},
        "metrics": [
            {"key": "sleep", "label": "昨夜睡眠", "value": 402, "unit": "min",
             "comparison": {"label": "近 28 日个人中位数", "reference_value": 425}},
            {"key": "steps", "label": "今日步数", "value": 7460, "unit": "steps", "detail": "截至 21:20"},
            {"key": "strength", "label": "力量训练", "value": 45, "unit": "min", "detail": "1 个动作 · 3 组"},
        ],
        "training": [{
            "title": "力量训练", "date": "2026-10-07", "duration_minutes": 45,
            "exercises": [{
                "name": "二头肌弯举", "exercise_id": "biceps_curl", "set_count": 3,
                "sets": [
                    {"order": index, "repetitions": repetitions, "weight_value": 10,
                     "weight_unit": "kg", "weight_basis": "per_hand"}
                    for index, repetitions in enumerate((12, 10, 8), 1)
                ],
                "reference_date": "2026-10-04", "comparison": "同样重量与组数下，总次数增加 6 次。",
            }],
        }],
        "suggestions": ["沿用现有计划，给今晚的睡眠留出时间。"],
        "sections": [{"key": "internal", "title": "来源资格", "facts": ["synthetic-only-debug-provenance"]}],
    }
    data.update(updates)
    return ReportBriefing.model_validate(data)


def test_markdown_and_html_keep_values_units_cutoff_and_ordered_sets():
    briefing = _briefing()
    md = render_report(briefing, "markdown")
    html = render_report(briefing, "html")
    md_text, _ = _text(markdown(md.content))
    html_text, tags = _text(html.content)
    for text in (md_text, html_text):
        for expected in (
            "6 小时 42 分钟", "7,460 步", "45 分钟", "每手 10 kg",
            "12 / 10 / 8 次", "2026-10-04", "2026-10-07 21:20",
        ):
            assert expected in text
        assert "3 × 10" not in text
        assert "synthetic-only-debug" not in text
        assert "请回复" not in text
    assert {"h1", "h2"}.issubset(tags)
    assert md.template == "markdown" and md.media_type == "text/markdown"
    assert html.template == "html" and html.media_type == "text/html"
    for report in (md, html):
        assert report.content_sha256 == hashlib.sha256(report.content.encode("utf-8")).hexdigest()
        assert report.as_dict()["content"] == report.content


def test_missing_optional_hrv_and_no_training_do_not_create_empty_sections():
    report = render_report(_briefing(training=[]))
    assert "## 力量训练" not in report.content
    assert "HRV" not in report.content
    assert "休息日" not in report.content
    assert "数据限制" not in report.content


def test_unknown_units_and_changing_weight_remain_local_per_set():
    briefing = _briefing()
    exercise = briefing.training[0].exercises[0]
    exercise.sets[0].weight_unit = None
    exercise.sets[1].weight_value = 12
    exercise.comparison = None
    for target in ("markdown", "html"):
        report = render_report(briefing, target)
        text, _ = _text(markdown(report.content) if target == "markdown" else report.content)
        assert "第 1 组 · 12 次 · 负重 10（单位未记录）" in text
        assert "第 2 组 · 10 次 · 每手 12 kg" in text
        assert "第 3 组 · 8 次 · 每手 10 kg" in text
        assert "增加 6" not in text


def test_zero_is_displayed_and_missing_is_not_zero():
    briefing = _briefing(metrics=[
        {"key": "steps", "label": "步数", "value": 0, "unit": "steps"},
        {"key": "sleep", "label": "昨夜睡眠", "unit": "min", "gap": "昨夜睡眠尚未同步。"},
    ])
    for target in ("markdown", "html"):
        report = render_report(briefing, target)
        text, _ = _text(markdown(report.content) if target == "markdown" else report.content)
        assert "0 步" in text
        assert "昨夜睡眠尚未同步。" in text
        assert "0 分钟" not in text


def test_untrusted_long_name_is_text_and_cannot_create_links_headings_or_css():
    name = '卧推\n\n# 伪造标题 <script>alert(1)</script> [点击](javascript:evil) ' + "很长动作名" * 80 + r" \n"
    briefing = _briefing()
    briefing.training[0].exercises[0].name = name
    for target in ("markdown", "html"):
        report = render_report(briefing, target)
        text, tags = _text(markdown(report.content) if target == "markdown" else report.content)
        assert "伪造标题" in text
        assert "很长动作名" * 80 in text
        assert tags.count("h1") == 1
        assert not {"a", "script", "img", "style", "iframe"}.intersection(tags)
        assert "\\n" not in report.content


@pytest.mark.parametrize("content,template", [
    ("```markdown\n# report\n```", "markdown"),
    ("~~~\n# report\n~~~", "markdown"),
    (json.dumps("# report\ntext"), "markdown"),
    (r"# report\ntext", "markdown"),
    ("# Markdown report", "html"),
    ("&lt;div&gt;report&lt;/div&gt;", "html"),
    ('<div><script>alert(1)</script></div>', "html"),
    ('<div onclick="evil()">report</div>', "html"),
    ('<div style="background:url(https://example.invalid)">report</div>', "html"),
    ("<div>HTML report</div>", "markdown"),
    ("report", "json"),
])
def test_transport_contract_rejects_invalid_content(content, template):
    with pytest.raises(ValueError):
        validate_report_content(content, template)


def test_long_ascii_action_keeps_its_text_and_wraps_without_css():
    name = "UnrecognizedStrengthAction" * 12
    briefing = _briefing()
    briefing.training[0].exercises[0].name = name
    rendered = render_report(briefing, "html")
    reader = _Text()
    reader.feed(rendered.content)
    assert "wbr" in reader.tags
    assert name in "".join(reader.values)


def test_media_type_is_checked_before_preparing_a_report():
    with pytest.raises(ValueError, match="media type"):
        RenderedReport("title", "body", "application/json")


def test_numeric_nan_cannot_enter_rendered_report():
    briefing = _briefing()
    briefing.metrics[0].value = float("nan")
    with pytest.raises(ValueError, match="finite"):
        render_report(briefing)


def test_daily_profile_has_its_own_projection_and_does_not_use_evening_label():
    daily_profile = {
        "analysis_run_id": "daily-run",
        "user_id": "synthetic-user",
        "date": "2026-10-07",
        "generated_at": "2026-10-07T13:20:00+00:00",
        "report_context": {"as_of": "2026-10-07T13:20:00+00:00", "timezone": "Asia/Shanghai", "source_mode": "mock"},
        "data_quality": {"status": "SUFFICIENT", "status_label": "数据完整"},
        "facts": {
            "sleep_duration": [{"value": 420, "unit": "min"}],
            "steps": [{"value": 7460, "unit": "steps"}],
        },
        "features": {
            "sleep": {"duration_minutes": 420},
            "activity": {"steps": {"value": 7460}},
            "training": {"recent_workouts": [], "running": {}, "strength": {}},
        },
        "events": [],
        "decision": {"action": "INSUFFICIENT_DATA", "action_plan": {}},
    }

    daily = render_report(daily_profile, "markdown")
    evening = render_report(_briefing(), "markdown")

    assert daily.title.startswith("Vitalis 日报 ·")
    assert evening.title.startswith("Vitalis 晚报 ·")
    assert daily.content != evening.content
    assert "数据质量" in daily.content
    assert "7 小时" in daily.content
    assert "日报" in daily.content
    assert "晚报" not in daily.content


def test_daily_profile_markdown_and_html_share_the_same_facts():
    daily_profile = {
        "analysis_run_id": "daily-run",
        "user_id": "synthetic-user",
        "date": "2026-10-07",
        "report_context": {"as_of": "2026-10-07T13:20:00+00:00", "timezone": "Asia/Shanghai"},
        "data_quality": {"status": "PARTIAL", "status_label": "部分可用"},
        "facts": {"steps": [{"value": 7460, "unit": "steps"}]},
        "features": {
            "sleep": {"duration_minutes": 420},
            "activity": {"steps": {"value": 7460}},
            "training": {"recent_workouts": [], "running": {}, "strength": {}},
        },
        "events": [],
        "decision": {"action": "INSUFFICIENT_DATA", "action_plan": {}},
    }
    markdown_report = render_report(daily_profile, "markdown")
    html_report = render_report(daily_profile, "html")
    markdown_text, _ = _text(markdown(markdown_report.content))
    html_text, _ = _text(html_report.content)
    for expected in ("日报", "7 小时", "7,460 步", "部分可用", "2026-10-07 21:20"):
        assert expected in markdown_text
        assert expected in html_text


@pytest.mark.parametrize("source_mode, label", [
    ("mock", "合成数据"), ("real", "真实数据"), ("replay", "回放数据"),
    ("unknown", "来源模式未标记"), (None, "来源模式未标记"),
])
def test_rendered_metadata_shows_source_mode_in_both_channels(source_mode, label):
    context = {"timezone": "Asia/Shanghai", "as_of": "2026-10-07T13:20:00+00:00"}
    if source_mode is not None:
        context["source_mode"] = source_mode
    briefing = _briefing(report_context=context)
    markdown_report = render_report(briefing, "markdown")
    html_report = render_report(briefing, "html")
    markdown_text, _ = _text(markdown(markdown_report.content))
    html_text, _ = _text(html_report.content)
    assert label in markdown_text and label in html_text
    if source_mode:
        assert briefing.report_context["source_mode"] == source_mode
        assert f"mode: {source_mode}" not in markdown_text


@pytest.mark.parametrize("observed_at, expected", [
    ("2026-10-07", "所属日期 2026-10-07"),
    ("2026-10-07T00:00:00Z", "观测于 2026-10-07 08:00 +0800"),
])
def test_daily_fact_dates_keep_their_time_precision(observed_at, expected):
    from vitalis.intelligence.contracts import MeasurementFact
    from vitalis.intelligence.report_presentation import daily_profile_presentation

    fact = MeasurementFact.model_validate({
        "metric": "steps", "value": 7460, "unit": "steps", "observed_at": observed_at,
        "provenance": {"source": "zepp", "source_scope": "normalized_daily_record"},
    })
    payload = {
        "date": "2026-10-07", "facts": {"steps": [fact.model_dump(mode="json")]},
        "features": {}, "report_context": {"timezone": "Asia/Shanghai"},
    }
    section = next(item for item in daily_profile_presentation(payload)["sections"] if item["key"] == "daily_facts")
    assert expected in section["facts"][0]
    if len(observed_at) == 10:
        assert "08:00" not in section["facts"][0]


@pytest.mark.parametrize("state, label", [
    ("stale", "数据已更新，显示上一版结果"),
    ("queued", "更新已排队，显示上一版结果"),
    ("running", "正在更新，显示上一版结果"),
    ("failed", "更新失败，显示上一版结果"),
])
def test_last_good_report_state_is_visible_in_both_channels(state, label):
    briefing = _briefing(report_context={"report_state": {"state": state}})
    for target in ("markdown", "html"):
        assert label in render_report(briefing, target).content


def test_generated_daily_and_evening_examples_are_distinct_named_reports():
    examples = Path(__file__).parents[1] / "docs" / "examples" / "reports"
    daily = (examples / "daily.md").read_text(encoding="utf-8")
    evening = (examples / "evening.md").read_text(encoding="utf-8")
    assert daily.startswith("# 日报 ·")
    assert evening.startswith("# 晚报 ·")
    assert daily != evening


def large_briefing(exercises=300):
    """An evening report with far more training detail than one push can carry."""
    return _briefing(training=[{
        "title": "力量训练", "date": "2026-10-07", "duration_minutes": 95,
        "exercises": [{
            "name": f"动作{index}", "exercise_id": f"exercise_{index}", "set_count": 3,
            "sets": [{"order": order, "repetitions": 9 + order, "weight_value": 20,
                      "weight_unit": "kg", "weight_basis": "total"} for order in (1, 2, 3)],
            "reference_date": "2026-10-04", "comparison": f"同样重量与组数下，总次数增加 {index % 5 + 1} 次。",
        } for index in range(exercises)],
    }])


_BUDGET_MEASURES = [
    ("markdown", markdown_html_length), ("markdown", provider_text_length), ("html", provider_text_length),
]


def test_provider_text_length_counts_utf16_code_units():
    assert provider_text_length("晚报") == 2
    assert provider_text_length("\U0001F600") == 2


def test_markdown_html_length_matches_the_converted_markdown():
    for content in (render_report(_briefing()).content, render_report(large_briefing(40)).content):
        # The estimate counts a newline after every block, including the last one.
        assert markdown_html_length(content) - provider_text_length(markdown(content)) in (0, 1)


@pytest.mark.parametrize(("target", "measure"), _BUDGET_MEASURES)
def test_channel_budget_leaves_reports_within_budget_unchanged(target, measure):
    briefing = _briefing()
    budgeted = render_report(briefing, target, max_length=18_000, measure=measure)
    assert budgeted.content == render_report(briefing, target).content


@pytest.mark.parametrize(("target", "measure"), _BUDGET_MEASURES)
def test_channel_budget_keeps_records_plan_and_whole_exercises(target, measure):
    briefing = large_briefing()
    assert measure(render_report(briefing, target).content) > 18_000

    content = render_report(briefing, target, max_length=18_000, measure=measure).content

    assert measure(content) <= 18_000
    assert content == render_report(briefing, target, max_length=18_000, measure=measure).content
    text = _text(content)[0] if target == "html" else content
    for expected in ("关键记录", "6 小时 42 分钟", "7,460 步", "明日重点", "沿用现有计划，给今晚的睡眠留出时间。",
                     "动作0", "本节另有", "完整报告可通过 Vitalis API 或 Hermes 查看。"):
        assert expected in text
    if target == "markdown":
        lines = [line for line in content.splitlines() if line]
        for index, line in enumerate(lines):
            if line.startswith("**动作"):
                assert "3 组" in lines[index + 1]
                assert lines[index + 2].startswith("对照 2026-10-04")


def test_html_key_records_pair_tone_colors_with_arrow_text():
    briefing = _briefing(metrics=[
        {"key": "rhr", "label": "静息心率", "value": 61, "unit": "bpm",
         "comparison": {"label": "个人参照（近 28 日）", "reference_value": 55.5, "change_percent": 9.9}},
        {"key": "hrv", "label": "HRV", "value": 59, "unit": "ms",
         "comparison": {"label": "个人参照（近 28 日）", "reference_value": 53.5, "change_percent": 10.3}},
        {"key": "sleep", "label": "睡眠时长", "value": 446, "unit": "min",
         "comparison": {"label": "个人参照（近 28 日）", "reference_value": 447.5, "change_percent": -0.3}},
    ])
    content = render_report(briefing, "html").content
    text = _text(content)[0]

    # A higher resting heart rate reads as "watch", a higher HRV as "good"; both carry the arrow as text.
    assert "↑ 9.9%" in text and "↑ 10.3%" in text and "↓ 0.3%" in text
    assert "color:#a8431f;font-weight:600\">↑ 9.9%" in content
    assert "color:#127a3e;font-weight:600\">↑ 10.3%" in content
    assert "color:#4a5964;font-weight:600\">↓ 0.3%" in content
    assert "变化 +9.9%" in render_report(briefing, "markdown").content


def test_stress_zone_shares_become_one_labelled_bar_in_html_only():
    zones = ["放松区间 21%", "正常区间 51%", "中等压力区间 25%", "高压力区间 3%"]
    briefing = _briefing(sections=[{
        "key": "display_signals", "title": "日内记录", "display": True,
        "facts": ["平均压力评分 51", *zones, "心率记录均值 70.3 次/分钟"],
    }])
    html_content = render_report(briefing, "html").content
    html_text = _text(html_content)[0]
    markdown_content = render_report(briefing, "markdown").content

    assert "压力分区占比" in html_text
    for label in ("放松 21%", "正常 51%", "中等压力 25%", "高压力 3%"):
        assert label in html_text
    assert html_text.count("区间") == 0
    assert "平均压力评分" in html_text and "70.3 次/分钟" in html_text
    for line in zones:
        assert line in markdown_content
    assert "压力分区占比" not in markdown_content
