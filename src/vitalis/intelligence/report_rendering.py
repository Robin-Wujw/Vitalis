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


def _fact_comparison(fact: dict[str, Any], block: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in block.get("comparisons") or [] if item.get("type") != "association" and
                 all(item.get(key) == fact.get(key) for key in ("metric", "source", "source_scope", "device_id"))), None)


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
    comparison = _fact_comparison(fact, block)
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


Item = tuple[str, str]
# Selection order when a channel budget applies: interpretation first, then
# measured facts and workouts in reading order, caveats, and absent signals.
_UNIT_RANKS = {"interpretation": 0, "fact": 1, "workout": 1, "limitation": 2, "unobserved": 3}
_SECTION_OMISSION = "推送篇幅有限，本节另有 {count} 项未展开。"
_FULL_REPORT_NOTE = "完整报告可通过 Vitalis API 或 Hermes 查看。"


@dataclass(frozen=True)
class _Section:
    """A block split into framing that is always shown and atomic optional units."""

    head: list[Item]
    units: list[tuple[int, list[Item]]]
    tail: list[Item]


def _fact_items(fact: dict[str, Any], block: dict[str, Any], payload: dict[str, Any]) -> list[Item]:
    items = [("strong" if fact.get("value") is not None else "paragraph", _fact_heading(fact))]
    items.extend(("note", note) for note in _fact_notes(fact, block, payload))
    return items


def _workout_items(workout: dict[str, Any], payload: dict[str, Any]) -> list[Item]:
    items = [("h3", str(workout.get("title") or "训练记录"))]
    info = [str(workout["date"])]
    if workout.get("started_at"):
        info.append(timestamp_text(workout["started_at"], (payload.get("report_context") or {}).get("timezone") or "UTC", short=True))
    if workout.get("duration_minutes") is not None:
        info.append(_value(workout["duration_minutes"], "min"))
    if workout.get("source"):
        info.append(_provenance(workout))
    items.append(("paragraph", " · ".join(info)))
    items.extend(("paragraph", str(item)) for item in workout.get("facts") or [])
    for exercise in workout.get("exercises") or []:
        items.append(("strong", str(exercise.get("name") or "未识别动作")))
        items.extend(("paragraph", line) for line in _exercise_lines(exercise))
        if exercise.get("comparison"):
            reference = f"对照 {exercise['reference_date']} · " if exercise.get("reference_date") else ""
            items.append(("note", reference + exercise["comparison"]))
    return items


def _unobserved(fact: dict[str, Any], block: dict[str, Any]) -> bool:
    """A fact without value, explanation, gap, or comparison only needs its name."""
    return (
        fact.get("value") is None
        and not any(fact.get(key) for key in ("text", "detail", "gap"))
        and _fact_comparison(fact, block) is None
    )


def _sections(payload: dict[str, Any], *, collapse_unobserved: bool = False) -> tuple[list[Item], list[_Section]]:
    preamble = [("paragraph", str(item)) for item in [*(payload.get("summary") or []), *(payload.get("alerts") or [])]]
    sections = []
    for block in sorted(payload["blocks"], key=lambda item: item["priority"]):
        if not (block["facts"] or block["workouts"] or block["interpretation"] or block.get("action")):
            continue
        units: list[tuple[int, list[Item]]] = []
        unobserved = []
        for fact in block["facts"]:
            if collapse_unobserved and _unobserved(fact, block):
                unobserved.append(_plain(fact.get("label") or "已保存记录"))
            else:
                units.append((_UNIT_RANKS["fact"], _fact_items(fact, block, payload)))
        if unobserved:
            labels = "、".join(dict.fromkeys(unobserved))
            units.append((_UNIT_RANKS["unobserved"], [("note", f"暂无可用记录：{labels}")]))
        units.extend((_UNIT_RANKS["workout"], _workout_items(workout, payload)) for workout in block["workouts"])
        units.extend((_UNIT_RANKS["interpretation"], [("paragraph", str(item))]) for item in block["interpretation"])
        units.extend((_UNIT_RANKS["limitation"], [("note", str(item))]) for item in block.get("limitations") or [])
        tail: list[Item] = []
        if block.get("action"):
            label = {"morning": "今天的安排", "evening": "明日重点", "weekly": "下周重点", "monthly": "下月重点", "daily": "下一步"}[payload["kind"]]
            tail.append(("h3", label))
            tail.extend(("paragraph", str(item)) for item in payload.get("suggestions") or [block["action"]])
        tail.append(("block_end", ""))
        sections.append(_Section([("block_start", ""), ("h2", str(block["title"]))], units, tail))
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


def _reading_items(payload: dict[str, Any]) -> list[Item]:
    """The same ordered content items feed Markdown and HTML."""
    preamble, sections = _sections(payload)
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


def _budgeted_items(payload: dict[str, Any], cost: Callable[[list[Item]], int], room: int) -> list[Item]:
    """Fit content into room without separating any fact from its notes."""
    preamble, sections = _sections(payload, collapse_unobserved=True)
    selected = [set(range(len(section.units))) for section in sections]
    items = _assemble(preamble, sections, selected, [])
    if cost(items) <= room:
        return items
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


def _markdown_report(payload: dict[str, Any], label: str, headline: str, items: list[Item] | None = None) -> str:
    lines = [f"# {_markdown(label)} · {_markdown(headline)}", "", _markdown(_subtitle(payload))]
    for kind, text in (_reading_items(payload) if items is None else items):
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


def _html_report(payload: dict[str, Any], label: str, headline: str, items: list[Item] | None = None) -> str:
    escape = _html_text
    output = [
        '<div style="max-width:680px;margin:0 auto;padding:16px;box-sizing:border-box;'
        'background:#ffffff;color:#111827;font-family:Arial,sans-serif;font-size:16px;'
        'line-height:1.65;overflow-wrap:anywhere;word-break:break-word">',
        f'<p style="margin:0 0 8px;font-size:14px;color:#475569">Vitalis · {escape(label)}</p>',
        f'<h1 style="margin:0 0 10px;font-size:24px;line-height:1.35">{escape(headline)}</h1>',
        f'<p style="margin:0 0 24px;font-size:14px;color:#475569">{escape(_subtitle(payload))}</p>',
    ]
    output.extend(_html_line(kind, text) for kind, text in (_reading_items(payload) if items is None else items))
    output.append("</div>")
    return "\n".join(output)


def _fit_report(
    payload: dict[str, Any], label: str, headline: str, target: ReportTarget,
    limit: int, measure: Callable[[str], int],
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
    return render(payload, label, headline, _budgeted_items(payload, cost, room))


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
    """Render one report; ``max_length`` fits a channel limit counted by ``measure``."""
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
    if max_length is not None and measure(content) > max_length:
        content = _fit_report(payload, label, headline, target, max_length, measure)
    return RenderedReport(
        title=f"Vitalis {label} · {headline}", content=content,
        media_type=_MEDIA_TYPES[target],
    )
