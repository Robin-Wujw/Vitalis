"""Cross-channel report content is qualified data, not an HTML/Markdown snapshot."""
from html.parser import HTMLParser
import hashlib
import json

from markdown import markdown
import pytest

from vitalis.intelligence.contracts import ReportBriefing
from vitalis.intelligence.report_rendering import (
    RenderedReport, render_report, validate_report_content,
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
    assert {"h1", "h2", "h3"}.issubset(tags)
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
