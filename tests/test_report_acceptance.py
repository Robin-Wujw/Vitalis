"""Reader-facing acceptance for the daily reports.

The examples under docs/examples/reports come from the production pipeline over
56+ days of synthetic history (``tools/generate_report_examples.py --check``
keeps them current). These tests judge what a person reads, not data contracts:
the 2026-10-09 renderer that listed every public projection fact (677 lines, 26k
characters on real data) passed all contract tests.
"""

import re
from pathlib import Path

import pytest
from markdown import markdown

from vitalis.intelligence import report_rendering
from vitalis.intelligence.public_reports import to_public_report_view
from vitalis.intelligence.report_rendering import render_report, visible_text, visible_text_length

EXAMPLES = Path(__file__).resolve().parents[1] / "docs" / "examples" / "reports"
# Natural (untrimmed) reading length for the synthetic long history; well below
# the hard push limits, so a report that grows several-fold fails here first.
READING_BUDGET = {"morning": 1_500, "daily": 2_500, "evening": 2_500}
REQUIRED_SECTIONS = {
    "morning": ("关键记录", "今天的重点"),
    "daily": ("关键记录",),
    "evening": ("关键记录", "明日重点"),
}
# Audit material that belongs to the API and weekly/monthly reports only.
FORBIDDEN = {
    "28/60/90-day window": re.compile(r"近\s*(?:28|60|90)\s*[日天]|(?:28|60|90)\s*[日天]窗口"),
    "T+N training response": re.compile(r"T\s*\+\s*(?:\d|N)|训练反应"),
    "q value": re.compile(r"(?<![A-Za-z])q\s*[=＝<≤]|q\s*值|BH\s*q"),
    "per-signal sources": re.compile(r"来源[:：]|来源\s*zepp|source_scope|覆盖\s*\d+\s*/\s*\d+"),
}
TRIM_NOTES = ("推送篇幅有限", "完整报告可通过")


def acceptance_problems(kind: str, content: str, template: str) -> list[str]:
    text = visible_text(content, template)
    problems = []
    length = visible_text_length(content, template)
    if length > READING_BUDGET[kind]:
        problems.append(f"visible length {length} > {READING_BUDGET[kind]}")
    headings = (
        re.findall(r"^#{2}\s+(.+?)\s*$", content, re.MULTILINE) if template == "markdown"
        else re.findall(r"<h2[^>]*>(.*?)</h2>", content)
    )
    problems += [f"missing section {name}" for name in REQUIRED_SECTIONS[kind] if name not in headings]
    problems += [f"forbidden {label}" for label, pattern in FORBIDDEN.items() if pattern.search(text)]
    problems += [f"trimmed: {note}" for note in TRIM_NOTES if note in text]
    return problems


@pytest.mark.parametrize("kind", sorted(READING_BUDGET))
@pytest.mark.parametrize("template, suffix", [("markdown", ".md"), ("html", ".fragment.html")])
def test_daily_reports_from_long_synthetic_history_pass_reader_acceptance(kind, template, suffix):
    content = (EXAMPLES / f"{kind}{suffix}").read_text(encoding="utf-8")
    assert acceptance_problems(kind, content, template) == []


def _spread_projection_sections(view, presentation):
    """The 2026-10-09 layout: every public projection fact with its provenance."""
    sections = []
    for block in view["blocks"]:
        lines = []
        for fact in block.get("facts") or []:
            coverage = (block.get("coverage") or {}).get(str(fact.get("metric")), [{}])[0]
            lines.append((report_rendering._UNIT_RANKS["fact"], [("row", (
                f"{fact.get('metric')} {fact.get('value')} {fact.get('unit') or ''} · "
                f"来源：{fact.get('source')}/{fact.get('source_scope')} · "
                f"覆盖 {coverage.get('distinct_days') or 0}/{coverage.get('expected_days') or 1}"
            ))]))
        for comparison in block.get("comparisons") or []:
            lines.append((report_rendering._UNIT_RANKS["finding"], [("paragraph", str(comparison))]))
        if lines:
            sections.append(report_rendering._block(str(block.get("title")), [], lines))
    return [], sections


def test_restoring_the_projection_dump_layout_fails_acceptance(monkeypatch):
    from tests.test_report_content import synthetic_daily_fixture
    from vitalis.intelligence.evening_briefing import EveningBriefingEngine

    briefing = EveningBriefingEngine().build(synthetic_daily_fixture())
    assert to_public_report_view(briefing, "evening").blocks
    normal = render_report(briefing, "markdown").content
    assert not [item for item in acceptance_problems("evening", normal, "markdown")
                if item.startswith(("forbidden", "trimmed"))]

    monkeypatch.setattr(report_rendering, "_report_sections", _spread_projection_sections)
    for template in ("markdown", "html"):
        dumped = render_report(briefing, template).content
        problems = acceptance_problems("evening", dumped, template)
        assert "forbidden per-signal sources" in problems
        assert "missing section 明日重点" in problems


def test_trimmed_reports_are_rejected_even_inside_the_budget():
    content = markdown("## 关键记录\n\n推送篇幅有限，本节另有 3 项未展开。\n\n## 明日重点\n\n保持作息。")
    assert acceptance_problems("evening", content, "html") == ["trimmed: 推送篇幅有限"]
