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
from .public_reports import PublicReportView, to_public_report_view


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


def _fact_heading(fact: dict[str, Any]) -> str:
    value = fact.get("value")
    if value is None:
        return str(fact.get("text") or fact.get("label") or "已保存记录")
    label = str(fact.get("label") or "已保存记录")
    aggregation = {"mean": "本期平均", "average": "本期平均", "median": "本期中位数", "latest": "本期末次", "sum": "已记录合计"}.get(fact.get("aggregation"))
    if aggregation:
        label = f"{label}（{aggregation}）"
    if isinstance(value, str):
        shown = value[:8] if fact.get("unit") == "clock" else value
    else:
        shown = _value(value, fact.get("unit") or "单位未记录", fact.get("digits", 1))
    return f"{label}｜{shown}"


def _provenance(item: dict[str, Any]) -> str:
    source = item.get("source")
    if not source:
        return "来源未记录"
    label = {"zepp": "Zepp", "user": "用户明确记录", "user_confirmed": "用户确认", "vitalis": "Vitalis 分析"}.get(source, str(source))
    scope = {"device": "设备记录", "user_fused": "账户汇总", "normalized_daily_record": "本地日记录", "workout": "训练记录"}.get(item.get("source_scope"))
    return f"来源 {label}" + (f" · {scope}" if scope else "") + (f" · 设备 {item['device_id']}" if item.get("device_id") else "")


