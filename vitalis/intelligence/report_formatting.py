"""Small helpers shared by deterministic report projections."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vitalis.time import local_timezone


def payload_of(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value or {})


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def number(value: Any, digits: int = 1) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if digits == 0:
        return f"{value:,.0f}"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}"


def percent(value: Any) -> str | None:
    text = number(value, 1)
    return f"{text}%" if text is not None else None


def date_text(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value or "日期未提供")


def minutes_text(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    total = int(value)
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def range_text(value: Any, unit: str) -> str | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    low, high = (number(item) for item in value)
    if low is None or high is None:
        return None
    return f"{low if low == high else low + '–' + high} {unit}"


def repetitions_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    return f"{text} 次" if re.fullmatch(r"\d+(?:\s*[-–~]\s*\d+)?", text) else text


def clock_text(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:5]


def baseline_text(deviation: dict[str, Any] | None, *, noun: str = "个人基线") -> str:
    if not isinstance(deviation, dict):
        return "尚无足够的同源个人参照，暂不比较"
    value = deviation.get("percent")
    direction = deviation.get("direction")
    reference = deviation.get("baseline_reference")
    details = []
    if isinstance(reference, (int, float)) and not isinstance(reference, bool):
        unit = deviation.get("unit")
        shown = minutes_text(reference) if unit == "min" else number(reference)
        unit_label = {"ms": "毫秒", "bpm": "次/分钟", "brpm": "次/分钟", "km": "公里", "kcal": "千卡", "steps": "步"}.get(unit, "")
        window = deviation.get("baseline_window_days")
        prefix = f"近 {window} 日" if window else "近期"
        details.append(f"{prefix}{noun}约 {shown}{(' ' + unit_label) if unit_label else ''}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        details.append(f"较{noun} {float(value):+.1f}%")
    else:
        labels = {"above": "高于", "below": "低于", "near": "接近"}
        if direction in labels:
            details.append(f"{labels[direction]}{noun}")
    return "，".join(details) if details else "尚无足够的同源个人参照，暂不比较"


def metric_label(metric: str | None, *, overnight: bool = False) -> str:
    labels = {
        "sleep_hrv": "睡眠 HRV",
        "hrv_rmssd": "HRV（RMSSD）",
        "hrv_sdnn": "HRV（SDNN）",
        "steps": "步数",
        "distance": "活动距离",
        "distance_km": "活动距离",
        "active_minutes": "活动时长",
        "calories": "热量记录",
        "heart_rate": "心率记录",
        "stress": "压力记录",
        "resting_hr": "静息心率",
        "sleep_rhr": "睡眠静息心率",
        "nocturnal_heart_rate": "夜间心率",
        "respiratory_rate": "呼吸频率",
    }
    label = labels.get(metric or "", "HRV" if metric is None else "其他观测")
    # RMSSD/SDNN may be all-day streams; only an explicitly selected sleep_hrv
    # value is named as a sleep signal in the user-facing report.
    return label


def coverage_text(status: Any, record_days: Any, unknown_days: Any, period_days: int) -> str:
    labels = {
        "COMPLETE": "周期记录已核实",
        "PARTIAL": "周期记录部分核实",
        "UNKNOWN": "周期记录覆盖尚未核实",
    }
    label = labels.get(str(status), "周期记录覆盖尚未核实")
    record = number(record_days, 0) or "0"
    unknown = number(unknown_days, 0) or "0"
    return f"{label}；已核实 {record}/{period_days} 天，尚未核实 {unknown} 天。"


def timestamp_text(value: Any, zone: str | None = None, *, short: bool = False) -> str:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        target = ZoneInfo(zone) if zone else local_timezone()
        return parsed.astimezone(target).strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M %z")
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return "时间未确认"


def as_of_line(context: dict[str, Any]) -> str | None:
    value = context.get("as_of")
    if not value:
        return None
    shown = timestamp_text(value, context.get("timezone"))
    suffix = "；当日记录仍可能更新" if context.get("target_day_complete") is False else ""
    return f"分析截至 {shown}{suffix}。"


def list_facts(mapping: dict[str, Any], labels: list[tuple[str, str]]) -> list[str]:
    output = []
    for key, label in labels:
        value = mapping.get(key)
        if value is not None:
            output.append(f"{label}：{value}。")
    return output


def running_class_label(code: str) -> str:
    from .running import CLASSIFICATION_LABELS

    return CLASSIFICATION_LABELS.get(code, "课型未确认")


def energy_label(role: str | None) -> str:
    return {
        "daily_total": "全天总消耗（设备估算）",
        "activity": "活动热量（设备估算）",
        "workout": "本次训练估算热量",
        "unspecified": "设备估算热量（统计范围待确认）",
    }.get(role or "unspecified", "设备估算热量（统计范围待确认）")
