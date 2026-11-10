"""Format saved supplemental context; no aggregation, I/O, or new advice."""
from .report_formatting import minutes_text, number, timestamp_text
from collections import Counter


SLEEP_LABELS = {"sleep_latency_minutes": "入睡用时", "out_of_bed_delay_minutes": "醒后至离床",
                "time_in_bed_minutes": "在床时间", "sleep_efficiency_percent": "睡眠效率"}
GOAL_LABELS = {"step_goal": "步数", "active_minutes_goal": "燃脂时间", "calorie_goal": "热量"}
UNITS = {"steps": "步", "min": "分钟", "kcal": "千卡"}


def sleep_timing_lines(sleep: dict, *, detailed: bool = False, timezone_name: str | None = None) -> list[str]:
    parts = []
    for metric, label in SLEEP_LABELS.items():
        value = sleep.get(metric)
        shown = f"{number(value)}%" if metric == "sleep_efficiency_percent" and value is not None else minutes_text(value)
        if shown is not None:
            parts.append(f"{label} {shown}")
    lines = ["；".join(parts)] if parts else []
    if detailed:
        clocks = [f"{label} {timestamp_text(sleep[key], timezone_name)}"
                  for key, label in (("in_bed_at", "上床"), ("out_of_bed_at", "离床")) if sleep.get(key)]
        if clocks:
            lines.insert(0, "；".join(clocks))
    return lines


def recorded_context_sections(context: dict, *, weekly: bool = False) -> list[dict]:
    groups = {"sleep": [], "goals": [], "body": []}
    goal_notes = []
    records = [*(context.get("metrics") or []), *(context.get("goals") or [])]
    counts = Counter(item.get("metric") for item in records)

    def source_suffix(item: dict) -> str:
        if counts[item.get("metric")] <= 1:
            return ""
        provenance = item.get("provenance") or {}
        scope = {"user_summary": "账户汇总", "user_fused": "账户融合"}.get(provenance.get("source_scope"), "来源未确认")
        device = provenance.get("device_id")
        return f" · {scope}" + (f" / {device}" if device else "")
    for item in context.get("metrics") or []:
        metric = item.get("metric")
        if metric in SLEEP_LABELS and weekly:
            shown = number(item.get("average")) if item.get("unit") == "%" else minutes_text(item.get("average"))
            if shown is None:
                continue
            unit = "%" if item.get("unit") == "%" else ""
            line = f"平均{SLEEP_LABELS[metric]} {shown}{unit}（记录 {item['available_days']} 晚）"
            if item.get("change") not in (None, 0):
                change_unit = "个百分点" if unit == "%" else "分钟"
                line += f"；较前周 {item['change']:+.1f} {change_unit}"
            groups["sleep"].append(line + source_suffix(item))
        elif metric == "weight":
            # A sparse measurement remains dated; it is not today's body weight.
            groups["body"].append(f"最近记录体重 {number(item.get('latest_value'))} 公斤（{item.get('latest_day') or '日期未确认'}）" + source_suffix(item))
    for item in context.get("goals") or []:
        label = GOAL_LABELS.get(item.get("metric"))
        if not label:
            continue
        unit = UNITS.get(item.get("unit"), "")
        if weekly:
            if item.get("evaluated_days", 0) > 0:
                line = f"可核对的 {item['evaluated_days']} 天中，有 {item['met_days']} 天达到当日 Zepp {label}目标"
            else:
                continue
        else:
            line = f"Zepp {label}目标 {number(item.get('latest_value'), 0)} {unit}"
            if item.get("actual_value") is not None:
                line += f"；已记录 {number(item['actual_value'], 0)} {unit}"
        groups["goals"].append(line + source_suffix(item))
        if item.get("metric") == "active_minutes_goal" and not goal_notes:
            goal_notes.append("燃脂时间按 Zepp 的心率区间记录，不等于训练总时长，也不能据此判断是否减脂。")
    sections = []
    for key, title in (("sleep", "睡眠时间与效率"), ("goals", "活动与当日目标"),
                       ("body", "最近体重记录")):
        if groups[key]:
            sections.append(dict(key=f"recorded_{key}", title=title, facts=groups[key],
                                 interpretation=[], limitations=goal_notes if key == "goals" else [], display=True))
    return sections