def _fact_notes(fact: dict[str, Any], block: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    notes = []
    if fact.get("value") is not None and fact.get("text"):
        notes.append(str(fact["text"]))
    for key in ("detail", "gap"):
        if fact.get(key):
            notes.append(str(fact[key]))
    status = {"PARTIAL": "部分记录", "STALE": "记录已过时", "UNKNOWN": "记录未取得", "INSUFFICIENT": "记录不足，暂不比较"}.get(fact.get("status"))
    if status and not fact.get("gap"):
        notes.append(status)
    if fact.get("shadow_only") or fact.get("decision_role") == "shadow":
        notes.append("shadow-only：仅作观察，不进入训练决策")
    comparison = next((item for item in block.get("comparisons") or [] if item.get("type") != "association" and
                       all(item.get(key) == fact.get(key) for key in ("metric", "source", "source_scope", "device_id"))), None)
    if comparison:
        text = _comparison({"comparison": comparison, "unit": fact.get("unit") or "单位未记录", "digits": fact.get("digits", 1)})
        if text:
            notes.append(text)
    if not fact.get("text"):
        qualification = [_provenance(fact)]
        observed = fact.get("observed_at")
        zone = (payload.get("report_context") or {}).get("timezone") or "UTC"
        if observed:
            qualification.append(f"所属日期 {observed}" if len(str(observed)) == 10 else f"观测于 {timestamp_text(observed, zone)}")
        days = fact.get("distinct_days")
        if days is None:
            days = fact.get("effective_days")
        expected = fact.get("expected_days")
        if days is not None:
            qualification.append(f"有效 {days}" + (f"/{expected}" if expected is not None else "") + " 天")
        if fact.get("sample_count") is not None:
            qualification.append(f"{fact['sample_count']} 条样本")
        ratio = fact.get("coverage_ratio")
        if ratio is not None:
            if not math.isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError("report coverage must be finite and within zero to one")
            qualification.append(f"覆盖 {ratio:.0%}")
        if fact.get("as_of") and fact["as_of"] != payload.get("as_of"):
            qualification.append(f"数据截至 {timestamp_text(fact['as_of'], zone)}")
        notes.append(" · ".join(qualification))
    return notes


def _reading_items(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """The same ordered content items feed Markdown and HTML."""
    output = [("paragraph", str(item)) for item in [*(payload.get("summary") or []), *(payload.get("alerts") or [])]]
    for block in sorted(payload["blocks"], key=lambda item: item["priority"]):
        if not (block["facts"] or block["workouts"] or block["interpretation"] or block.get("action")):
            continue
        output.extend([("block_start", ""), ("h2", str(block["title"]))])
        for fact in block["facts"]:
            output.append(("strong" if fact.get("value") is not None else "paragraph", _fact_heading(fact)))
            output.extend(("note", note) for note in _fact_notes(fact, block, payload))
        for workout in block["workouts"]:
            output.append(("h3", str(workout.get("title") or "训练记录")))
            info = [str(workout["date"])]
            if workout.get("started_at"):
                info.append(timestamp_text(workout["started_at"], (payload.get("report_context") or {}).get("timezone") or "UTC", short=True))
            if workout.get("duration_minutes") is not None:
                info.append(_value(workout["duration_minutes"], "min"))
            if workout.get("source"):
                info.append(_provenance(workout))
            output.append(("paragraph", " · ".join(info)))
            output.extend(("paragraph", str(item)) for item in workout.get("facts") or [])
            for exercise in workout.get("exercises") or []:
                output.append(("strong", str(exercise.get("name") or "未识别动作")))
                output.extend(("paragraph", line) for line in _exercise_lines(exercise))
                if exercise.get("comparison"):
                    reference = f"对照 {exercise['reference_date']} · " if exercise.get("reference_date") else ""
                    output.append(("note", reference + exercise["comparison"]))
        output.extend(("paragraph", str(item)) for item in block["interpretation"])
        output.extend(("note", str(item)) for item in block.get("limitations") or [])
        if block.get("action"):
            label = {"morning": "今天的安排", "evening": "明日重点", "weekly": "下周重点", "monthly": "下月重点", "daily": "下一步"}[payload["kind"]]
            output.append(("h3", label))
            output.extend(("paragraph", str(item)) for item in payload.get("suggestions") or [block["action"]])
        output.append(("block_end", ""))
    return output


def _markdown_report(payload: dict[str, Any], label: str, headline: str) -> str:
    lines = [f"# {_markdown(label)} · {_markdown(headline)}", "", _markdown(_subtitle(payload))]
    for kind, text in _reading_items(payload):
        if kind in {"block_start", "block_end"}:
            continue
        shown = _markdown(text)
        if kind in {"h2", "h3"}:
            shown = f"{'##' if kind == 'h2' else '###'} {shown}"
        elif kind == "strong":
            shown = f"**{shown}**"
        lines.extend(["", shown])
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
    output = [
        '<div style="max-width:680px;margin:0 auto;padding:16px;box-sizing:border-box;'
        'background:#ffffff;color:#111827;font-family:Arial,sans-serif;font-size:16px;'
        'line-height:1.65;overflow-wrap:anywhere;word-break:break-word">',
        f'<p style="margin:0 0 8px;font-size:14px;color:#475569">Vitalis · {escape(label)}</p>',
        f'<h1 style="margin:0 0 10px;font-size:24px;line-height:1.35">{escape(headline)}</h1>',
        f'<p style="margin:0 0 24px;font-size:14px;color:#475569">{escape(_subtitle(payload))}</p>',
    ]
    for kind, text in _reading_items(payload):
        if kind == "block_start":
            output.append('<div style="margin:16px 0;padding:14px 16px;border:1px solid #dfe7ed;border-radius:8px;background:#f5f7fa">')
        elif kind == "block_end":
            output.append("</div>")
        elif kind == "h2":
            output.append(f'<h2 style="margin:0 0 12px;color:#111827;font-size:18px;line-height:1.45">{escape(text)}</h2>')
        elif kind == "h3":
            output.append(f'<h3 style="margin:18px 0 6px;color:#111827;font-size:17px;line-height:1.5">{escape(text)}</h3>')
        elif kind == "strong":
            output.append(f'<p style="margin:14px 0 6px;color:#111827;font-size:17px;font-weight:700;line-height:1.5">{escape(text)}</p>')
        elif kind == "note":
            output.append(f'<p style="margin:4px 0 10px;color:#475569;font-size:14px;line-height:1.65">{escape(text)}</p>')
        else:
            output.append(f'<p style="margin:8px 0;color:#334155;line-height:1.65">{escape(text)}</p>')
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


def render_report(report: Any, target: ReportTarget = "markdown") -> RenderedReport:
    if target not in _MEDIA_TYPES:
        raise ValueError("report target must be markdown or html")
    # Every channel enters through the same public projection, regardless of
    # whether the caller holds a saved profile, briefing, or serialized payload.
    if not isinstance(report, PublicReportView):
        report = to_public_report_view(report)
    # Revalidate mutable nested fields before producing transport bytes.
    payload = PublicReportView.model_validate(report.model_dump(mode="json")).model_dump(mode="json")
    label = _LABELS[report.kind]
    headline = _plain(report.title)
    content = _markdown_report(payload, label, headline) if target == "markdown" else _html_report(payload, label, headline)
    return RenderedReport(
        title=f"Vitalis {label} · {headline}", content=content,
        media_type=_MEDIA_TYPES[target],
    )
