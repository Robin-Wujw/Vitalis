"""Render already-computed report projections without I/O or health algorithms."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import html
from html.parser import HTMLParser
import json
import math
import re
from typing import Any, Literal

from .report_formatting import minutes_text, number, timestamp_text


RENDERER_VERSION = "2.0"
ReportTarget = Literal["markdown", "html"]
_MEDIA_TYPES = {"markdown": "text/markdown", "html": "text/html"}
_LABELS = {"morning": "晨报", "evening": "日报", "weekly": "周报", "monthly": "月报"}
_UNITS = {
    "min": "分钟", "minutes": "分钟", "steps": "步", "steps/day": "步/日",
    "ms": "毫秒", "bpm": "次/分", "km": "公里", "m": "米", "kcal": "千卡",
    "sessions": "次", "days": "天", "sets": "组", "sessions/week": "次/周",
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
    start = payload.get("period_start") or payload.get("date")
    end = payload.get("period_end") or payload.get("date")
    period = str(start) if start == end else f"{start}—{end}"
    context = payload.get("report_context") or {}
    cutoff = payload.get("as_of") or context.get("as_of")
    if cutoff:
        shown = timestamp_text(cutoff, context.get("timezone") or "UTC")
        if re.search(r" [+-]\d{4}$", shown):
            shown = shown.rsplit(" ", 1)[0]
        period += f" · 数据截至 {shown}"
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


def _sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [section for section in payload.get("sections") or [] if section.get("display")]


def _markdown_report(payload: dict[str, Any], label: str, headline: str) -> str:
    lines = [f"# {_markdown(label)} · {_markdown(headline)}", "", _markdown(_subtitle(payload))]
    for alert in payload.get("alerts") or []:
        lines.extend(["", _markdown(alert)])
    metrics = payload.get("metrics") or []
    if metrics:
        lines.extend(["", "## 关键记录"])
        for metric in metrics:
            value = metric.get("value")
            shown = _value(value, metric.get("unit", ""), metric.get("digits", 0)) if value is not None else None
            heading = _markdown(metric.get("label"))
            if shown is not None:
                heading += "｜" + _markdown(shown)
            lines.extend(["", f"**{heading}**"])
            for note in (_comparison(metric), metric.get("detail"), metric.get("gap")):
                if note:
                    lines.extend(["", _markdown(note)])
    suggestions = payload.get("suggestions") or []
    if payload.get("period") == "morning" and suggestions:
        lines.extend(["", "## 今天的重点"])
        for suggestion in suggestions:
            lines.extend(["", _markdown(suggestion)])
    findings = payload.get("findings") or []
    if findings:
        heading = {"morning": "昨夜与今天", "evening": "今天的变化", "weekly": "本周变化", "monthly": "本月变化"}.get(payload.get("period"), "变化")
        lines.extend(["", f"## {heading}"])
        for finding in findings:
            lines.extend(["", _markdown(finding)])
    for workout in payload.get("training") or []:
        lines.extend(["", f"## {_markdown(workout.get('title') or '训练记录')}"])
        info = [str(workout.get("date"))]
        if workout.get("started_at"):
            info.append(timestamp_text(workout["started_at"], (payload.get("report_context") or {}).get("timezone") or "UTC", short=True))
        if workout.get("duration_minutes") is not None:
            info.append(_value(workout["duration_minutes"], "min"))
        lines.extend(["", _markdown(" · ".join(info))])
        for fact in workout.get("facts") or []:
            lines.extend(["", _markdown(fact)])
        for exercise in workout.get("exercises") or []:
            lines.extend(["", f"**{_markdown(exercise.get('name') or '未识别动作')}**"])
            for line in _exercise_lines(exercise):
                lines.extend(["", _markdown(line)])
            if exercise.get("comparison"):
                reference = f"对照 {exercise['reference_date']} · " if exercise.get("reference_date") else ""
                lines.extend(["", _markdown(reference + exercise["comparison"])])
    for section in _sections(payload):
        contents = [*(section.get("facts") or []), *(section.get("interpretation") or []), *(section.get("limitations") or [])]
        if not contents:
            continue
        lines.extend(["", f"## {_markdown(section.get('title'))}"])
        for item in contents:
            lines.extend(["", _markdown(item)])
    suggestions = payload.get("suggestions") or []
    if suggestions and payload.get("period") != "morning":
        heading = {"evening": "今晚或下一次的重点", "weekly": "下周重点", "monthly": "下月重点"}.get(payload.get("period"), "下一步")
        lines.extend(["", f"## {heading}"])
        for suggestion in suggestions:
            lines.extend(["", _markdown(suggestion)])
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


def _html_report(payload: dict[str, Any], label: str, headline: str) -> str:
    escape = _html_text
    paragraph = lambda value: f'<p style="margin:8px 0;color:#334155;line-height:1.65">{escape(value)}</p>'
    heading = lambda value: f'<h2 style="margin:24px 0 10px;color:#111827;font-size:18px;line-height:1.45">{escape(value)}</h2>'
    output = [
        '<div style="max-width:680px;margin:0 auto;padding:16px;box-sizing:border-box;'
        'background:#ffffff;color:#111827;font-family:Arial,sans-serif;font-size:16px;'
        'line-height:1.65;overflow-wrap:anywhere;word-break:break-word">',
        f'<p style="margin:0 0 8px;font-size:14px;color:#475569">Vitalis · {escape(label)}</p>',
        f'<h1 style="margin:0 0 10px;font-size:24px;line-height:1.35">{escape(headline)}</h1>',
        f'<p style="margin:0 0 24px;font-size:14px;color:#475569">{escape(_subtitle(payload))}</p>',
    ]
    output.extend(paragraph(alert) for alert in payload.get("alerts") or [])
    metrics = payload.get("metrics") or []
    if metrics:
        output.append(heading("关键记录"))
        for metric in metrics:
            output.append('<div style="margin:12px 0;padding:14px 16px;border:1px solid #dfe7ed;border-radius:8px;background:#f5f7fa">')
            output.append(f'<p style="margin:0 0 4px;font-size:14px;color:#475569">{escape(metric.get("label"))}</p>')
            if metric.get("value") is not None:
                shown = _value(metric["value"], metric.get("unit", ""), metric.get("digits", 0))
                output.append(f'<p style="margin:0;font-size:28px;font-weight:700;line-height:1.4;color:#111827">{escape(shown)}</p>')
            for note in (_comparison(metric), metric.get("detail"), metric.get("gap")):
                if note:
                    output.append(f'<p style="margin:6px 0 0;font-size:14px;color:#475569">{escape(note)}</p>')
            output.append("</div>")
    suggestions = payload.get("suggestions") or []
    if payload.get("period") == "morning" and suggestions:
        output.append(heading("今天的重点"))
        output.extend(paragraph(suggestion) for suggestion in suggestions)
    findings = payload.get("findings") or []
    if findings:
        output.append(heading({"morning": "昨夜与今天", "evening": "今天的变化", "weekly": "本周变化", "monthly": "本月变化"}.get(payload.get("period"), "变化")))
        output.extend(paragraph(finding) for finding in findings)
    for workout in payload.get("training") or []:
        output.append(heading(workout.get("title") or "训练记录"))
        info = [str(workout.get("date"))]
        if workout.get("started_at"):
            info.append(timestamp_text(workout["started_at"], (payload.get("report_context") or {}).get("timezone") or "UTC", short=True))
        if workout.get("duration_minutes") is not None:
            info.append(_value(workout["duration_minutes"], "min"))
        output.append(paragraph(" · ".join(info)))
        output.extend(paragraph(fact) for fact in workout.get("facts") or [])
        for exercise in workout.get("exercises") or []:
            output.append(f'<h3 style="margin:18px 0 6px;font-size:17px;line-height:1.5">{escape(exercise.get("name") or "未识别动作")}</h3>')
            output.extend(paragraph(line) for line in _exercise_lines(exercise))
            if exercise.get("comparison"):
                reference = f"对照 {exercise['reference_date']} · " if exercise.get("reference_date") else ""
                output.append(paragraph(reference + exercise["comparison"]))
    for section in _sections(payload):
        contents = [*(section.get("facts") or []), *(section.get("interpretation") or []), *(section.get("limitations") or [])]
        if contents:
            output.append(heading(section.get("title")))
            output.extend(paragraph(item) for item in contents)
    suggestions = payload.get("suggestions") or []
    if suggestions and payload.get("period") != "morning":
        output.append(heading({"evening": "今晚或下一次的重点", "weekly": "下周重点", "monthly": "下月重点"}.get(payload.get("period"), "下一步")))
        output.extend(paragraph(suggestion) for suggestion in suggestions)
    output.append("</div>")
    return "\n".join(output)


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


def render_report(briefing: Any, target: ReportTarget = "markdown") -> RenderedReport:
    if target not in _MEDIA_TYPES:
        raise ValueError("report target must be markdown or html")
    payload = briefing.model_dump(mode="json") if hasattr(briefing, "model_dump") else dict(briefing)
    period = payload.get("period") or ("morning" if "decision_action" in payload else None)
    if period not in _LABELS:
        raise ValueError("report briefing has an unsupported period")
    payload["period"] = period
    label = _LABELS[period]
    headline = _plain(payload.get("headline")) or "已记录数据回顾"
    content = _markdown_report(payload, label, headline) if target == "markdown" else _html_report(payload, label, headline)
    return RenderedReport(
        title=f"Vitalis {label} · {headline}", content=content,
        media_type=_MEDIA_TYPES[target],
    )
