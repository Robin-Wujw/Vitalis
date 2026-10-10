"""Render already-computed report projections without I/O or health algorithms."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
import html
from html.parser import HTMLParser
import json
import math
import re
from typing import Any, Literal

from .report_formatting import minutes_text, number, payload_of, timestamp_text
from .public_reports import PublicReportView, report_presentation, to_public_report_view


RENDERER_VERSION = "4.0"
ReportTarget = Literal["markdown", "html"]
_MEDIA_TYPES = {"markdown": "text/markdown", "html": "text/html"}
_LABELS = {"morning": "晨报", "daily": "日报", "evening": "晚报", "weekly": "周报", "monthly": "月报"}
_SOURCE_MODE_LABELS = {"mock": "合成数据", "real": "真实数据", "replay": "回放数据"}
_UNITS = {
    "min": "分钟", "minutes": "分钟", "steps": "步", "steps/day": "步/日",
    "ms": "毫秒", "bpm": "次/分", "km": "公里", "m": "米", "kcal": "千卡",
    "sessions": "次", "days": "天", "sets": "组", "sessions/week": "次/周",
    "brpm": "次/分钟", "score": "分", "load": "负荷单位", "times": "次",
    "events/hour": "次/小时", "observations": "项", "ml/kg/min": "ml/kg/min",
}
_BASIS = {"per_hand": "每手", "total": "总重", "machine": "器械标示", "bodyweight": "自重"}


@dataclass(frozen=True)
class RenderedReport:
    title: str
    content: str
    media_type: str
    renderer_version: str = RENDERER_VERSION
    template: str = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        target = next((key for key, value in _MEDIA_TYPES.items() if value == self.media_type), None)
        if target is None:
            raise ValueError("unsupported report media type")
        validate_report_content(self.content, target)
        object.__setattr__(self, "template", target)
        object.__setattr__(self, "content_sha256", sha256(self.content.encode("utf-8")).hexdigest())

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title, "content": self.content, "media_type": self.media_type,
            "template": self.template, "renderer_version": self.renderer_version,
            "content_sha256": self.content_sha256,
        }


def _plain(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


def _markdown(value: Any) -> str:
    # Only templates create Markdown structure. Field newlines, raw HTML,
    # links, and heading markers are displayed as text in both channels.
    text = html.escape(_plain(value), quote=False).replace("\\", "&#92;")
    text = re.sub(r"([`*_{}\[\]#|!])", r"\\\1", text)
    return re.sub(r"(?i)\b(https?|javascript|data):", lambda match: match.group(1) + "&#58;", text)


def _value(value: Any, unit: str, digits: int = 0) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("report metric must contain a finite numeric value")
    if unit in {"min", "minutes", "分钟"}:
        return minutes_text(round(value)) or ""
    if unit in {"min/night", "分钟/晚"}:
        return (minutes_text(round(value)) or "") + "/晚"
    shown = number(value, digits)
    label = _UNITS.get(unit, unit)
    return f"{shown} {label}".strip()


def _comparison(metric: dict[str, Any]) -> str | None:
    comparison = metric.get("comparison")
    if not comparison:
        return None
    label = _plain(comparison.get("label"))
    parts = [label]
    start, end = comparison.get("reference_period_start"), comparison.get("reference_period_end")
    period_text = (str(start) if start == end else f"{start}—{end}") if start and end else ""
    if period_text and period_text not in label:
        parts.append(period_text)
    reference = comparison.get("reference_value")
    if reference is not None:
        parts.append(_value(reference, metric.get("unit", ""), metric.get("digits", 0)))
    change = comparison.get("change_percent")
    if change is not None:
        if not math.isfinite(change):
            raise ValueError("report comparison must be finite")
        parts.append("持平" if change == 0 else f"变化 {change:+.1f}%")
    return " · ".join(part for part in parts if part) or None


def _subtitle(payload: dict[str, Any]) -> str:
    start, end = payload["period_start"], payload["period_end"]
    period = str(start) if start == end else f"{start}—{end}"
    context = payload.get("report_context") or {}
    cutoff = payload.get("as_of")
    if cutoff:
        shown = timestamp_text(cutoff, context.get("timezone") or "UTC")
        if re.search(r" [+-]\d{4}$", shown):
            shown = shown.rsplit(" ", 1)[0]
        period += f" · 数据截至 {shown}"
    else:
        period += " · 数据截至时间未记录"
    period += f" · {_SOURCE_MODE_LABELS.get(payload['source_mode'], '来源模式未标记')}"
    state_label = {
        "stale": "数据已更新，显示上一版结果",
        "queued": "更新已排队，显示上一版结果",
        "running": "正在更新，显示上一版结果",
        "failed": "更新失败，显示上一版结果",
    }.get(payload["state"])
    if state_label:
        period += f" · {state_label}"
    if context.get("period_mode") == "rolling":
        period += " · 滚动窗口"
    for key, label in (("late", "延迟报告"), ("partial", "部分报告"), ("facts_only", "仅展示事实")):
        if payload.get(key):
            period += f" · {label}"
    if payload.get("delivered_as_of") and payload["delivered_as_of"] != cutoff:
        period += f" · 投递截至 {timestamp_text(payload['delivered_as_of'], context.get('timezone') or 'UTC')}"
    return period


def _set_weight(row: dict[str, Any]) -> str:
    value = row.get("weight_value")
    basis = _BASIS.get(row.get("weight_basis"), "")
    if value is None:
        return "自重" if row.get("weight_basis") == "bodyweight" else "负重未记录"
    unit = row.get("weight_unit")
    shown = number(value)
    if not unit:
        return f"负重 {shown}（单位未记录）"
    return f"{basis + ' ' if basis else ''}{shown} {unit}"


def _exercise_lines(exercise: dict[str, Any]) -> list[str]:
    rows = exercise.get("sets") or []
    count = exercise.get("set_count", len(rows))
    if not rows:
        return [f"{count} 组 · 次数和负重未记录"] if count else []
    weights = {(row.get("weight_value"), row.get("weight_unit"), row.get("weight_basis")) for row in rows}
    repetitions = [row.get("repetitions") for row in rows]
    compact = (
        len(weights) == 1 and len(rows) == count
        and all(value is not None for value in repetitions)
        and not any(row.get("duration_seconds") is not None or row.get("rest_seconds") is not None for row in rows)
    )
    if compact:
        # A changing distribution is never replaced by an average dose.
        reps = " / ".join(str(value) for value in repetitions)
        return [f"{_set_weight(rows[0])} · {count} 组 · {reps} 次"]
    lines = []
    for index, row in enumerate(rows, 1):
        repetitions = row.get("repetitions")
        parts = [
            f"第 {row.get('order', index)} 组",
            f"{repetitions} 次" if repetitions is not None else "次数未记录",
            _set_weight(row),
        ]
        if row.get("duration_seconds") is not None:
            parts.append(f"用时 {row['duration_seconds']} 秒")
        if row.get("rest_seconds") is not None:
            parts.append(f"休息 {row['rest_seconds']} 秒")
        lines.append(" · ".join(parts))
    return lines


Item = tuple[str, str]
# Selection order when a channel budget applies: key records and changes first,
# then training and section facts in reading order, caveats last.
_UNIT_RANKS = {"record": 0, "finding": 0, "fact": 1, "limitation": 2}
_SECTION_OMISSION = "推送篇幅有限，本节另有 {count} 项未展开。"
_FULL_REPORT_NOTE = "完整报告可通过 Vitalis API 或 Hermes 查看。"
_FINDING_HEADINGS = {"morning": "昨夜与今天", "daily": "今天的变化", "evening": "今天的变化", "weekly": "本周变化", "monthly": "本月变化"}
_NEXT_HEADINGS = {"daily": "下一步", "evening": "明日重点", "weekly": "下周重点", "monthly": "下月重点"}
# Reading reports show the engine-curated sections for their own period. Audit
# listings (versions, sample counts, every saved fact) stay in the API payload.
_PERIOD_SECTIONS = {
    "morning": {"yesterday_activity", "observed_training"},
    "weekly": {"sleep_recovery", "training"},
    "monthly": {"sleep_recovery", "associations"},
}
_AUDIT_SECTIONS = {"daily_facts", "daily_quality", "daily_audit"}
_QUALITY_LABELS = {"SUFFICIENT": "数据完整", "PARTIAL": "部分可用", "INSUFFICIENT": "数据不足"}
_NON_CAUSAL = "个人数据观测关联，不表示因果"


def _section_line(section_key: str, text: str) -> str | None:
    # Running class counts are internal bookkeeping; associations are always
    # stated as observed, never causal.
    if section_key in {"training", "training_activity"} and "本次分析的课型" in text:
        return None
    if section_key == "associations" and "不表示因果" not in text:
        return f"{text}（{_NON_CAUSAL}）"
    return text


@dataclass(frozen=True)
class _Section:
    """A block split into framing that is always shown and atomic optional units."""

    head: list[Item]
    units: list[tuple[int, list[Item]]]
    tail: list[Item]


def _block(title: str, units: list[tuple[int, list[Item]]], tail: list[Item] | None = None) -> _Section:
    return _Section([("block_start", ""), ("h2", title)], units, [*(tail or []), ("block_end", "")])


def _record_units(metrics: list[dict[str, Any]], *, facts_only: bool) -> list[tuple[int, list[Item]]]:
    units = []
    for metric in metrics:
        heading = str(metric.get("label") or "已保存记录")
        if metric.get("value") is not None:
            heading += "｜" + _value(metric["value"], metric.get("unit", ""), metric.get("digits", 0))
        notes = [None if facts_only else _comparison(metric), metric.get("detail"), metric.get("gap")]
        units.append((_UNIT_RANKS["record"], [("strong", heading), *(("note", str(note)) for note in notes if note)]))
    return units


def _workout_units(workout: dict[str, Any], zone: str, *, facts_only: bool) -> list[tuple[int, list[Item]]]:
    info = [str(workout.get("date") or "")]
    if workout.get("started_at"):
        info.append(timestamp_text(workout["started_at"], zone, short=True))
    if workout.get("duration_minutes") is not None:
        info.append(_value(workout["duration_minutes"], "min"))
    summary = [("paragraph", " · ".join(part for part in info if part))]
    summary.extend(("paragraph", str(item)) for item in workout.get("facts") or [])
    units = [(_UNIT_RANKS["fact"], summary)]
    for exercise in workout.get("exercises") or []:
        items = [("strong", str(exercise.get("name") or "未识别动作"))]
        items.extend(("paragraph", line) for line in _exercise_lines(exercise))
        if exercise.get("comparison") and not facts_only:
            reference = f"对照 {exercise['reference_date']} · " if exercise.get("reference_date") else ""
            items.append(("note", reference + exercise["comparison"]))
        units.append((_UNIT_RANKS["fact"], items))
    return units


def _shown_section(section: dict[str, Any], kind: str) -> bool:
    key = section.get("key")
    return key not in _AUDIT_SECTIONS and bool(section.get("display") or key in _PERIOD_SECTIONS.get(kind, set()))


def _report_sections(view: dict[str, Any], presentation: dict[str, Any]) -> tuple[list[Item], list[_Section]]:
    """Lay out the curated report content: key records, plan, changes, training, period sections."""
    kind = view["kind"]
    facts_only = bool(view.get("facts_only"))
    zone = (view.get("report_context") or {}).get("timezone") or "UTC"
    preamble = [("paragraph", str(item)) for item in [*(view.get("summary") or []), *(view.get("alerts") or [])]]
    quality = view.get("data_quality") or {}
    if kind == "daily" and quality.get("status"):
        label = quality.get("status_label") or _QUALITY_LABELS.get(quality["status"], "资格未确认")
        preamble.append(("paragraph", f"数据质量：{label}"))
    suggestions = [("paragraph", str(item)) for item in view.get("suggestions") or []]
    sections = []
    records = _record_units(presentation.get("metrics") or [], facts_only=facts_only)
    if records:
        sections.append(_block("关键记录", records))
    if kind == "morning" and suggestions:
        sections.append(_block("今天的重点", [], suggestions))
    findings = [] if facts_only else list(dict.fromkeys(str(item) for item in presentation.get("findings") or []))
    if findings:
        sections.append(_block(_FINDING_HEADINGS[kind], [(_UNIT_RANKS["finding"], [("paragraph", item)]) for item in findings]))
    for workout in presentation.get("training") or []:
        sections.append(_block(str(workout.get("title") or "训练记录"), _workout_units(workout, zone, facts_only=facts_only)))
    shown = set(findings)
    for section in presentation.get("sections") or []:
        if not _shown_section(section, kind):
            continue
        units = []
        for key, rank, style in (("facts", "fact", "paragraph"), ("interpretation", "finding", "paragraph"),
                                 ("limitations", "limitation", "note")):
            for item in section.get(key) or []:
                text = _section_line(str(section.get("key")), str(item))
                # Engines repeat a period's main change inside several sections.
                if text is not None and text not in shown:
                    shown.add(text)
                    units.append((_UNIT_RANKS[rank], [(style, text)]))
        if units:
            sections.append(_block(str(section.get("title") or "记录"), units))
    if kind != "morning" and suggestions:
        sections.append(_block(_NEXT_HEADINGS[kind], [], suggestions))
    return preamble, sections


def _assemble(preamble: list[Item], sections: list[_Section], selected: list[set[int]], closing: list[Item]) -> list[Item]:
    items = list(preamble)
    for section, chosen in zip(sections, selected):
        items.extend(section.head)
        for index, (_, unit) in enumerate(section.units):
            if index in chosen:
                items.extend(unit)
        if len(chosen) < len(section.units):
            items.append(("note", _SECTION_OMISSION.format(count=len(section.units) - len(chosen))))
        items.extend(section.tail)
    return items + closing


def _all_items(preamble: list[Item], sections: list[_Section]) -> list[Item]:
    return _assemble(preamble, sections, [set(range(len(section.units))) for section in sections], [])


def provider_text_length(text: str) -> int:
    """Count UTF-16 code units, the unit push providers use for content limits."""
    return len(text.encode("utf-16-le")) // 2


_MARKDOWN_ESCAPE = re.compile(r"\\([`*_{}\[\]#|!])")


def markdown_html_length(content: str) -> int:
    """Length of the HTML a provider stores after converting this renderer's Markdown.

    Every block is one heading, bold paragraph, or plain paragraph, so the
    conversion adds a fixed tag per block and resolves Markdown escapes.
    """
    total = 0
    for block in content.strip().split("\n\n"):
        line, tags = block.strip(), len("<p></p>")
        for prefix in ("### ", "## ", "# "):
            if line.startswith(prefix):
                line, tags = line[len(prefix):], len("<h2></h2>")
                break
        else:
            if len(line) >= 4 and line.startswith("**") and line.endswith("**"):
                line, tags = line[2:-2], len("<p><strong></strong></p>")
        text = html.unescape(_MARKDOWN_ESCAPE.sub(r"\1", line))
        total += provider_text_length(html.escape(text, quote=True)) + tags + 1
    return total


def _water_fill(room: int, needs: list[int]) -> list[int]:
    """Share room so small sections keep everything and large ones split the rest."""
    caps = [0] * len(needs)
    pending = [index for index, need in enumerate(needs) if need > 0]
    while pending and room > 0:
        share = room // len(pending)
        satisfied = [index for index in pending if needs[index] - caps[index] <= share]
        if not satisfied:
            for index in pending:
                caps[index] += share
            break
        for index in satisfied:
            room -= needs[index] - caps[index]
            caps[index] = needs[index]
        pending = [index for index in pending if index not in satisfied]
    return caps


def _budgeted_items(
    preamble: list[Item], sections: list[_Section], cost: Callable[[list[Item]], int], room: int,
) -> list[Item]:
    """Fit content into room without separating any record or exercise from its notes."""
    closing = [("note", _FULL_REPORT_NOTE)]
    framing = cost(preamble) + cost(closing) + sum(
        cost(section.head) + cost(section.tail)
        + cost([("note", _SECTION_OMISSION.format(count=len(section.units)))])
        for section in sections
    )
    unit_costs = [[cost(unit) for _, unit in section.units] for section in sections]
    orders = [sorted(range(len(section.units)), key=lambda index, units=section.units: (units[index][0], index))
              for section in sections]
    selected = [set() for _ in sections]
    spare = room - framing
    if spare > 0:
        caps = _water_fill(spare, [sum(costs) for costs in unit_costs])
        for costs, order, cap, chosen in zip(unit_costs, orders, caps, selected):
            used = 0
            for index in order:
                if used + costs[index] <= cap:
                    chosen.add(index)
                    used += costs[index]
            spare -= used
        for costs, order, chosen in zip(unit_costs, orders, selected):
            for index in order:
                if index not in chosen and costs[index] <= spare:
                    chosen.add(index)
                    spare -= costs[index]
    items = _assemble(preamble, sections, selected, closing)
    # Framing alone can only exceed the room for a pathological report; keep
    # the highest-priority sections and the closing pointer to the full report.
    while cost(items) > room and sections:
        sections, selected = sections[:-1], selected[:-1]
        items = _assemble(preamble, sections, selected, closing)
    while cost(items) > room and preamble:
        preamble = preamble[:-1]
        items = _assemble(preamble, sections, selected, closing)
    return items


def _markdown_line(kind: str, text: str) -> str | None:
    if kind in {"block_start", "block_end"}:
        return None
    shown = _markdown(text)
    if kind in {"h2", "h3"}:
        return f"{'##' if kind == 'h2' else '###'} {shown}"
    if kind == "strong":
        return f"**{shown}**"
    return shown


def _markdown_report(payload: dict[str, Any], label: str, headline: str, items: list[Item]) -> str:
    lines = [f"# {_markdown(label)} · {_markdown(headline)}", "", _markdown(_subtitle(payload))]
    for kind, text in items:
        line = _markdown_line(kind, text)
        if line is not None:
            lines.extend(["", line])
    return "\n".join(lines) + "\n"


def _html_text(value: Any) -> str:
    parts = re.split(r"([A-Za-z0-9_]{24,})", _plain(value))
    output = []
    for part in parts:
        if re.fullmatch(r"[A-Za-z0-9_]{24,}", part):
            output.append("<wbr>".join(html.escape(part[index:index + 16], quote=True) for index in range(0, len(part), 16)))
        else:
            output.append(html.escape(part, quote=True).replace("\\", "&#92;"))
    return "".join(output)


def _html_line(kind: str, text: str) -> str:
    escape = _html_text
    if kind == "block_start":
        return '<div style="margin:16px 0;padding:14px 16px;border:1px solid #dfe7ed;border-radius:8px;background:#f5f7fa">'
    if kind == "block_end":
        return "</div>"
    if kind == "h2":
        return f'<h2 style="margin:0 0 12px;color:#111827;font-size:18px;line-height:1.45">{escape(text)}</h2>'
    if kind == "h3":
        return f'<h3 style="margin:18px 0 6px;color:#111827;font-size:17px;line-height:1.5">{escape(text)}</h3>'
    if kind == "strong":
        return f'<p style="margin:14px 0 6px;color:#111827;font-size:17px;font-weight:700;line-height:1.5">{escape(text)}</p>'
    if kind == "note":
        return f'<p style="margin:4px 0 10px;color:#475569;font-size:14px;line-height:1.65">{escape(text)}</p>'
    return f'<p style="margin:8px 0;color:#334155;line-height:1.65">{escape(text)}</p>'


def _html_report(payload: dict[str, Any], label: str, headline: str, items: list[Item]) -> str:
    escape = _html_text
    output = [
        '<div style="max-width:680px;margin:0 auto;padding:16px;box-sizing:border-box;'
        'background:#ffffff;color:#111827;font-family:Arial,sans-serif;font-size:16px;'
        'line-height:1.65;overflow-wrap:anywhere;word-break:break-word">',
        f'<p style="margin:0 0 8px;font-size:14px;color:#475569">Vitalis · {escape(label)}</p>',
        f'<h1 style="margin:0 0 10px;font-size:24px;line-height:1.35">{escape(headline)}</h1>',
        f'<p style="margin:0 0 24px;font-size:14px;color:#475569">{escape(_subtitle(payload))}</p>',
    ]
    output.extend(_html_line(kind, text) for kind, text in items)
    output.append("</div>")
    return "\n".join(output)


def _fit_report(
    payload: dict[str, Any], label: str, headline: str, target: ReportTarget,
    content: tuple[list[Item], list[_Section]], limit: int, measure: Callable[[str], int],
) -> str:
    """Render within a channel limit; output is block-joined, so item costs add up exactly."""
    if target == "markdown":
        def cost(items: list[Item]) -> int:
            return sum(measure("\n\n" + line) for kind, text in items
                       if (line := _markdown_line(kind, text)) is not None)
        render = _markdown_report
    else:
        def cost(items: list[Item]) -> int:
            return sum(measure("\n" + _html_line(kind, text)) for kind, text in items)
        render = _html_report
    room = limit - measure(render(payload, label, headline, []))
    return render(payload, label, headline, _budgeted_items(*content, cost, room))


class _HTMLContract(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"div", "p", "h1", "h2", "h3", "strong", "span", "br", "wbr", "ul", "li", "blockquote"}:
            raise ValueError("HTML report contains an unsupported element")
        if any(name not in {"style", "lang"} for name, _ in attrs):
            raise ValueError("HTML report contains an unsupported attribute")
        for name, value in attrs:
            if name == "style" and value and re.search(r"url\s*\(|expression\s*\(|@import", value, re.I):
                raise ValueError("HTML report contains an unsafe style")
        self.tags.append(tag)


def validate_report_content(content: str, template: str) -> None:
    if template not in _MEDIA_TYPES:
        raise ValueError("report template must be markdown or html")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("report content is empty")
    text = content.strip()
    if re.match(r"^(?:```|~~~)", text) and re.search(r"(?:```|~~~)\s*$", text):
        raise ValueError("report content must not be wrapped in a code fence")
    if "\\n" in content or "\\r" in content:
        raise ValueError("report content contains literal escaped newlines")
    try:
        serialized = json.loads(text)
    except ValueError:
        serialized = None
    if isinstance(serialized, str):
        raise ValueError("report content is a second JSON serialization")
    if template == "html":
        if not text.startswith("<") or re.match(r"&lt;", text):
            raise ValueError("HTML template requires unescaped HTML content")
        parser = _HTMLContract()
        parser.feed(content)
        parser.close()
        if not parser.tags:
            raise ValueError("HTML template requires HTML elements")
    elif re.search(r"<(?:/?(?:div|p|h[1-6]|script|style|img|a))\b", content, re.I):
        raise ValueError("Markdown template cannot contain raw report HTML")


def validate_rendered_report(report: RenderedReport) -> None:
    validate_report_content(report.content, report.template)


def render_report(
    report: Any, target: ReportTarget = "markdown", *,
    max_length: int | None = None, measure: Callable[[str], int] = provider_text_length,
) -> RenderedReport:
    """Render one saved report for reading; ``max_length`` fits a channel limit counted by ``measure``.

    The page shows the content the report engine curated (key records, plan,
    changes, training, and the report's own period sections). The public view
    supplies only the header: dates, cutoff, source mode, state, and the
    facts-only/retrospective qualification of what may be shown.
    """
    if target not in _MEDIA_TYPES:
        raise ValueError("report target must be markdown or html")
    if isinstance(report, PublicReportView) or "blocks" in payload_of(report):
        raise ValueError("render a saved report briefing or profile, not its public view")
    view = to_public_report_view(report)
    # Revalidate mutable nested fields before producing transport bytes.
    payload = PublicReportView.model_validate(view.model_dump(mode="json")).model_dump(mode="json")
    content = _report_sections(payload, report_presentation(report, view.kind))
    label = _LABELS[view.kind]
    headline = _plain(view.title)
    render = _markdown_report if target == "markdown" else _html_report
    body = render(payload, label, headline, _all_items(*content))
    if max_length is not None and measure(body) > max_length:
        body = _fit_report(payload, label, headline, target, content, max_length, measure)
    return RenderedReport(
        title=f"Vitalis {label} · {headline}", content=body,
        media_type=_MEDIA_TYPES[target],
    )
