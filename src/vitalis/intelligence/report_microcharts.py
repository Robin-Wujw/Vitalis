"""Small, pure report fragments for already-filtered Zepp App chart values.

Only sleep stages and sparse stress points are supported. Geometry uses saved
instants/values, never new health statistics, interpolation or source fusion.
The caller selects the report section and supplies the account projection and
its reporting window. A last observation is NOT a reporting cutoff. Sleep uses
saved interval bounds, excluding explicitly labelled naps; stress uses the
supplied period. Large inputs use an existing preview or an explicit omission,
never renderer downsampling. Chart.resolution_seconds belongs to full series;
the current contract does not specify a separate preview bucket width.

These fragments use the same inline absolute-position mechanism as the existing
chart renderer. Provider allowlist validation and local browser layout do not
prove PushPlus rendering: transport acceptance and actual receiver visuals must
be checked separately. No title attributes, SVG, images, scripts or I/O are used.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_ACCOUNT = "zepp:account"
_SCOPES = {"user_summary", "user_fused"}
_STAGES = {"deep": ("深睡", "#8297b7"), "light": ("浅睡", "#bfd0df"),
           "rem": ("REM", "#bea9c5"), "awake": ("清醒", "#c7a184"),
           "unknown": ("未知", "#e8ecef")}
_AGGREGATIONS = {"observed": "已保存观测", "median": "已保存中位数", "mean": "已保存均值",
                 "last": "已保存末值", "sum": "已保存累计值"}
_MAX_FRAGMENT = 8000


def _mapping(payload: Any) -> Mapping:
    if isinstance(payload, Mapping):
        return payload
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        result = dump(mode="json")
        if isinstance(result, Mapping):
            return result
    raise ValueError("microchart input must be a chart mapping or model")


def _rows(payload: Mapping, key: str, limit: int) -> list:
    rows = payload.get(key, [])
    if not isinstance(rows, (list, tuple)) or len(rows) > limit or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"invalid chart {key}")
    return list(rows)


def _instant(value: Any) -> float:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("chart time needs a valid absolute instant") from exc


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stress value must be finite numeric data")
    try:
        valid = isfinite(value) and 0 <= value <= 100
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("stress value must be finite and in 0–100")
    return value


def _pos(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _p(text: str, *, extra: str = "") -> str:
    # All visible text is fixed vocabulary or formatted validated numbers/time.
    return f'<p style="margin:0;font-size:11px;line-height:17px;color:#71808b;{extra}">{text}</p>'


def _card(title: str, body: str, note: str) -> str:
    return ('<div style="box-sizing:border-box;width:100%;max-width:680px;margin:8px 0;'
            'padding:9px 10px;background:#fff;border:1px solid #edf0f2;border-radius:10px">'
            + _p(title, extra="font-size:12px;color:#4e6173;font-weight:600;margin-bottom:5px")
            + body + _p(note, extra="margin-top:4px") + "</div>")


def _empty(key: str, reason: str = "暂无可绘制记录；缺失不补零。") -> str:
    return _card("睡眠阶段" if key == "sleep" else "压力观测", _p(reason), "")


def _source_check(payload: Mapping) -> bool:
    if payload.get("selected_route") != _ACCOUNT:
        raise ValueError("report microchart requires the selected account route, not all or device")
    routes = _rows(payload, "routes", 64)
    ids = [row.get("id") for row in routes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate chart source routes")
    route = next((row for row in routes if row.get("id") == _ACCOUNT), None)
    if route is None or route.get("kind") != "account" or route.get("source") != "zepp":
        raise ValueError("missing verified account route")
    identities = set()
    for key in ("series", "preview_series", "intervals", "bands", "regions"):
        for row in _rows(payload, key, 3000 if key == "intervals" else 128 if key == "regions" else 48):
            if key == "intervals" and row.get("category") == "annotation" and row.get("source") == "user":
                continue
            if row.get("route_id") != _ACCOUNT or row.get("source") != "zepp" or row.get("source_scope") not in _SCOPES:
                raise ValueError("unfiltered or unqualified account source")
            identities.add((row.get("source_scope"), row.get("device_id")))
    if len(identities) > 1:
        raise ValueError("multiple source/device identities cannot share a report microchart")
    return route.get("state") == "available" and payload.get("state") != "unavailable"


def _clock(stamp: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(stamp, zone).strftime("%H:%M")


def _axis(start: float, end: float, zone: ZoneInfo, *, sleep: bool) -> str:
    # Equal elapsed-time spacing, including on DST transition days.
    count = 2 if sleep else 4
    labels = []
    for index in range(count + 1):
        stamp = start + (end - start) * index / count
        local = datetime.fromtimestamp(stamp, zone)
        label = _clock(stamp, zone)
        if not sleep:
            if index == count and local.hour == 0 and local.date() != datetime.fromtimestamp(start, zone).date():
                label = "24:00"
            elif index in (0, 2):
                label += " 夜间" if local.hour < 6 else " 午后" if 12 <= local.hour < 18 else ""
        labels.append(f"<span>{label}</span>")
    return ('<div style="display:flex;justify-content:space-between;font-size:10px;line-height:17px;'
            'color:#71808b">' + "".join(labels) + "</div>")


def _sleep(payload: Mapping, zone: ZoneInfo, start: float, end: float) -> str:
    rows, naps = [], False
    for row in _rows(payload, "intervals", 3000):
        if row.get("category") == "annotation":
            continue
        left, right = _instant(row.get("start")), _instant(row.get("end"))
        if right <= left:
            raise ValueError("sleep interval must increase")
        if "午睡" in str(row.get("label", "")):
            naps = True
            continue
        left, right = max(start, left), min(end, right)
        if right > left:
            rows.append((left, right, row.get("category")))
    if not rows:
        return _empty("sleep")
    if len(rows) > 52:
        return _empty("sleep", "阶段记录较多，微图未展开；未重新合并分期。")
    rows.sort(key=lambda row: row[0])
    if any(right > following[0] for (_, right, _), following in zip(rows, rows[1:])):
        raise ValueError("overlapping sleep intervals cannot be fused")
    start, end = rows[0][0], rows[-1][1]
    blocks = []
    for left, right, stage in rows:
        category = stage if stage in _STAGES else "unknown"
        color = _STAGES[category][1]
        style = f"position:absolute;left:{_pos((left-start)/(end-start)*100)}%;width:{_pos((right-left)/(end-start)*100)}%;height:18px;"
        # A narrow cap marks awake while its temporal interval stays to scale.
        style += f"box-sizing:border-box;border-top:3px solid {color}" if category == "awake" else f"background:{color}"
        blocks.append(f'<span style="{style}"></span>')
    legend = " ".join(f'<span style="color:{color if category != "unknown" else "#71808b"}">▪ {label}</span>'
                      for category, (label, color) in _STAGES.items())
    body = ('<div style="position:relative;height:18px;margin:7px 0 2px;background:#e8ecef;border-radius:3px;overflow:hidden">'
            + "".join(blocks) + "</div>" + _axis(start, end, zone, sleep=True)
            + _p(legend, extra="margin-top:3px;font-size:11px"))
    note = "灰色为未知/缺测 · 厂商估计"
    if naps:
        note += " · 午睡未展开"
    if payload.get("truncated"):
        note += " · 记录已截断"
    result = _card("睡眠阶段", body, note)
    return result if len(result) < _MAX_FRAGMENT else _empty("sleep", "阶段记录较多，微图未展开；未重新合并分期。")


def _stress(payload: Mapping, zone: ZoneInfo, start: float, end: float) -> str:
    choices = []
    for key in ("series", "preview_series"):
        rows = _rows(payload, key, 48)
        if len(rows) > 1:
            raise ValueError("multiple account series cannot be combined")
        if rows:
            row = rows[0]
            if row.get("unit") != "score":
                raise ValueError("stress series needs score units")
            aggregation = row.get("aggregation", "observed")
            if aggregation not in _AGGREGATIONS:
                raise ValueError("invalid saved series aggregation")
            points, ranges = [], []
            for point in _rows(row, "points", 3000):
                stamp, value = _instant(point.get("timestamp")), _number(point.get("value"))
                if start <= stamp <= end and value is not None:
                    points.append((stamp, value))
                    if key == "preview_series":
                        low, high = _number(point.get("minimum")), _number(point.get("maximum"))
                        if low is not None and high is not None:
                            if not low <= value <= high:
                                raise ValueError("invalid saved preview range")
                            if high > low:
                                ranges.append((stamp, low, high))
            choices.append((key, aggregation, points, ranges, row.get("last_observed_at")))
    for key, aggregation, points, ranges, last_observed_at in choices:
        if not points or len(points) > 120:
            continue
        tiles = []
        for stamp, low, high in ranges:
            color = "#c58b61" if high >= 75 else "#b8c9d1"
            tiles.append(f'<span style="position:absolute;left:{_pos((stamp-start)/(end-start)*100)}%;top:{_pos(100-high)}%;height:{_pos(high-low)}%;border-left:2px solid {color}"></span>')
        for stamp, value in points:
            color = ";color:#c58b61" if value >= 75 else ""
            tiles.append(f'<span style="position:absolute;left:{_pos((stamp-start)/(end-start)*100)}%;top:{_pos(100-value)}%{color}">▪</span>')
        body = ('<div style="display:flex;gap:7px;margin:5px 0 0">'
                '<div style="display:flex;flex-direction:column;justify-content:space-between;width:29px;'
                'height:52px;flex-shrink:0;font-size:9px;line-height:10px;color:#8a969e">'
                '<span>高100</span><span>中50</span><span>低0</span></div>'
                '<div style="flex:1;min-width:0">'
                '<div style="position:relative;height:42px;margin:5px 3px 5px 0;font:10px/0 monospace;color:#8faab8">'
                + "".join(tiles) + "</div>" + _axis(start, end, zone, sleep=False) + "</div></div>")
        note = "空白表示未记录，不是零压力。"
        if last_observed_at is not None:
            last = _instant(last_observed_at)
            if start <= last <= end:
                note = f"观测截至 {_clock(last, zone)}。" + note
        if key == "preview_series":
            note += "时段概览：点为" + _AGGREGATIONS[aggregation].removeprefix("已保存") + ("，竖线为观测范围。" if ranges else "。")
        else:
            note += "点为" + _AGGREGATIONS[aggregation].removeprefix("已保存") + "。"
        if payload.get("truncated"):
            note += " · 已截断"
        result = _card("压力观测 · 0–100 · 低 / 中 / 高参考，非诊断", body, note)
        if len(result) < _MAX_FRAGMENT:
            return result
    if any(points for _, _, points, _, _ in choices):
        return _empty("stress", "点位较多，微图未展开；未重新抽样或聚合。")
    return _empty("stress")


def render_report_microchart(payload: Any) -> str:
    """Render one bounded account-only HTML fragment, without changing input.

    Mapping and Pydantic Chart inputs are equivalent. Unavailable/empty records
    get a small textual placeholder; unsafe source selections or malformed
    measurements raise ValueError. Only saved values determine plotted shapes.
    """
    chart = _mapping(payload)
    key = chart.get("key")
    if key not in {"sleep", "stress"} or chart.get("kind") not in ({"stages"} if key == "sleep" else {"scatter", "bars"}):
        raise ValueError("only sleep stages and sparse stress charts are supported")
    try:
        zone = ZoneInfo(chart.get("timezone", ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("chart requires a valid IANA timezone") from exc
    start, end = _instant(chart.get("period_start")), _instant(chart.get("period_end"))
    if end <= start:
        raise ValueError("chart period must increase")
    resolution = chart.get("resolution_seconds")
    if resolution is not None and (isinstance(resolution, bool) or not isinstance(resolution, int) or resolution < 1):
        raise ValueError("chart resolution must be a positive integer")
    if not _source_check(chart):
        return _empty(key)
    return _sleep(chart, zone, start, end) if key == "sleep" else _stress(chart, zone, start, end)
