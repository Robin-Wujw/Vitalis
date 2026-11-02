"""Pure presentation of saved chart values; no I/O, health inference or resampling.

SVG uses absolute instants for horizontal geometry and local time for labels.
Different units have separate scales and different sources never share a line.
The push fragment accepts only already-aggregated preview points.  A standalone
page adds a fixed, local-only chart selector; no user value enters JavaScript.
"""
from __future__ import annotations

import html
import math
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_PALETTE = ("#3f6d8f", "#a66543", "#477d70", "#78658f", "#9a6077", "#7c8543", "#527b83", "#806b56")
_INK, _MUTED, _RULE = "#292620", "#6b6459", "#e4dfd4"
_UNKNOWN = "#dce3e8"
_STAGE = {
    "awake": ("清醒", "#b47755"), "rem": ("REM", "#a87b9c"),
    "light": ("浅睡", "#8fa9bd"), "deep": ("深睡", "#42618c"),
    "unknown": ("未知", _UNKNOWN),
}
_AGGREGATION = {"observed": "原始观测", "median": "已保存桶中位数", "mean": "已保存桶均值", "last": "每桶最后观测", "sum": "已保存桶累计值"}
_KINDS = {"line", "scatter", "bars", "stages", "body_map"}
_BODY_NOTE = "训练接触估算，非实测肌肉受力；已记录参与组数不是有效组数，各部位不可相加。"
_SIDE = {"left": "左侧", "right": "右侧", "bilateral": "双侧", "unknown": "侧别未知"}
_ROUTE_STATE = {"available": "可用", "missing": "不可用", "unknown": "状态未知"}
_ROUTE_NOTE = "来源独立展示，非融合；对比使用相同时间轴，同单位共轴，不同单位分面。"


def _payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json")
        if isinstance(result, Mapping):
            return result
    raise ValueError("chart input must be a mapping or a model with model_dump")


def _plain(value: Any) -> str:
    text = str(value if value is not None else "")
    # XML 1.0 disallows control characters and unpaired Unicode surrogates.
    text = "".join(c for c in text if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
    return " ".join(text.split())


def _escape(value: Any) -> str:
    return html.escape(_plain(value), quote=True).replace("\\", "&#92;")


def _numeric(value: Any, *, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("chart measurements must be finite numeric values")  # noqa: TRY004 - stable validation error
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValueError("chart measurements must be finite numeric values")
    return value


def _number(value: float | None) -> str:
    if value is None:
        return "未知"
    # Retain precision of saved values in tooltips, rather than rounding facts.
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _position(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("chart coordinates must be finite")
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _instant(value: Any) -> float:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("chart time requires an explicit timezone and valid instant") from exc


def _rows(payload: Mapping, key: str, maximum: int) -> list[Mapping]:
    result = payload.get(key) or []
    if not isinstance(result, (list, tuple)) or len(result) > maximum or any(not isinstance(row, Mapping) for row in result):
        raise ValueError(f"invalid or oversized chart {key}")
    return list(result)


def _routes(payload: Mapping) -> list[Mapping]:
    rows = _rows(payload, "routes", 64)
    ids = [row.get("id") for row in rows]
    if any(not isinstance(value, str) or not value or value == "all" for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("chart route ids must be unique non-empty strings other than all")
    return rows


def _route_status(route: Mapping) -> str:
    state = route.get("state", "unknown")
    parts = [route.get("label") or route.get("id"), _ROUTE_STATE.get(state, "状态未知")]
    if state == "available":
        parts.append(f'已保存样本数 {route.get("sample_count", 0)}')
    else:
        parts.append("无可用数据，不以其它来源替代")
    if route.get("reason"):
        parts.append(route["reason"])
    return " · ".join(_plain(part) for part in parts)


def _route_view(payload: Mapping, route_id: str | None = None) -> Mapping:
    """Select saved geometry only; never infer provenance or measurement facts."""
    rows = _routes(payload)
    selected = payload.get("selected_route", "all") if route_id is None else route_id
    if selected == "all":
        return {**payload, "selected_route": selected} if rows else payload
    route = next((row for row in rows if row["id"] == selected), None)
    if route is None:
        raise ValueError("selected chart route is not present in routes")
    view = {**payload, "selected_route": selected}
    available = route.get("state") == "available"
    for key in ("series", "preview_series", "bands", "regions"):
        view[key] = [row for row in payload.get(key) or [] if available and row.get("route_id") == selected]
    view["intervals"] = [row for row in payload.get("intervals") or []
                         if (available and row.get("route_id") == selected)
                         or (row.get("source") == "user" and row.get("category") == "annotation")]
    # The reference has no route attribution. It cannot be borrowed by a
    # selected device/account, even when its numeric range looks plausible.
    view["reference"] = None
    view["source_summaries"] = [row for row in payload.get("source_summaries") or [] if row.get("route_id") == selected]
    view["summary"] = [_route_status(route), *[row.get("text", "") for row in view["source_summaries"]]]
    view["state"] = ("partial" if payload.get("state") == "partial" else "available") if available else "unavailable"
    return view


def _default_route(payload: Mapping, routes: list[Mapping]) -> str:
    selected = payload.get("selected_route", "all")
    if selected != "all":
        if not any(route["id"] == selected for route in routes):
            raise ValueError("selected chart route is not present in routes")
        return selected
    account = next((route for route in routes if route["id"] == "zepp:account" and route.get("state") == "available"), None)
    device = next((route for route in routes if route.get("kind") == "device" and route.get("state") == "available"), None)
    unknown = next((route for route in routes if route.get("kind") == "unknown" and route.get("state") == "available"), None)
    available = next((route for route in routes if route.get("state") == "available"), None)
    choice = account or device or unknown or available or next((route for route in routes if route["id"] == "zepp:account"), None)
    return choice["id"] if choice else "all"


def _source_group(row: Mapping, css: str) -> str:
    route = row.get("route_id")
    attribute = f' data-source-route="{_escape(route)}"' if route is not None else ""
    return f'<g class="{css}"{attribute}>'


@dataclass(frozen=True)
class _Point:
    time: float
    value: float | None
    count: int
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class _Series:
    fields: Mapping
    points: tuple[_Point, ...]
    color: str

    @property
    def unit(self) -> str:
        return _plain(self.fields.get("unit"))


def _series(rows: list[Mapping], routes: list[Mapping]) -> list[_Series]:
    output = []
    route_indices = {route["id"]: index for index, route in enumerate(routes)}
    route_series = {}
    for index, row in enumerate(rows):
        points = []
        for point in _rows(row, "points", 3000):
            count = point.get("sample_count", 0)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("chart sample_count must be a non-negative integer")
            lower, upper = _numeric(point.get("minimum")), _numeric(point.get("maximum"))
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("chart minimum must not exceed maximum")
            points.append(_Point(_instant(point.get("timestamp")), _numeric(point.get("value")), count, lower, upper))
        route = row.get("route_id")
        offset = route_series.get(route, 0)
        color_index = route_indices[route] + offset * max(1, len(routes)) if route in route_indices else index
        route_series[route] = offset + 1
        output.append(_Series(row, tuple(sorted(points, key=lambda point: point.time)), _PALETTE[color_index % len(_PALETTE)]))
    return output


@dataclass(frozen=True)
class _Chart:
    fields: Mapping
    zone: ZoneInfo
    start: float
    end: float
    series: tuple[_Series, ...]
    gap: float | None

    def local(self, instant: float) -> str:
        return datetime.fromtimestamp(instant, self.zone).isoformat(sep=" ", timespec="seconds")

    def x(self, instant: float, left: float, width: float) -> float:
        return left + (instant - self.start) / (self.end - self.start) * width


def _chart(value: Any, *, preview: bool = False) -> _Chart:
    payload = _route_view(_payload(value))
    if payload.get("kind") not in _KINDS:
        raise ValueError("unsupported chart kind")
    try:
        zone = ZoneInfo(payload.get("timezone", ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("chart timezone must be a valid IANA timezone") from exc
    start, end = _instant(payload.get("period_start")), _instant(payload.get("period_end"))
    if end <= start:
        raise ValueError("chart period must increase")
    resolution = _numeric(payload.get("resolution_seconds"))
    if resolution is not None and resolution <= 0:
        raise ValueError("chart resolution must be positive")
    series = _series(_rows(payload, "preview_series" if preview else "series", 48), _routes(payload))
    for interval in _rows(payload, "intervals", 3000):
        if _instant(interval.get("start")) >= _instant(interval.get("end")):
            raise ValueError("chart interval must increase")
    for band in _rows(payload, "bands", 8):
        for point in _rows(band, "points", 366):
            _instant(point.get("timestamp"))
            lower, upper = _numeric(point.get("lower")), _numeric(point.get("upper"))
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("chart band lower must not exceed upper")
    reference = payload.get("reference")
    if reference:
        if not isinstance(reference, Mapping):
            raise ValueError("chart reference must be a mapping")
        lower, upper = _numeric(reference.get("lower")), _numeric(reference.get("upper"))
        _numeric(reference.get("median"))
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("chart reference lower must not exceed upper")
    for region in _rows(payload, "regions", 128):
        for key in ("direct_sets", "indirect_sets", "observed_sets"):
            number = _numeric(region.get(key))
            if number is not None and number < 0:
                raise ValueError("chart region counts must be non-negative")
    return _Chart(payload, zone, start, end, tuple(series), resolution * 1.5 if resolution else None)


def _text(text: Any, x: float, y: float, *, size: int = 12, color: str = _INK, css: str = "", anchor: str = "start") -> str:
    return (f'<text x="{_position(x)}" y="{_position(y)}" font-size="{size}" fill="{color}"'
            f' text-anchor="{anchor}"' + (f' class="{css}"' if css else "") + f'>{_escape(text)}</text>')


def _paragraph(text: Any, x: float, y: float, width: float, *, size: int = 12, color: str = _MUTED) -> tuple[str, float]:
    lines = textwrap.wrap(_plain(text), max(12, int(width / size)), break_long_words=True, break_on_hyphens=False) or [""]
    return "".join(_text(line, x, y + index * (size + 6), size=size, color=color) for index, line in enumerate(lines)), len(lines) * (size + 6)


def _caption(series: _Series, *, detailed: bool = True) -> str:
    fields = series.fields
    parts = [fields.get("label") or fields.get("key") or "观测", series.unit, fields.get("device_label")]
    if detailed or not fields.get("device_label"):
        parts.extend((f'来源 {fields.get("source") or "未标记"}', fields.get("source_scope"), fields.get("device_id")))
    parts.append(fields.get("qualification"))
    return " · ".join(_plain(part) for part in dict.fromkeys(parts) if part)


def _tooltip(chart: _Chart, series: _Series, point: _Point) -> str:
    value = "缺失" if point.value is None else f"{_number(point.value)} {series.unit}".strip()
    parts = [_caption(series), chart.local(point.time), value, f"样本数 {point.count}"]
    if point.minimum is not None or point.maximum is not None:
        parts.append(f"原始范围 {_number(point.minimum)}—{_number(point.maximum)} {series.unit}")
    if series.fields.get("source_field"):
        parts.append(f'字段 {series.fields["source_field"]}')
    parts.append(_AGGREGATION.get(series.fields.get("aggregation", "observed"), "聚合口径未标记"))
    return " · ".join(parts)


def _scale(values: list[float], *, zero: bool = False) -> tuple[float, float, list[float]]:
    values = list(values)
    if not values:
        return 0, 1, []
    if zero and all(value == 0 for value in values):
        return 0, 1, [0, 1]
    if zero:
        values.append(0)
    lower, upper = min(values), max(values)
    span = upper - lower
    if not math.isfinite(span):
        raise ValueError("chart numeric range is too large")
    padding = span * 0.05 if span else max(abs(lower) * 0.05, 1)
    lower, upper = lower - padding, upper + padding
    if zero and min(values) >= 0:
        lower = 0
    if zero and max(values) <= 0:
        upper = 0
    rough = (upper - lower) / 4
    if not math.isfinite(rough) or rough <= 0:
        raise ValueError("chart numeric range cannot be displayed")
    magnitude = 10 ** math.floor(math.log10(rough))
    step = next((factor * magnitude for factor in (1, 2, 2.5, 5, 10) if factor * magnitude >= rough), 10 * magnitude)
    first, last = math.floor(lower / step), math.ceil(upper / step)
    ticks = [index * step for index in range(first, last + 1)]
    return ticks[0], ticks[-1], ticks


def _x_axis(chart: _Chart, left: float, top: float, width: float, height: float) -> str:
    result = [f'<line x1="{_position(left)}" x2="{_position(left + width)}" y1="{_position(top + height)}" y2="{_position(top + height)}" stroke="{_RULE}"/>']
    count = 4 if width >= 500 else 2
    for index in range(count + 1):
        time = chart.start + (chart.end - chart.start) * index / count
        local = datetime.fromtimestamp(time, chart.zone)
        label = local.strftime("%m-%d %H:%M") if chart.end - chart.start < 3 * 86400 else local.strftime("%Y-%m-%d")
        x = left + width * index / count
        result.append(_text(label, x, top + height + 20, size=11, color=_MUTED, css="x-tick", anchor="start" if index == 0 else "end" if index == count else "middle"))
    return "".join(result)


def _segments(points: tuple[_Point, ...], chart: _Chart) -> list[list[_Point]]:
    segments, current = [], []
    for point in points:
        if point.value is None or not chart.start <= point.time <= chart.end:
            if current:
                segments.append(current)
            current = []
            continue
        if current and chart.gap is not None and point.time - current[-1].time > chart.gap:
            segments.append(current)
            current = []
        current.append(point)
    if current:
        segments.append(current)
    return segments


def _numeric_svg(chart: _Chart, width: int, y: float, preferred_height: int) -> tuple[str, float]:
    payload = chart.fields
    units = list(dict.fromkeys(s.unit for s in chart.series)) or [_plain(payload.get("unit"))]
    if (payload.get("bands") or payload.get("reference")) and _plain(payload.get("unit")) not in units:
        units.append(_plain(payload.get("unit")))
    left, plot_width = 82.0, width - 110.0
    plot_height = max(155, min(300, preferred_height - 220))
    output = []
    for unit in units:
        selected = [series for series in chart.series if series.unit == unit]
        values = [point.value for series in selected for point in series.points if point.value is not None and chart.start <= point.time <= chart.end]
        band_rows = payload.get("bands") or [] if unit == _plain(payload.get("unit")) else []
        reference = payload.get("reference") if unit == _plain(payload.get("unit")) else None
        for band in band_rows:
            for point in band.get("points") or []:
                if chart.start <= _instant(point["timestamp"]) <= chart.end:
                    values.extend(point[key] for key in ("lower", "upper") if point.get(key) is not None)
        if reference:
            values.extend(reference[key] for key in ("lower", "upper", "median") if reference.get(key) is not None)
        lower, upper, ticks = _scale(values, zero=payload["kind"] == "bars")
        top = y + 24

        def py(value, top=top, upper=upper, lower=lower):
            return top + (upper - value) / (upper - lower) * plot_height

        output.append('<g class="unit-panel">')
        output.append(_text(unit or "单位未标记", left, y + 7, size=12))
        for tick in ticks:
            line_y = py(tick)
            output.append(f'<line x1="{left:g}" x2="{_position(left + plot_width)}" y1="{_position(line_y)}" y2="{_position(line_y)}" stroke="{_RULE}"/>')
            output.append(_text(f"{tick:.8g}", left - 10, line_y + 4, size=11, css="y-tick", color=_MUTED, anchor="end"))
        if reference:
            a, b, median = reference.get("lower"), reference.get("upper"), reference.get("median")
            if a is not None and b is not None:
                output.append(f'<rect class="reference-band" x="{left:g}" y="{_position(py(b))}" width="{_position(plot_width)}" height="{_position(py(a) - py(b))}" fill="#e8edf0"><title>{_escape(reference.get("label"))}: {_escape(_number(a))}—{_escape(_number(b))}</title></rect>')
            if median is not None:
                output.append(f'<line class="reference-median" x1="{left:g}" x2="{_position(left + plot_width)}" y1="{_position(py(median))}" y2="{_position(py(median))}" stroke="#7c8892" stroke-dasharray="5 4"><title>{_escape(reference.get("label"))}: {_escape(_number(median))}</title></line>')
        for interval in payload.get("intervals") or []:
            start, end = max(chart.start, _instant(interval["start"])), min(chart.end, _instant(interval["end"]))
            if end > start:
                output.append(_source_group(interval, "annotation-source"))
                output.append(f'<rect class="annotation" x="{_position(chart.x(start, left, plot_width))}" y="{_position(top)}" width="{_position((end - start) / (chart.end - chart.start) * plot_width)}" height="{plot_height}" fill="#e9b662" fill-opacity="0.12"><title>{_escape(interval.get("label"))} · {_escape(interval.get("source"))} · {_escape(chart.local(_instant(interval["start"])))}—{_escape(chart.local(_instant(interval["end"])))}</title></rect></g>')
        for band_index, band in enumerate(band_rows):
            output.append(_source_group(band, "band-source"))
            groups, current = [], []
            for point in sorted(band.get("points") or [], key=lambda row: _instant(row["timestamp"])):
                time = _instant(point["timestamp"])
                if point.get("lower") is None or point.get("upper") is None or not chart.start <= time <= chart.end:
                    if current:
                        groups.append(current)
                    current = []
                    continue
                if current and chart.gap is not None and time - current[-1][0] > chart.gap:
                    groups.append(current)
                    current = []
                current.append((time, point["lower"], point["upper"]))
            if current:
                groups.append(current)
            color = _PALETTE[band_index % len(_PALETTE)]
            description = f'{band.get("label", "区间")} · 来源 {band.get("source", "未知")} · {band.get("qualification", "未知")}'
            for group in groups:
                if len(group) > 1:
                    coords = [(chart.x(t, left, plot_width), py(b)) for t, a, b in group] + [(chart.x(t, left, plot_width), py(a)) for t, a, b in reversed(group)]
                    points = " ".join(f"{_position(x)},{_position(z)}" for x, z in coords)
                    output.append(f'<polygon class="band" points="{points}" fill="{color}" fill-opacity="0.16"><title>{_escape(description)}</title></polygon>')
                for time, a, b in group:
                    x = chart.x(time, left, plot_width)
                    title = _escape(f"{description} · {chart.local(time)} · {_number(a)}—{_number(b)} {unit}")
                    output.append(f'<line class="band-point" x1="{_position(x)}" x2="{_position(x)}" y1="{_position(py(a))}" y2="{_position(py(b))}" stroke="{color}" stroke-opacity="0.3" stroke-width="3" tabindex="0"><title>{title}</title></line>')
            output.append("</g>")
        count = 0
        for series in selected:
            output.append(_source_group(series.fields, "series-source"))
            isolated = set()
            if payload["kind"] == "line":
                for segment in _segments(series.points, chart):
                    if len(segment) >= 2:
                        coords = " ".join(f"{_position(chart.x(point.time, left, plot_width))},{_position(py(point.value))}" for point in segment)
                        output.append(f'<polyline class="series-line" points="{coords}" fill="none" stroke="{series.color}" stroke-width="2" stroke-linejoin="round"/>')
                    else:
                        isolated.add(segment[0])
            for point in series.points:
                if point.value is None or not chart.start <= point.time <= chart.end:
                    continue
                count += 1
                x, point_y = chart.x(point.time, left, plot_width), py(point.value)
                title = _escape(_tooltip(chart, series, point))
                if payload["kind"] == "bars":
                    baseline = py(0)
                    output.append(f'<rect class="bar" x="{_position(max(left, x - 3))}" y="{_position(min(baseline, point_y))}" width="{_position(min(6, left + plot_width - max(left, x - 3)))}" height="{_position(abs(baseline - point_y))}" fill="{series.color}" stroke="{series.color}" fill-opacity="0.75"><title>{title}</title></rect>')
                else:
                    hit = ' fill-opacity="0" stroke-opacity="0" pointer-events="all"' if payload["kind"] == "line" else ""
                    output.append(f'<circle class="point" cx="{_position(x)}" cy="{_position(point_y)}" r="3.5" fill="{series.color}" stroke="#ffffff" stroke-width="0.8"{hit} tabindex="0"><title>{title}</title></circle>')
                    if point in isolated:
                        output.append(f'<circle class="isolated-point" cx="{_position(x)}" cy="{_position(point_y)}" r="2" fill="{series.color}" pointer-events="none"/>')
            output.append("</g>")
        if not count:
            output.append(_text("无可绘制的数值（缺失不补零）", left + plot_width / 2, top + plot_height / 2, anchor="middle", color=_MUTED))
        output.append(_x_axis(chart, left, top, plot_width, plot_height))
        y = top + plot_height + 45
        for series in selected:
            output.append(f'<rect x="{left:g}" y="{_position(y - 9)}" width="10" height="10" fill="{series.color}"/>')
            caption = _caption(series, detailed=False)
            if any(point.value is None for point in series.points):
                caption += " · 缺失处不连线"
            rendered, used = _paragraph(caption, left + 17, y, plot_width - 17, size=11)
            output.append(rendered)
            y += used + 4
        for band in band_rows:
            rendered, used = _paragraph(f'区间：{band.get("label", "")} · 来源 {band.get("source", "未知")} · {band.get("qualification", "未知")}', left, y, plot_width, size=11)
            output.append(rendered)
            y += used + 2
        if reference:
            rendered, used = _paragraph(f'{reference.get("label", "参照")} · 下限 {_number(reference.get("lower"))} · 上限 {_number(reference.get("upper"))} · 中位数 {_number(reference.get("median"))} {unit}', left, y, plot_width, size=11)
            output.append(rendered)
            y += used + 2
        output.append("</g>")
        y += 14
    return "".join(output), y


def _stage_identity(row: Mapping) -> tuple:
    return tuple(row.get(key) for key in ("route_id", "source", "source_scope", "device_id"))


def _stage_svg(chart: _Chart, width: int, y: float) -> tuple[str, float]:
    intervals = chart.fields.get("intervals") or []
    sources = list(dict.fromkeys(_stage_identity(row) for row in intervals))
    labels = {row["id"]: row.get("label") for row in _routes(chart.fields)}
    left, plot_width = 82, width - 110
    output = []
    if not sources:
        return _text("暂无睡眠阶段记录；未知不补为清醒或深睡。", 24, y + 20, color=_MUTED), y + 48
    for identity in sources:
        route, source, scope, device = identity
        caption = " · ".join(_plain(part) for part in (labels.get(route), f"来源 {source or '未标记'}", scope, device) if part)
        output.append(_source_group({"route_id": route}, "stage-source"))
        output.append(_text(caption, 24, y + 8))
        top, row_height = y + 25, 28
        for index, (category, (label, color)) in enumerate(_STAGE.items()):
            output.append(_text(label, left - 10, top + index * row_height + 16, color=color if category != "unknown" else _MUTED, anchor="end"))
            output.append(f'<line x1="{left}" x2="{left + plot_width}" y1="{_position(top + index * row_height + 22)}" y2="{_position(top + index * row_height + 22)}" stroke="{_RULE}"/>')
        for interval in intervals:
            if _stage_identity(interval) != identity:
                continue
            start, end = max(chart.start, _instant(interval["start"])), min(chart.end, _instant(interval["end"]))
            if end <= start:
                continue
            category = interval.get("category") if interval.get("category") in _STAGE else "unknown"
            label, color = _STAGE[category]
            row_y = top + list(_STAGE).index(category) * row_height
            title = f'{interval.get("label") or label} · {chart.local(_instant(interval["start"]))}—{chart.local(_instant(interval["end"]))} · {caption} · {interval.get("qualification", "未知")}'
            output.append(f'<rect class="stage-interval" x="{_position(chart.x(start, left, plot_width))}" y="{_position(row_y)}" width="{_position((end - start) / (chart.end - chart.start) * plot_width)}" height="22" rx="2" fill="{color}" tabindex="0"><title>{_escape(title)}</title></rect>')
        output.append(_x_axis(chart, left, top, plot_width, row_height * len(_STAGE)))
        output.append("</g>")
        y = top + row_height * len(_STAGE) + 47
    return "".join(output), y


# Original, deliberately schematic geometry, not a third-party anatomical asset.
# One image-left half is mirrored for the other half. These are coarse regions,
# not claims about precise activation, force or confirmed unilateral training.
_FRONT = {
    "shoulders": "M72 58 L53 63 L44 88 L62 95 L73 78 Z",
    "chest": "M74 61 L98 64 L98 104 L67 99 L64 82 Z",
    "biceps": "M46 93 L62 98 L55 129 L39 125 Z",
    "forearms": "M38 129 L53 134 L43 175 L29 171 Z",
    "core": "M69 106 L98 109 L98 160 L75 158 Z",
    "quadriceps": "M73 171 L97 171 L92 235 L69 235 L64 199 Z",
    "calves": "M70 243 L91 243 L87 292 L73 295 L67 270 Z",
}
_BACK = {
    "shoulders": "M72 58 L53 63 L44 88 L62 95 L73 78 Z",
    "upper_back": "M74 62 L98 64 L98 95 L71 92 L64 79 Z",
    "back": "M89 98 L98 98 L98 131 L89 131 Z",
    "lats": "M69 98 L86 99 L86 133 L76 141 L69 126 Z",
    "lower_back": "M80 137 L98 137 L98 164 L77 163 Z",
    "triceps": "M46 93 L62 98 L55 129 L39 125 Z",
    "forearms": "M38 129 L53 134 L43 175 L29 171 Z",
    "glutes": "M73 169 L98 168 L98 195 L67 195 Z",
    "hamstrings": "M68 201 L97 201 L92 237 L70 237 Z",
    "calves": "M70 244 L91 244 L87 292 L73 295 L67 270 Z",
}
_COARSE_FRONT = {
    "arm_front": ("biceps", _FRONT["biceps"]),
    "thigh_front": ("quadriceps", _FRONT["quadriceps"]),
    "lower_leg_front": ("calves", _FRONT["calves"]),
    "neck": ("neck", "M88 49 L98 50 L98 61 L84 57 Z"),
}
_COARSE_BACK = {
    "arm_back": ("triceps", _BACK["triceps"]),
    "thigh_back": ("hamstrings", _BACK["hamstrings"]),
    "neck": ("neck", "M88 49 L98 50 L98 61 L84 57 Z"),
}
_OUTLINE = "M86 52 L73 56 L50 62 L39 93 L32 129 L22 173 L36 181 L50 143 L60 112 L66 153 L61 193 L65 236 L64 268 L68 303 L91 303 L95 264 L99 218 L101 218 L105 264 L109 303 L132 303 L136 268 L135 236 L139 193 L134 153 L140 112 L150 143 L164 181 L178 173 L168 129 L161 93 L150 62 L127 56 L114 52 Z"
_BODY_RAMP = ((0, "#f8fafc"), (3, "#cde9e6"), (6, "#80c8ba"), (10, "#319b84"), (math.inf, "#0f6556"))


def _body_color(value: float | None) -> str:
    return _UNKNOWN if value is None else next(color for bound, color in _BODY_RAMP if value <= bound)


def _region_caption(region: Mapping) -> str:
    side = _SIDE.get(region.get("side", "unknown"), "侧别未知")
    return (f'{region.get("label") or region.get("key") or "未知部位"} · {side} · 主要 {_number(region.get("direct_sets"))} 组'
            f' · 辅助 {_number(region.get("indirect_sets"))} 组 · 已记录参与 {_number(region.get("observed_sets"))} 组'
            f' · {region.get("qualification") or "资格未知"}')


def _body_svg(chart: _Chart, width: int, y: float) -> tuple[str, float]:
    regions = chart.fields.get("regions") or []
    sources = list(dict.fromkeys((_plain(row.get("source")), _plain(row.get("qualification"))) for row in regions)) or [("", "")]
    coarse = {**_COARSE_FRONT, **_COARSE_BACK}
    groups = []
    for source, qualification in sources:
        rows = [region for region in regions if (_plain(region.get("source")), _plain(region.get("qualification"))) == (source, qualification)]
        keys = {row.get("key") for row in rows}
        if any(key in keys and target in keys and key != target for key, (target, _) in coarse.items()):
            # A coarse vendor region and a named muscle may cover the same
            # drawing. Keep their observations on separate diagrams.
            groups.append((source, qualification, [row for row in rows if row.get("key") not in coarse]))
            groups.append((source, qualification, [row for row in rows if row.get("key") in coarse]))
        else:
            groups.append((source, qualification, rows))
    output = []
    figure_scale = min(1.05, (width - 48) / 440)
    figure_height = 322 * figure_scale
    for source, qualification, rows in groups:
        output.append('<g class="body-source">')
        output.append(_text(f"来源 {source or '未标记'} · {qualification or '资格未知'}", 24, y + 9))
        figure_y = y + 39
        for view_index, (label, base_shapes) in enumerate((("正面", _FRONT), ("背面", _BACK))):
            shapes = dict(base_shapes)
            for key, (target, path) in (_COARSE_FRONT if view_index == 0 else _COARSE_BACK).items():
                if any(row.get("key") == key for row in rows):
                    shapes.pop(target, None)
                    shapes[key] = path
            origin = (width - 440 * figure_scale) / 2 + view_index * 230 * figure_scale
            output.append(_text(label, origin + 100 * figure_scale, y + 29, anchor="middle"))
            output.append(f'<g transform="translate({_position(origin)} {_position(figure_y)}) scale({_position(figure_scale)})">')
            output.append(f'<circle cx="100" cy="28" r="21" fill="{_UNKNOWN}" stroke="#b3c1ca"/>')
            output.append(f'<path d="{_OUTLINE}" fill="{_UNKNOWN}" stroke="#b3c1ca" stroke-width="1.5"/>')
            for key, path in shapes.items():
                for mirrored in (False, True):
                    side = ("left" if mirrored else "right") if view_index == 0 else ("right" if mirrored else "left")
                    matched = [row for row in rows if row.get("key") == key and row.get("side", "unknown") in {side, "bilateral", "unknown", None}]
                    # Conflicting multiple entries are not summed or silently
                    # preferred. Their independent counts remain in the legend.
                    value = matched[0].get("observed_sets") if len(matched) == 1 else None
                    title = "；".join(_region_caption(row) for row in matched) if matched else f"{key} · {side} · 未知"
                    transform = ' transform="translate(200 0) scale(-1 1)"' if mirrored else ""
                    output.append(f'<path class="body-region" data-region="{key}" data-side="{side}" d="{path}"{transform} fill="{_body_color(value)}" stroke="#ffffff" stroke-width="1.2" tabindex="0"><title>{_escape(title)}</title></path>')
            output.append("</g>")
        y = figure_y + figure_height + 14
        for region in rows:
            rendered, used = _paragraph(_region_caption(region), 24, y, width - 48, size=12)
            output.append(rendered)
            y += used + 3
        if not rows:
            output.append(_text("暂无可定位的部位记录，灰色表示未知。", 24, y))
            y += 22
        output.append("</g>")
        y += 12
    for label, color in (("未知", _UNKNOWN), ("0", _BODY_RAMP[0][1]), ("0–3", _BODY_RAMP[1][1]), ("3–6", _BODY_RAMP[2][1]), ("6–10", _BODY_RAMP[3][1]), (">10", _BODY_RAMP[4][1])):
        index = ("未知", "0", "0–3", "3–6", "6–10", ">10").index(label)
        x = 24 + index * min(95, (width - 48) / 6)
        output.append(f'<rect x="{_position(x)}" y="{_position(y - 10)}" width="12" height="12" fill="{color}" stroke="#b3c1ca"/>')
        output.append(_text(label, x + 17, y, size=11))
    y += 24
    for note in ("颜色：已记录参与组数；主要/辅助分别列示，不合成计数。", _BODY_NOTE,
                 "前后分区为简化示意；侧别未知时双侧示意不表示已确认左右训练。"):
        rendered, used = _paragraph(note, 24, y, width - 48, size=11)
        output.append(rendered)
        y += used + 3
    return "".join(output), y


def render_chart_svg(chart: Any, *, width: int = 900, height: int = 420) -> str:
    """Return standalone SVG with local-time ticks and original-value tooltips.

    Height is a minimum: unit/source panels and complete legends may extend it.
    Missing values break lines, and supplied resolution also bounds each gap.
    No smoothing, interpolation, aggregation or cross-source merging occurs.
    """
    for value, minimum, maximum in ((width, 360, 2400), (height, 240, 1600)):
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ValueError("chart dimensions are outside the supported range")
    return _render_svg(_chart(chart), width, height, include_notes=True)


def _render_svg(context: _Chart, width: int, height: int, *, include_notes: bool) -> str:
    payload = context.fields
    title = payload.get("title") or "已保存图表"
    output = []
    rendered, used = _paragraph(title, 24, 29, width - 48, size=19, color=_INK)
    output.append(rendered)
    y = 29 + used
    rendered, used = _paragraph(f'{context.local(context.start)} — {context.local(context.end)} · {payload.get("timezone")}', 24, y, width - 48, size=11)
    output.append(rendered)
    y += used + 15
    if payload["kind"] == "body_map":
        rendered, y = _body_svg(context, width, y)
    elif payload["kind"] == "stages":
        rendered, y = _stage_svg(context, width, y)
    else:
        rendered, y = _numeric_svg(context, width, y, height)
    output.append(rendered)
    notes = [*(payload.get("summary") or []), *(payload.get("notes") or [])] if include_notes else []
    if include_notes and payload.get("routes"):
        notes.append(_ROUTE_NOTE)
    if payload.get("state") in {"partial", "unavailable"}:
        notes.append("部分记录可用，缺失不补零。" if payload["state"] == "partial" else "暂无可用记录，缺失不补零。")
    if payload.get("truncated"):
        notes.append("数据已截断；仅展示模型提供的记录，不代表完整覆盖。")
    for note in dict.fromkeys(_plain(note) for note in notes if note):
        rendered, used = _paragraph(note, 24, y, width - 48, size=12)
        output.append(rendered)
        y += used + 4
    total_height = max(height, math.ceil(y + 18))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {total_height}" width="{width}" height="{total_height}" role="img" aria-label="{_escape(title)}" font-family="sans-serif">'
            f'<title>{_escape(title)}</title><desc>时间横轴按实际时间间隔绘制；悬停或聚焦数据点查看保存值，缺失不补零。</desc>'
            f'<rect width="{width}" height="{total_height}" rx="12" fill="#ffffff"/>' + "".join(output) + "</svg>")


def _p(text: Any, *, color: str = _MUTED) -> str:
    return f'<p style="margin:5px 0;font-size:12px;line-height:1.5;color:{color}">{_escape(text)}</p>'


def render_chart_html(chart: Any) -> str:
    """Return conservative push HTML (only div/span/p and trusted inline CSS).

    At most two preview series are displayed, each with at most 32 supplied
    points. Oversized series are omitted, never downsampled here. The caller's
    report renderer remains responsible for the complete transport budget.
    """
    context = _chart(chart, preview=True)
    payload = context.fields
    output = [f'<div style="margin:8px 0;padding:10px;background:#ffffff;border:1px solid {_RULE};border-radius:8px">', _p(payload.get("title") or "已保存图表", color=_INK)]
    output.extend(_p(text) for text in (payload.get("summary") or [])[:2])
    for note in (payload.get("notes") or [])[:1]:
        output.append(_p(note))
    output.append(_p(f'{datetime.fromtimestamp(context.start, context.zone):%m-%d %H:%M}—{datetime.fromtimestamp(context.end, context.zone):%m-%d %H:%M} · {payload.get("timezone")}'))
    if payload["kind"] == "body_map":
        rows = (payload.get("regions") or [])[:4]
        output.extend(_p(f'{region.get("label") or region.get("key")} · 主要 {_number(region.get("direct_sets"))} 组 · 辅助 {_number(region.get("indirect_sets"))} 组') for region in rows)
        output.append(_p(_BODY_NOTE))
        if len(payload.get("regions") or []) > 4:
            output.append(_p("更多部位记录未在微图展开。"))
    elif payload["kind"] == "stages":
        intervals = payload.get("intervals") or []
        if 0 < len(intervals) <= 32:
            # Separate sources and categories; interval widths are geometry,
            # not a newly computed sleep statistic.
            groups = list(dict.fromkeys((_stage_identity(row), _plain(row.get("category"))) for row in intervals))
            for identity, category in groups:
                label, color = _STAGE.get(category, _STAGE["unknown"])
                output.append(_p(" · ".join(_plain(part) for part in (label, *identity) if part)))
                output.append('<div style="position:relative;height:8px;margin:3px 3px 8px;background:#f1f4f6">')
                for interval in intervals:
                    if (_stage_identity(interval), _plain(interval.get("category"))) != (identity, category):
                        continue
                    start, end = max(context.start, _instant(interval["start"])), min(context.end, _instant(interval["end"]))
                    if end > start:
                        x = context.x(start, 0, 100)
                        size = (end - start) / (context.end - context.start) * 100
                        output.append(f'<span style="position:absolute;left:{_position(x)}%;width:{_position(size)}%;height:8px;background:{color}"></span>')
                output.append("</div>")
        else:
            output.append(_p("睡眠阶段微图省略：未提供不超过32段的阶段记录。"))
    elif not context.series:
        output.append(_p("未提供已聚合时间桶，微图未生成；完整图可查看原始记录。"))
    else:
        for series in context.series[:2]:
            output.append(_p(series.fields.get("label") or series.fields.get("key") or "已保存观测"))
            if len(series.points) > 32:
                output.append(_p("微图省略：预聚合时间桶超过32点；渲染层不重新聚合。"))
                continue
            points = [point for point in series.points if context.start <= point.time <= context.end]
            values = [point.value for point in points if point.value is not None]
            if not values:
                output.append(_p("无可绘制的数值（缺失不补零）"))
                continue
            lower, upper, _ = _scale(values)
            output.append(_p(f'{_AGGREGATION.get(series.fields.get("aggregation", "observed"), "聚合口径未标记")} · 纵轴 {lower:.8g}—{upper:.8g} {series.unit}'))
            output.append(f'<div style="position:relative;height:48px;margin:5px 3px;border-bottom:1px solid {_RULE};background:#f5f8fa">')
            for point in points:
                if point.value is None:
                    continue
                x = context.x(point.time, 0, 100)
                bottom = (point.value - lower) / (upper - lower) * 90
                output.append(f'<span style="position:absolute;left:{_position(x)}%;bottom:{_position(bottom)}%;width:4px;height:4px;border-radius:2px;background:{series.color}"></span>')
            output.append("</div>")
            observed = [point for point in points if point.value is not None]
            if observed:
                endpoints = [observed[0]] if len(observed) == 1 else [observed[0], observed[-1]]
                labels = [f'{datetime.fromtimestamp(point.time, context.zone):%m-%d %H:%M} {_number(point.value)} {series.unit}' for point in endpoints]
                output.append(_p(" → ".join(labels)))
            else:
                output.append(_p("无可绘制的数值（缺失不补零）"))
            if any(point.value is None for point in points):
                output.append(_p("缺失时段留白，不推断连续观测。"))
        if len(context.series) > 2:
            output.append(_p(f"另有 {len(context.series) - 2} 条来源系列未在微图展开。"))
    if payload.get("truncated"):
        output.append(_p("记录已截断，微图不代表完整覆盖。"))
    output.append("</div>")
    return "".join(output)


_PAGE_STYLE = """
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0;background:#faf8f4;color:#292620;font:14px/1.5 system-ui,sans-serif}
main{max-width:960px;margin:auto;padding:20px 24px}
header{padding:0 0 14px;border-bottom:1px solid #e4dfd4}
h1{font-size:20px;font-weight:600;margin:0 0 6px}
p{margin:5px 0;overflow-wrap:anywhere}.meta{font-size:12px;color:#6b6459;font-variant-numeric:tabular-nums}
.chart-nav{display:flex;align-items:center;gap:12px;padding:12px 0;flex-wrap:wrap}
label{font-size:12px;color:#6b6459}
select{font:inherit;max-width:100%;padding:6px 28px 6px 9px;color:inherit;background:#fffdf9;border:1px solid #e4dfd4;border-radius:4px}
section{margin:0 0 12px;background:#fffdf9;border:1px solid #e4dfd4;border-radius:4px;overflow:hidden}
.chart-plot{overflow:auto}svg{display:block;width:100%;height:auto;min-width:360px}
.chart-summary{padding:10px 16px 0;font-size:13px}
.source-nav{display:flex;align-items:center;gap:10px;padding:12px 16px 0;flex-wrap:wrap}
.source-status,.source-note{padding:6px 16px 0;font-size:12px;color:#6b6459}
circle.point[fill-opacity="0"]:hover,circle.point[fill-opacity="0"]:focus{fill-opacity:1;stroke-opacity:1}
details{padding:10px 16px;border-top:1px solid #e4dfd4;font-size:12px;color:#6b6459}
summary{cursor:pointer;color:#292620}details p{margin-top:8px}
select:focus-visible,summary:focus-visible,circle:focus,path:focus,rect:focus{outline:2px solid #806b56;outline-offset:2px}
[hidden]{display:none!important}
@media(max-width:600px){main{padding:14px 12px}h1{font-size:18px}.chart-nav{gap:8px}}
"""
_PAGE_SCRIPT = """
(() => {
  const selector = document.getElementById('chart-filter');
  const cards = document.querySelectorAll('[data-chart-index]');
  selector.addEventListener('change', () => {
    for (const card of cards) {
      card.hidden = card.dataset.chartIndex !== selector.value;
    }
  });
  for (const card of cards) {
    const source = card.querySelector('.source-filter');
    if (!source) continue;
    source.addEventListener('change', () => {
      for (const panel of card.querySelectorAll('.source-panel')) {
        panel.hidden = panel.dataset.sourceRoute !== source.value;
      }
    });
  }
})();
"""


def _page_panel(chart: Mapping, route_id: str, *, hidden: bool) -> str:
    view = _route_view(chart, route_id)
    svg = _render_svg(_chart(view), 900, 300, include_notes=False)
    summary = "".join(f'<p>{_escape(item)}</p>' for item in view.get("summary") or [])
    explanation = "".join(f'<p>{_escape(item)}</p>' for item in view.get("notes") or [])
    sources = "".join(f'<p>{_escape(" · ".join(_plain(row.get(key)) for key in ("label", "device_label", "source", "source_scope", "device_id", "source_field", "qualification") if row.get(key)))}</p>' for row in view.get("series") or [])
    details = f'<details><summary>来源与说明</summary>{sources}{explanation}</details>' if sources or explanation else ''
    summary = f'<div class="chart-summary">{summary}</div>' if summary else ''
    status = ""
    if route_id == "all" and chart.get("routes"):
        statuses = "".join(f'<p>{_escape(_route_status(row))}</p>' for row in _routes(chart))
        status = f'<div class="source-status" role="status">{statuses}</div>'
    visibility = " hidden" if hidden else ""
    return (f'<div class="source-panel" data-source-route="{_escape(route_id)}"{visibility}>'
            f'{status}{summary}<div class="chart-plot">{svg}</div>{details}</div>')


def render_chart_page(bundle: Any) -> str:
    """Return a complete local HTML page with safe, fixed chart-filter logic."""
    payload = _payload(bundle)
    charts = _rows(payload, "charts", 128)
    title = f'Vitalis 图表 · {_plain(payload.get("period_start"))}—{_plain(payload.get("period_end"))}'
    options = []
    cards = []
    for index, chart in enumerate(charts):
        selected, hidden = (' selected', '') if index == 0 else ('', ' hidden')
        options.append(f'<option value="{index}"{selected}>{_escape(chart.get("title") or "已保存图表")}</option>')
        routes = _routes(chart)
        active = _default_route(chart, routes)
        navigation = ""
        route_ids = ["all", *(route["id"] for route in routes)]
        if routes:
            route_options = []
            for route_id, label in [("all", "全部来源对比（非融合）"), *((route["id"], f'{route.get("label") or route["id"]} · {_ROUTE_STATE.get(route.get("state"), "状态未知")}') for route in routes)]:
                chosen = ' selected' if route_id == active else ''
                route_options.append(f'<option value="{_escape(route_id)}"{chosen}>{_escape(label)}</option>')
            navigation = (f'<div class="source-nav"><label for="source-filter-{index}">来源</label>'
                          f'<select id="source-filter-{index}" class="source-filter">{"".join(route_options)}</select></div>'
                          f'<p class="source-note">{_escape(_ROUTE_NOTE)}</p>')
        panels = "".join(_page_panel(chart, route_id, hidden=route_id != active) for route_id in route_ids)
        cards.append(f'<section data-chart-index="{index}"{hidden}>{navigation}{panels}</section>')
    notes = "".join(f'<p>{_escape(note)}</p>' for note in payload.get("notes") or [])
    if notes:
        notes = f'<details><summary>报告说明</summary>{notes}</details>'
    disabled = '' if cards else ' disabled'
    if not cards:
        options.append('<option>暂无图表</option>')
        notes += "<p>暂无可展示图表，缺失不补零。</p>"
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src \'none\'; connect-src \'none\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>{_escape(title)}</title><style>{_PAGE_STYLE}</style></head><body><main>'
            f'<header><h1>{_escape(title)}</h1><p class="meta">数据截至 {_escape(payload.get("as_of"))} · {_escape(payload.get("timezone"))}</p>'
            '<p class="meta">悬停或聚焦查看保存值；来源与单位分开展示，缺失不补零。</p></header>'
            f'<nav class="chart-nav" aria-label="图表导航"><label for="chart-filter">指标</label><select id="chart-filter"{disabled}>{"".join(options)}</select>'
            f'<span class="meta">{len(charts)} 张图 · 每次查看一张</span></nav>'
            '<noscript><p class="meta">脚本未启用，仅展示首张图表；切换图表需要启用本页脚本。</p></noscript>'
            + "".join(cards) + notes + f'</main><script>{_PAGE_SCRIPT}</script></body></html>')
