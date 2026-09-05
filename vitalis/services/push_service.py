"""Push already-rendered daily health messages to configured transports."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from vitalis.intelligence.contracts import MorningBriefing
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

log = logging.getLogger("vitalis.push")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


@dataclass
class PushMessage:
    """一条推送消息。"""

    title: str
    body: str
    user_id: str
    template: str = "html"
    timestamp: datetime = field(default_factory=datetime.now)
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _ReportHtmlBlock:
    """Trusted presentation HTML generated only from validated numeric features."""

    html: str


class PushService:
    """推送服务。"""

    def __init__(self, webhook_url: str = "", pushplus_token: str | None = None):
        self.webhook_url = webhook_url
        self.pushplus_token = (
            os.getenv("PUSHPLUS_TOKEN", "")
            if pushplus_token is None
            else pushplus_token
        )
        self._handlers: list[Callable[[PushMessage], None]] = []
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """注册默认推送处理器。"""
        self._handlers.append(self._log_handler)
        if self.webhook_url:
            self._handlers.append(self._webhook_handler)
        if self.pushplus_token:
            self._handlers.append(self._pushplus_handler)

    def add_handler(self, handler: Callable[[PushMessage], None]) -> None:
        self._handlers.append(handler)

    def push(self, msg: PushMessage) -> dict:
        """发送推送，返回各渠道结果。"""
        results = {}
        for handler in self._handlers:
            try:
                handler(msg)
                results[handler.__name__] = "ok"
            except Exception as exc:
                results[handler.__name__] = f"error: {exc}"
                log.warning("push handler failed: %s", exc)
        return results

    def push_daily_profile(self, user_id: str, profile, period: str = "morning") -> dict:
        """Render an already-computed profile; this layer never makes decisions."""
        payload = (
            profile.model_dump(mode="json")
            if hasattr(profile, "model_dump")
            else profile
        )
        if period == "morning":
            briefing = MorningBriefingEngine().build_payload(
                payload,
                payload.get("delivery_metadata"),
            )
            return self.push_morning_briefing(user_id, briefing)
        elif period == "evening":
            payload = EveningBriefingEngine().build_payload(payload)
            title, body_lines = _render_evening(payload)
        else:
            raise ValueError("period must be morning or evening")

        msg = PushMessage(
            title=title,
            body=_render_report_html(body_lines),
            user_id=user_id,
            template="html",
            extras=payload,
        )
        return self.push(msg)

    def push_morning_briefing(
        self, user_id: str, briefing: MorningBriefing | dict
    ) -> dict:
        payload = (
            briefing.model_dump(mode="json")
            if hasattr(briefing, "model_dump")
            else briefing
        )
        title, body_lines = _render_morning(payload)
        return self.push(PushMessage(
            title=title,
            body=_render_report_html(body_lines),
            user_id=user_id,
            template="html",
            extras=payload,
        ))

    def push_weekly_profile(self, user_id: str, profile) -> dict:
        """Render and send one weekly profile; this method does not schedule delivery."""
        payload = WeeklyBriefingEngine().build_payload(profile)
        title, body_lines = _render_weekly(payload)
        return self.push(PushMessage(
            title=title,
            body=_render_report_html(body_lines),
            user_id=user_id,
            template="html",
            extras=payload,
        ))

    @staticmethod
    def _log_handler(msg: PushMessage) -> None:
        log.info("[PUSH] user=%s title=%s\n%s", msg.user_id, msg.title, msg.body)

    def _webhook_handler(self, msg: PushMessage) -> None:
        if not self.webhook_url:
            return
        try:
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                client.post(
                    self.webhook_url,
                    json={
                        "user_id": msg.user_id,
                        "title": msg.title,
                        "body": msg.body,
                        "template": msg.template,
                        "timestamp": msg.timestamp.isoformat(),
                        "extras": msg.extras,
                    },
                )
        except Exception:
            log.warning("webhook push failed")

    def _pushplus_handler(self, msg: PushMessage) -> None:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.post(
                PUSHPLUS_URL,
                json={
                    "token": self.pushplus_token,
                    "title": msg.title,
                    "content": msg.body,
                    "template": msg.template,
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("PushPlus returned an invalid response") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("PushPlus returned an invalid response")
            code = payload.get("code")
            if code != 200:
                raise RuntimeError(f"PushPlus rejected delivery with code {code}")


_REPORT_ELEMENT_STYLES = {
    "blockquote": (
        "margin:0 0 18px;padding:10px 12px;border-left:4px solid #0f766e;"
        "background:#f0fdfa;color:#475569"
    ),
    "h2": (
        "margin:24px 0 10px;padding:8px 10px;border-left:4px solid #0f766e;"
        "border-radius:4px;background:#f1f5f9;color:#0f4c5c;font-size:19px;"
        "line-height:1.35"
    ),
    "h3": "margin:18px 0 8px;color:#243b53;font-size:16px;line-height:1.4",
    "p": "margin:8px 0;color:#334155;line-height:1.75",
    "ul": "margin:8px 0 12px;padding-left:21px;color:#334155",
    "ol": "margin:8px 0 12px;padding-left:23px;color:#334155",
    "li": "margin:6px 0;line-height:1.7",
    "strong": "color:#111827;font-weight:650",
    "code": (
        "display:inline-block;max-width:100%;box-sizing:border-box;padding:5px 7px;"
        "border:1px solid #cbd5e1;border-radius:4px;background:#f8fafc;"
        "color:#0f172a;font-family:monospace;font-size:13px;line-height:1.5;"
        "white-space:pre;letter-spacing:0"
    ),
}


class _ReportStyleTreeprocessor(Treeprocessor):
    def run(self, root):
        for element in root.iter():
            style = _REPORT_ELEMENT_STYLES.get(element.tag)
            if style:
                element.set("style", style)
        return root


class _ReportStyleExtension(Extension):
    def extendMarkdown(self, markdown):  # noqa: N802 - Markdown extension API
        markdown.treeprocessors.register(
            _ReportStyleTreeprocessor(markdown), "vitalis_report_style", 5
        )


def _render_report_html(lines: list[str | _ReportHtmlBlock]) -> str:
    """Convert the deterministic report dialect into portable, safe PushPlus HTML."""
    source_lines = []
    html_blocks = {}
    for index, line in enumerate(lines):
        if isinstance(line, _ReportHtmlBlock):
            token = f"VITALISREPORTHTMLBLOCK{index}"
            source_lines.extend(["", token, ""])
            html_blocks[token] = line.html
        else:
            source_lines.append(line.replace("&", "&amp;").replace("<", "&lt;"))
    source = "\n".join(source_lines)
    fragment = Markdown(extensions=[_ReportStyleExtension()]).convert(source)
    for token, block in html_blocks.items():
        styled_paragraph = f'<p style="{_REPORT_ELEMENT_STYLES["p"]}">{token}</p>'
        fragment = fragment.replace(styled_paragraph, block)
    return (
        '<div style="max-width:680px;margin:0 auto;padding:14px 14px 22px;'
        'box-sizing:border-box;border:1px solid #dbe4e8;border-radius:8px;'
        'background:#ffffff;color:#1f2937;font-family:Arial,sans-serif;'
        'font-size:15px;line-height:1.65;letter-spacing:0;word-break:break-word">'
        f"{fragment}</div>"
    )


def _render_morning(briefing: dict) -> tuple[str, list[str]]:
    action_plan = briefing["action_plan"]
    primary = action_plan.get("primary_session")
    report_date = briefing["date"]
    action_title = primary["title"] if primary else briefing["action_label"]
    title = f"Vitalis 晨报 · {report_date} · {action_title}"
    lines = _timing_lines(briefing) + ["", "## 昨晚睡眠与身体状态", ""]
    observations = [item.get("text", "") for item in briefing.get("observations", [])]
    if observations:
        lines.extend(f"- {item}" for item in observations if item)
    else:
        lines.append("昨晚可用的睡眠与身体状态观测不足。")
    lines.extend(["", "## 今天做什么", ""])
    if briefing["decision_action"] == "INSUFFICIENT_DATA":
        lines.append("今天不生成训练建议。")
    else:
        lines.extend(_render_coach_actions(action_plan))
    reasons = [item["text"] for item in briefing.get("key_reasons", [])]
    if reasons:
        lines.extend(["", "## 为什么", "", *(f"- {item}" for item in reasons)])
    cautions = briefing.get("cautions", [])
    if cautions:
        lines.extend(["", "## 注意", "", *(f"- {item}" for item in cautions)])
    safety = MorningBriefingEngine.safety_lines(briefing)
    if safety:
        lines.extend(["", "## 安全限制", "", *(f"- {item}" for item in safety)])
    feedback_prompt = briefing.get("feedback_prompt")
    if feedback_prompt:
        lines.extend(["", "## 训练后告诉我", "", feedback_prompt])
    return title, lines


def _render_weekly(payload: dict) -> tuple[str, list[str]]:
    period_start = payload.get("period_start", "未知")
    period_end = payload.get("period_end", "未知")
    context = payload.get("report_context") or {}
    quality = payload.get("data_quality") or {}
    quality_label = quality.get("status_label", "数据状态未知")
    title = f"Vitalis 周报 · {period_end} · {quality_label}"
    lines = [
        f"> **滚动周期：{period_start} 至 {period_end}** · 睡眠/HRV：{quality_label}",
        "",
        *_timing_lines(payload),
        "",
        "## 事实",
        "",
    ]
    sections = payload.get("weekly_sections") or {}
    facts = sections.get("facts") or payload.get("facts") or {}
    sleep = facts.get("sleep") or {}
    sleep_parts = []
    if sleep.get("available_days") is not None:
        sleep_parts.append(f"有效 {sleep['available_days']} 天")
    if sleep.get("average_minutes") is not None:
        sleep_parts.append(f"平均 {sleep['average_minutes']:g} 分钟")
    if sleep.get("median_minutes") is not None:
        sleep_parts.append(f"中位数 {sleep['median_minutes']:g} 分钟")
    if sleep_parts:
        lines.append(f"- **睡眠**：{'；'.join(sleep_parts)}。")
    else:
        lines.append("- **睡眠**：本周期没有可用睡眠事实。")

    recovery = facts.get("recovery") or {}
    recovery_parts = []
    if recovery.get("hrv_median_ms") is not None:
        metric_label = recovery.get("hrv_metric_label") or "心率变异性"
        recovery_parts.append(
            f"{metric_label}中位数 {recovery['hrv_median_ms']:g} 毫秒"
            f"（{recovery.get('hrv_available_days', 0)} 天）"
        )
    if recovery.get("rhr_median_bpm") is not None:
        recovery_parts.append(f"静息心率中位数 {recovery['rhr_median_bpm']:g} 次/分钟")
    if recovery_parts:
        lines.append(f"- **身体状态观测**：{'；'.join(recovery_parts)}。")

    training = facts.get("training") or {}
    training_parts = []
    for key, label, unit in (
        ("workout_count", "训练", "次"),
        ("duration_minutes", "训练时长", "分钟"),
        ("vendor_load", "设备训练负荷", ""),
        ("aerobic_minutes", "有氧", "分钟"),
        ("strength_sessions", "力量训练", "次"),
        ("rest_days", "已确认休息日", "天"),
    ):
        value = training.get(key)
        if value is not None:
            training_parts.append(f"{label} {value:g}{unit}")
    if training_parts:
        training_label = "训练（已记录合计）" if training.get("totals_are_partial", True) else "训练"
        lines.append(f"- **{training_label}**：{'；'.join(training_parts)}。")
    else:
        lines.append("- **训练**：本周期没有可用训练合计。")

    activity = facts.get("activity") or {}
    activity_parts = []
    if activity.get("available_days") is not None:
        activity_parts.append(f"有效 {activity['available_days']} 天")
    if activity.get("total_steps") is not None:
        activity_parts.append(f"累计 {activity['total_steps']:,} 步")
    if activity.get("average_steps") is not None:
        activity_parts.append(f"日均 {activity['average_steps']:g} 步")
    if activity_parts:
        lines.append(f"- **活动**：{'；'.join(activity_parts)}。")
    else:
        lines.append("- **活动**：本周期没有可用步数事实。")

    lines.extend(["", "## 趋势", ""])
    inferences = payload.get("inferences") or {}
    changes = sections.get("key_changes") or inferences.get("key_changes") or []
    trend_items = sections.get("trends") or inferences.get("trends") or []
    if changes:
        lines.extend(f"- {item}" for item in changes[:6])
    elif trend_items:
        lines.append("- 已达到 TrendEngine 门槛的趋势本周期没有明显变化。")
    else:
        lines.append("- 当前没有达到比较门槛的趋势；不对缺失周期做趋势判断。")

    lines.extend(["", "## 覆盖与限制", ""])
    coverage = (
        sections.get("training_coverage")
        or context.get("training_coverage")
        or context.get("coverage")
        or {}
    )
    if coverage:
        status_label = {
            "COMPLETE": "本周期清单已核实", "PARTIAL": "清单部分已核实",
            "UNKNOWN": "清单覆盖未确认",
        }.get(coverage.get("coverage_status"), "清单覆盖未确认")
        lines.append(
            f"- 训练历史：{status_label}；"
            f"已核实 {coverage.get('record_days', 0)} 天；"
            f"未知 {coverage.get('unknown_days', 7)} 天。"
        )
    limitations = sections.get("limitations") or inferences.get("limitations") or []
    lines.extend(f"- {item}" for item in limitations[:6])
    if not coverage and not limitations:
        lines.append("- 覆盖上下文未提供；不能将本周期解释为完整。")

    lines.extend(["", "## 建议", ""])
    recommendations = sections.get("recommendations") or (payload.get("actions") or {}).get("recommendations") or []
    if recommendations:
        for item in recommendations[:3]:
            action = item.get("action") or ""
            reasons = "；".join(item.get("reasons") or [])
            line = f"- **{item.get('title', '建议')}**：{action}"
            if reasons:
                line += f"（依据：{reasons}）"
            lines.append(line)
    else:
        lines.append("- 当前没有可由事实支持的周期建议。")
    return title, lines


def _render_evening(payload: dict) -> tuple[str, list[str]]:
    features = payload["features"]
    training = features["training"]
    report_date = payload["date"]
    today = (payload.get("evening_facts") or {}).get("today_workouts")
    if today is None:
        today = [
            workout
            for workout in training.get("recent_workouts", [])
            if workout.get("date") == report_date
        ]
    if len(today) == 1:
        title_label = today[0]["sport_mode_label"]
    elif today:
        title_label = "多项训练回顾"
    else:
        title_label = "日常活动回顾"
    title = f"Vitalis 晚报 · {report_date} · {title_label}"
    lines = _metadata(payload)
    lines.extend([
        "",
        "## 今日回顾",
        "",
    ])
    if today:
        for workout in today:
            details = [
                _display_with_unit(workout.get("duration_minutes"), "分钟"),
                f"设备训练负荷 {_display(workout.get('vendor_load'))}",
            ]
            if workout.get("heart_rate_avg_bpm") is not None:
                details.append(f"平均心率 {workout['heart_rate_avg_bpm']} 次/分钟")
            lines.append(
                f"- **{workout['sport_mode_label']}** · {' · '.join(details)}"
            )
    else:
        lines.append("今天没有正式训练记录；这只是对当前记录的观察，不能据此推断休息状态，也不需要在晚上补课。")

    details = _render_today_workout_details(training, report_date)
    if details:
        lines.extend(["", "## 训练表现", "", *details])

    daily_state = _render_evening_daily_state(payload, training)
    if daily_state:
        lines.extend(["", "## 全天状态", "", *daily_state])
    open_health_lines = _render_open_health_evening(payload)
    if open_health_lines:
        lines.extend(["", "## 开放洞察", "", *open_health_lines])

    lines.extend([
        "",
        "## 今晚恢复",
        "",
        _evening_recovery_action(training, today, report_date),
        "",
        "## 明天衔接",
        "",
        _tomorrow_bridge(training, today, report_date),
    ])
    return title, lines


def _render_evening_daily_state(payload: dict, training: dict) -> list[str]:
    lines = []
    steps = _fact_value(payload, "steps")
    if steps is not None:
        lines.append(f"- **日常活动**：今天走了 {round(steps):,} 步。")

    stress = _fact_value(payload, "stress")
    relaxed = _fact_value(payload, "stress_relaxed_pct")
    high = _fact_value(payload, "stress_high_pct")
    stress_parts = []
    if stress is not None:
        stress_parts.append(f"设备记录的平均压力 {stress:g}")
    if relaxed is not None:
        stress_parts.append(f"放松状态占 {relaxed:g}%")
    if high is not None:
        stress_parts.append(f"高压力占 {high:g}%")
    if stress_parts:
        lines.append(f"- **压力分布**：{'；'.join(stress_parts)}。")

    features = payload.get("features", {})
    lines.extend(_render_weekly_sleep_hrv_trend(features.get("hrv") or {}))

    running = training.get("running") or {}
    strength = training.get("strength") or {}
    run_count = running.get("sessions_7d")
    strength_count = strength.get("sessions_7d", training.get("strength_sessions_7d"))
    rhythm = []
    if run_count is not None:
        rhythm.append(f"跑步 {run_count} 次")
    if strength_count is not None:
        rhythm.append(f"力量训练 {strength_count} 次")
    if rhythm:
        lines.append(f"- **最近 7 天**：{'；'.join(rhythm)}。")

    load = training.get("load_7d")
    reference = training.get("load_7d_reference")
    change = training.get("load_7d_change_percent")
    if load is not None:
        text = f"截至今天的 7 天，设备训练负荷为 {load:g}"
        meaning = "这是训练刺激，不直接代表恢复好坏"
        if reference is not None and change is not None:
            if abs(change) < 5:
                text += f"，与此前 3 周周均（{reference:g}）基本相同（{change:+.1f}%）"
                meaning = "近期训练节奏总体稳定"
            elif change > 0:
                text += (
                    f"，比此前 3 周周均（{reference:g}）高 {abs(change):.1f}%"
                )
                meaning = "这表示近期训练刺激增加，不代表恢复变差"
            else:
                text += (
                    f"，比此前 3 周周均（{reference:g}）低 {abs(change):.1f}%"
                )
                meaning = "这表示近期训练刺激减少，不代表恢复变差"
        lines.append(
            f"- **近期负荷**：{text}。{meaning}。"
        )
    return lines


def _render_weekly_sleep_hrv_trend(hrv: dict) -> list[str | _ReportHtmlBlock]:
    trend = hrv.get("sleep_hrv_daily_trend") or {}
    points = [
        item for item in (trend.get("points") or [])
        if isinstance(item, dict)
        and isinstance(item.get("date"), str)
        and isinstance(item.get("value_ms"), (int, float))
    ]
    if not points:
        return []
    return [
        "",
        "### 近 7 天睡眠 HRV",
        "",
        _ReportHtmlBlock(_render_weekly_sleep_hrv_chart_html(points)),
        "",
        f"{trend['device_label']} 近 7 天有 {len(points)} 晚记录，"
        f"昨晚 {trend['today_value_ms']:g} 毫秒。"
        "这是每晚睡眠 HRV 的逐日趋势，用于观察恢复变化。",
        "",
    ]


def _render_weekly_sleep_hrv_chart_html(points: list[dict]) -> str:
    values = [float(item["value_ms"]) for item in points]
    low_value = min(values)
    high_value = max(values)
    axis_low = max(0.0, float(int(low_value // 10) * 10 - 10))
    axis_high = float(int((high_value + 9) // 10) * 10 + 10)
    if axis_high <= axis_low:
        axis_high = axis_low + 20
    left, right, top, bottom = 48.0, 576.0, 28.0, 128.0
    span = max(len(points) - 1, 1)

    def coordinate(index: int) -> tuple[float, float]:
        x = left + index / span * (right - left)
        ratio = (values[index] - axis_low) / (axis_high - axis_low)
        return x, bottom - ratio * (bottom - top)

    coordinates = [coordinate(index) for index in range(len(points))]
    polyline = ""
    if len(points) >= 2:
        encoded = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
        polyline = (
            f'<polyline points="{encoded}" fill="none" stroke="#2f9e44" '
            'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    markers = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ffffff" '
        'stroke="#2f9e44" stroke-width="3"/>'
        f'<text x="{x:.1f}" y="{max(y - 11, 14):.1f}" text-anchor="middle" '
        f'fill="#237a35" font-size="12" font-weight="600">{values[index]:g}</text>'
        for index, (x, y) in enumerate(coordinates)
    )
    dates = "".join(
        f'<text x="{x:.1f}" y="154" text-anchor="middle" fill="#64748b" font-size="11">'
        f'{item["date"][5:].replace("-", "/")}</text>'
        for item, (x, _) in zip(points, coordinates)
    )
    grid = "".join(
        f'<line x1="{left:.0f}" y1="{top + offset * (bottom - top) / 2:.1f}" '
        f'x2="{right:.0f}" y2="{top + offset * (bottom - top) / 2:.1f}" '
        'stroke="#e2e8f0" stroke-width="1"/>'
        for offset in range(3)
    )
    labels = "".join(
        f'<text x="40" y="{top + offset * (bottom - top) / 2 + 4:.1f}" '
        f'text-anchor="end" fill="#94a3b8" font-size="11">{value:g}</text>'
        for offset, value in enumerate((axis_high, (axis_low + axis_high) / 2, axis_low))
    )
    return (
        '<div style="margin:6px 0 10px;padding:8px;border:1px solid #dbe4e8;'
        'border-radius:6px;background:#ffffff;overflow:hidden">'
        '<svg viewBox="0 0 600 170" width="100%" role="img" '
        'aria-label="近七天睡眠心率变异性逐日趋势" '
        'style="display:block;width:100%;height:auto;background:#ffffff">'
        f'{grid}{labels}{polyline}{markers}{dates}'
        '</svg></div>'
    )


def _evening_recovery_action(
    training: dict, today: list[dict], report_date: str
) -> str:
    running_sessions = _sessions_on_day(training.get("running"), report_date)
    strength_sessions = _sessions_on_day(training.get("strength"), report_date)
    hard_run = any(
        item.get("classification") in {"TEMPO_RUN", "INTERVAL_RUN", "LONG_RUN"}
        and item.get("confidence") in {"MODERATE", "HIGH"}
        for item in running_sessions
    )
    if hard_run:
        return (
            "今天的跑步包含明确的较高强度或长距离刺激。今晚不再追加跑步或腿部力量，"
            "正常吃晚餐、补足水分，只保留轻松走动或舒展。"
        )
    if running_sessions and strength_sessions:
        return (
            "今天跑步和力量都已完成。今晚不再追加训练，正常吃晚餐、补足水分，"
            "睡前只做轻松活动。"
        )
    if running_sessions:
        return (
            "今天已经完成跑步。今晚不再追加力量或补跑，正常吃晚餐、补足水分，"
            "腿部有紧张感时只做轻松走动。"
        )
    if strength_sessions:
        return (
            "今天已经完成力量训练。今晚不再追加跑步或补组，正常吃晚餐、补足水分，"
            "让已训练肌群休息。"
        )
    if today:
        return "今天的正式活动已经完成。今晚不再追加训练，按正常饮食、补水和作息收尾。"
    return "今天没有正式训练记录；今晚不据此判断恢复状态，也不安排补训练，按身体感受收尾。"


def _tomorrow_bridge(training: dict, today: list[dict], report_date: str) -> str:
    running_sessions = _sessions_on_day(training.get("running"), report_date)
    strength_sessions = _sessions_on_day(training.get("strength"), report_date)
    hard_run = any(
        item.get("classification") in {"TEMPO_RUN", "INTERVAL_RUN", "LONG_RUN"}
        and item.get("confidence") in {"MODERATE", "HIGH"}
        for item in running_sessions
    )
    lower_strength = any(
        item.get("focus") in {"LEGS", "LOWER"} for item in strength_sessions
    )
    if hard_run or lower_strength:
        return (
            "今天的训练记录提示明天避免连续安排质量跑和腿部力量；"
            "明天具体是否训练，留待明早数据和身体感受到齐后再决定。"
        )

    next_focus = (training.get("strength") or {}).get("next_focus_label")
    if next_focus:
        rest_prefix = "" if today else "今天没有训练记录，不需要用明天加量补偿。"
        return (
            f"{rest_prefix}下一次力量训练的候选重点是{next_focus}；"
            "是否安排在明天，留待明早数据和身体感受明确后再决定。"
        )
    if today:
        return "明天的训练安排留待明早数据和身体感受明确后再决定。"
    return "今天没有训练记录，不需要用明天加量补偿；明天按计划和身体感受安排。"


def _sessions_on_day(feature: dict | None, report_date: str) -> list[dict]:
    return [
        item for item in (feature or {}).get("recent_sessions", [])
        if item.get("date") == report_date
    ]


def _render_open_health_morning(payload: dict) -> list[str]:
    bundle = payload.get("open_health_insights")
    if not isinstance(bundle, dict):
        return []
    lines: list[str] = []
    readiness = bundle.get("readiness") or {}
    readiness_payload = readiness.get("payload") or {}
    if readiness.get("status") in {"AVAILABLE", "PARTIAL"}:
        state = readiness_payload.get("state")
        current_ln = readiness_payload.get("ln_rmssd")
        baseline_ln = readiness_payload.get("baseline_ln_rmssd")
        swc = readiness_payload.get("swc")
        numeric = (current_ln, baseline_ln, swc)
        if all(isinstance(value, (int, float)) for value in numeric):
            from math import exp

            current = exp(current_ln)
            baseline = exp(baseline_ln)
            lower = exp(baseline_ln - swc)
            upper = exp(baseline_ln + swc)
            relation = {
                "suppressed": "低于动态参考范围",
                "elevated": "高于动态参考范围",
                "normal": "位于近期动态范围内",
            }.get(state)
            if relation:
                prior_nights = readiness_payload.get("prior_nights")
                history_label = (
                    f"此前 {prior_nights} 夜"
                    if isinstance(prior_nights, int) and prior_nights > 0
                    else "近期夜间"
                )
                lines.append(
                    f"- **实验性 RMSSD 观察**：Zepp 夜间汇总 "
                    f"{current:.1f} 毫秒；{history_label}基线约 {baseline:.1f} "
                    f"毫秒，动态参考范围 {lower:.1f}-{upper:.1f} 毫秒，"
                    f"{relation}。"
                )
    anomaly = bundle.get("anomaly") or {}
    anomaly_payload = anomaly.get("payload") or {}
    if anomaly.get("status") in {"AVAILABLE", "PARTIAL"} and anomaly_payload.get("flagged"):
        lines.append("- **多指标观察**：多个夜间信号连续偏离个人常态。")
    unavailable = []
    if anomaly.get("status") == "REFUSED":
        unavailable.append("多指标观察缺少可比的同源信号")
    load = bundle.get("training_load") or {}
    if load.get("status") == "REFUSED":
        unavailable.append("训练负荷因上游同步覆盖尚未完整验证而不输出")
    if unavailable:
        lines.append(f"- **暂未生成**：{'；'.join(unavailable)}。")
    lines.append(
        "参考范围按此前最多 7 夜的 lnRMSSD 均值和波动计算；"
        "以上是非诊断性实验观察，不参与今日训练决策。"
    )
    return lines


def _render_open_health_evening(payload: dict) -> list[str]:
    bundle = payload.get("open_health_insights")
    if not isinstance(bundle, dict):
        return []
    load = bundle.get("training_load") or {}
    load_payload = load.get("payload") or {}
    report_date = payload.get("date")
    points = [
        item for item in (load_payload.get("daily_points") or [])
        if item.get("date") == report_date
    ]
    point = points[-1] if points else None
    lines = []
    if point and point.get("status") == "SCORED" and point.get("trimp") is not None:
        lines.append(f"- **当日 TRIMP**：{point['trimp']:.1f}。")
    values = []
    for label, key in (("ATL", "atl"), ("CTL", "ctl"), ("TSB", "tsb")):
        value = (point or {}).get(key)
        if value is None:
            value = load_payload.get(key)
        if value is not None:
            values.append(f"{label} {float(value):.1f}")
    if values:
        suffix = (
            "（下界估计，上游同步覆盖尚未完全验证）"
            if load.get("status") == "PARTIAL" or load_payload.get("lower_bound")
            else ""
        )
        lines.append(f"- **描述性训练负荷**：{'；'.join(values)}{suffix}。")
    if not lines:
        refusal = load.get("refusal_reason") or {}
        missing = refusal.get("missing_inputs") or []
        if missing:
            lines.append(
                f"- 训练负荷开放洞察缺少输入：{'、'.join(_open_health_missing_labels(missing))}。"
            )
    return lines


def _open_health_missing_labels(values: list[str]) -> list[str]:
    labels = {
        "UserProfile.sex": "已确认性别",
        "UserProfile.confirmed_hrmax_bpm": "确认最大心率",
        "UserProfile.sex=MALE": "支持的性别档案",
        "UserProfile.sex.source=USER_CONFIRMED": "性别确认来源",
        "UserProfile.confirmed_hrmax_bpm.source=USER_CONFIRMED": "最大心率确认来源",
        "queried_history_days>=14": "至少 14 个已查询自然日",
    }
    output = [labels[item] for item in dict.fromkeys(values) if item in labels]
    return output or ["部分结构化输入"]


def _fact_value(payload: dict, metric: str) -> float | None:
    facts = payload.get("facts", {}).get(metric, [])
    if not facts:
        return None
    priority = {
        "user_fused": 0,
        "normalized_daily_record": 1,
        "device": 2,
        "unknown": 3,
    }
    selected = min(
        facts,
        key=lambda item: priority.get(
            (item.get("provenance") or {}).get("source_scope"), 4
        ),
    )
    value = selected.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def _timing_lines(payload: dict) -> list[str]:
    context = payload.get("report_context") or {}
    as_of = context.get("as_of")
    if not isinstance(as_of, str):
        return ["> 分析截止时刻未提供；日期汇总不代表实时测量。"]
    try:
        clock = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        clock = clock.astimezone(ZoneInfo(context.get("timezone") or "UTC"))
        displayed = clock.strftime("%Y-%m-%d %H:%M %z")
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        displayed = as_of
    suffix = "；当日记录仍可能更新" if context.get("target_day_complete") is False else ""
    return [f"> **分析截至：{displayed}**{suffix}。不代表所有指标在此时测量。"]


def _metadata(payload: dict) -> list[str]:
    quality = payload.get("data_quality", {}).get("status_label", "数据状态未知")
    return [f"> **数据日期：{payload['date']}** · 夜间关键观测：{quality}", "", *_timing_lines(payload)]


def _daily_conclusion(
    decision: dict,
    states: dict,
    action_plan: dict,
    sleep: dict,
    hrv: dict,
    overnight_vitals: dict,
) -> str:
    action = decision["action"]
    recovery_state = states.get("recovery")
    opening = {
        "GOOD": "昨夜恢复指标整体良好",
        "NORMAL": "昨夜身体状态整体稳定",
        "SUPPRESSED": "昨夜有多项恢复指标偏弱",
        "INSUFFICIENT_DATA": "昨夜可用于判断的恢复数据较少",
    }.get(recovery_state, "昨夜身体状态暂时无法完整判断")
    evidence = _status_evidence(sleep, hrv, overnight_vitals)
    if evidence:
        opening += "：" + "，".join(evidence)
    primary = action_plan.get("primary_session")
    if action in {"TRAIN_HARD", "TRAIN_NORMAL", "TRAIN_LIGHT"} and primary:
        return (
            f"{opening}。今天安排{primary['title']}，"
            f"按{primary['intensity_label']}完成。"
        )
    conclusions = {
        "RECOVERY": f"{opening}。今天以恢复活动为主。",
        "REST": f"{opening}。今天休息，不安排正式训练。",
        "INSUFFICIENT_DATA": f"{opening}。今天不根据设备数据安排强度训练。",
    }
    return conclusions[action]


def _status_evidence(sleep: dict, hrv: dict, vitals: dict) -> list[str]:
    evidence = []
    sleep_direction = (sleep.get("duration_deviation") or {}).get("direction")
    if sleep_direction == "near":
        evidence.append("睡眠时长接近平时水平")
    elif sleep_direction == "above":
        evidence.append("睡眠时长高于平时水平")
    elif sleep_direction == "below":
        evidence.append("睡眠时长低于平时水平")

    if not hrv.get("corroboration_affects_decision"):
        hrv_direction = (hrv.get("deviation") or {}).get("direction")
        if hrv_direction == "above":
            evidence.append("心率变异性高于个人基线")
        elif hrv_direction == "near":
            evidence.append("心率变异性在个人正常范围内")
        elif hrv_direction == "below":
            evidence.append("心率变异性低于个人基线")

    rhr_direction = (hrv.get("rhr_deviation") or {}).get("direction")
    if rhr_direction == "near":
        evidence.append("静息心率在个人正常范围内")
    elif rhr_direction == "above":
        evidence.append("静息心率高于个人基线")
    elif rhr_direction == "below":
        evidence.append("静息心率低于个人基线")

    respiratory_direction = (
        vitals.get("respiratory_rate_deviation") or {}
    ).get("direction")
    if respiratory_direction == "near":
        evidence.append("呼吸频率平稳")
    return evidence[:4]


def _render_coach_actions(action_plan: dict) -> list[str]:
    primary = action_plan.get("primary_session")
    optional = action_plan.get("optional_session")
    if primary is None:
        return ["今天没有生成训练安排，先按正常生活节奏活动。"]

    lines = _render_coach_session(primary, "主要")
    if optional is None:
        return lines

    relationship = action_plan["session_relationship"]
    if relationship == "ADDITION":
        relationship_text = (
            f"有余力时，晚些时候可再做{optional['title']}；不做也不影响今天的主要安排。"
        )
    else:
        relationship_text = (
            f"也可以把主要训练改成{optional['title']}，两项选一，不要在同一天都做。"
        )
    lines.extend(["", relationship_text, "", *_render_coach_session(optional, "可选")])
    return lines


def _render_coach_session(session: dict, role: str) -> list[str]:
    duration = session.get("total_duration_minutes")
    duration_text = f" · {_range(duration, '分钟')}" if duration else ""
    lines = [
        f"### {role}：{session['title']}{duration_text} · {session['intensity_label']}",
        "",
        session["focus"],
        "",
    ]
    for step in session.get("steps", []):
        details = []
        if step.get("duration_minutes"):
            details.append(_range(step["duration_minutes"], "分钟"))
        if step.get("sets"):
            details.append(f"{step['sets']} 组")
        if step.get("repetitions"):
            details.append(step["repetitions"])
        if step.get("load_kg") is not None:
            details.append(f"{step['load_kg']:g} 千克")
        if step.get("rest_seconds"):
            details.append(f"休息 {_range(step['rest_seconds'], '秒')}")
        if step.get("intensity"):
            details.append(step["intensity"])
        instruction = "；".join(step.get("instructions", []))
        suffix = " · ".join(details)
        text = f"{step['order']}. **{step['name']}**"
        if suffix:
            text += f"：{suffix}"
        if instruction:
            text += f"。{instruction}"
        lines.append(text)
    return lines


def _render_overnight_summary(
    sleep: dict, hrv: dict, overnight_vitals: dict
) -> list[str]:
    lines = []
    sleep_parts = []
    if sleep.get("duration_minutes") is not None:
        sleep_parts.append(_minutes_as_hours(sleep["duration_minutes"]))
    deviation = sleep.get("duration_deviation") or {}
    if deviation.get("percent") is not None:
        sleep_parts.append(f"较个人基线 {deviation['percent']:+.1f}%")
    if sleep.get("bedtime") and sleep.get("wake_time"):
        sleep_parts.append(
            f"{_clock_minute(sleep['bedtime'])}–{_clock_minute(sleep['wake_time'])}"
        )
    if sleep.get("vendor_sleep_score") is not None:
        sleep_parts.append(f"设备睡眠评分 {sleep['vendor_sleep_score']:g}")
    if sleep_parts:
        lines.append(f"- **睡眠**：{'；'.join(sleep_parts)}。")

    stage_parts = []
    if sleep.get("deep_minutes") is not None:
        stage_parts.append(f"深睡 {sleep['deep_minutes']} 分钟")
    if sleep.get("rem_minutes") is not None:
        stage_parts.append(f"快速眼动睡眠 {sleep['rem_minutes']} 分钟")
    if sleep.get("awake_minutes") is not None:
        stage_parts.append(f"清醒 {sleep['awake_minutes']} 分钟")
    if sleep.get("wake_count") is not None:
        wake_text = f"夜间醒来 {sleep['wake_count']} 次"
        wake_deviation = sleep.get("wake_count_deviation") or {}
        if wake_deviation.get("percent") is not None:
            wake_text += f"（较个人基线 {wake_deviation['percent']:+.1f}%）"
        stage_parts.append(wake_text)
    if stage_parts:
        lines.append(f"- **睡眠结构**：{'；'.join(stage_parts)}。设备分期主要看长期变化。")

    cardiovascular = []
    if hrv.get("value_ms") is not None:
        label = (
            "Zepp 睡眠心率变异性"
            if hrv.get("fusion_method") == "vendor_fused_with_device_audit"
            else "心率变异性"
        )
        text = f"{label} {hrv['value_ms']:g} 毫秒"
        hrv_deviation = hrv.get("deviation") or {}
        if hrv_deviation.get("percent") is not None:
            text += f"（较个人基线 {hrv_deviation['percent']:+.1f}%）"
        cardiovascular.append(text)
    if hrv.get("recent_7d_change_percent") is not None:
        cardiovascular.append(
            f"近 7 天较此前 7 天 {hrv['recent_7d_change_percent']:+.1f}%"
        )
    if hrv.get("rhr_bpm") is not None:
        label = {
            "sleep_rhr": "Zepp 睡眠静息心率",
            "resting_hr": "Zepp 日静息心率",
            "nocturnal_heart_rate": "分设备整夜心率中位数",
        }.get(hrv.get("rhr_metric"), "静息心率")
        text = f"{label} {hrv['rhr_bpm']:g} 次/分钟"
        rhr_deviation = hrv.get("rhr_deviation") or {}
        if rhr_deviation.get("percent") is not None:
            text += f"（较个人基线 {rhr_deviation['percent']:+.1f}%）"
        cardiovascular.append(text)
    if cardiovascular:
        lines.append(f"- **恢复相关**：{'；'.join(cardiovascular)}。")

    night = hrv.get("nocturnal_heart_rate") or {}
    if (
        night.get("status") == "AVAILABLE"
        and hrv.get("rhr_metric") == "nocturnal_heart_rate"
    ):
        parts = [f"中位数 {night['median_bpm']:g} 次/分钟"]
        if night.get("low_5m_bpm") is not None:
            low = f"稳定 5 分钟低点 {night['low_5m_bpm']:g} 次/分钟"
            if night.get("low_5m_time"):
                low += f"（{night['low_5m_time']}）"
            parts.append(low)
        if night.get("second_minus_first_bpm") is not None:
            delta = night["second_minus_first_bpm"]
            movement = "回落" if delta < 0 else "升高" if delta > 0 else "持平"
            amount = f" {abs(delta):g} 次/分钟" if delta else ""
            parts.append(f"后半夜较前半夜{movement}{amount}")
        lines.append(f"- **分设备整夜心率**：{'；'.join(parts)}。")

    vitals = _render_overnight_vitals_reason(overnight_vitals)
    if vitals:
        lines.append(f"- **其他夜间指标**：{vitals}")
    return lines


def _render_weekly_training_summary(training: dict) -> list[str]:
    lines = []
    total = training.get("duration_7d")
    load = training.get("load_7d")
    if total is not None or load is not None:
        parts = []
        if total is not None:
            parts.append(f"共训练 {total} 分钟")
        if load is not None:
            parts.append(f"设备训练负荷 {load:g}")
        lines.append(f"- **总量**：{'；'.join(parts)}。")

    running = training.get("running") or {}
    run_parts = []
    if running.get("sessions_7d") is not None:
        run_parts.append(f"{running['sessions_7d']} 次")
    if running.get("duration_minutes_7d") is not None:
        run_parts.append(f"{running['duration_minutes_7d']} 分钟")
    if running.get("distance_km_7d") is not None:
        run_parts.append(f"{round(running['distance_km_7d'], 2):g} 公里")
    strength = training.get("strength") or {}
    strength_sessions = strength.get("sessions_7d", training.get("strength_sessions_7d"))
    discipline_parts = []
    if run_parts:
        discipline_parts.append(f"跑步 {'、'.join(run_parts)}")
    if strength_sessions is not None:
        discipline_parts.append(f"力量训练 {strength_sessions} 次")
    if discipline_parts:
        lines.append(f"- **构成**：{'；'.join(discipline_parts)}。")

    reference = training.get("load_7d_reference")
    change = training.get("load_7d_change_percent")
    if reference is not None and change is not None:
        relation = "高" if change > 0 else "低" if change < 0 else "相同"
        amount = f" {abs(change):.1f}%" if change else ""
        lines.append(
            f"- **与近期相比**：本周训练负荷比此前 3 周周均（{reference:g}）"
            f"{relation}{amount}。这表示训练刺激的多少，不代表恢复好坏。"
        )

    status = training.get("training_status") or {}
    fitness_parts = []
    if status.get("vo2max_ml_kg_min") is not None:
        text = f"最大摄氧量 {status['vo2max_ml_kg_min']:g} 毫升/公斤/分钟"
        if status.get("vo2max_change_28d_percent") is not None:
            text += f"（近 28 天 {status['vo2max_change_28d_percent']:+.1f}%）"
        fitness_parts.append(text)
    if status.get("lactate_threshold_pace_seconds_per_km") is not None:
        text = (
            "乳酸阈值配速 "
            f"{_pace(status['lactate_threshold_pace_seconds_per_km'])}/公里"
        )
        if status.get("lactate_threshold_hr_bpm") is not None:
            text += f"，对应心率 {status['lactate_threshold_hr_bpm']:g} 次/分钟"
        fitness_parts.append(text)
    if fitness_parts:
        lines.append(f"- **跑步能力**：{'；'.join(fitness_parts)}。")

    if status.get("pai_earned_7d") is not None:
        pai_text = f"近 7 天累计获得 {status['pai_earned_7d']:g} PAI"
        if status.get("dominant_pai_zone_label"):
            pai_text += f"，主要来自{status['dominant_pai_zone_label']}活动"
        lines.append(f"- **活动强度贡献**：{pai_text}。")

    return lines or ["最近 7 天没有可汇总的训练记录。"]


def _render_plan_reasons(training: dict, action_plan: dict) -> list[str]:
    lines = []
    primary = action_plan.get("primary_session") or {}
    for reason in primary.get("personalization_reasons", []):
        lines.append(f"- {reason}")
        if len(lines) >= 2:
            break
    balance = action_plan.get("weekly_balance") or {}
    if primary and len(lines) < 3:
        lines.append(
            f"- 近 7 天跑步 {balance.get('running_completed_7d', 0)} 次、"
            f"力量 {balance.get('strength_completed_7d', 0)} 次；"
            f"今天优先安排{primary['title']}。"
        )
    return lines


def _render_overnight_vitals_reason(vitals: dict) -> str | None:
    if vitals.get("status") != "AVAILABLE":
        return None
    parts = []
    respiratory = vitals.get("respiratory_rate")
    respiratory_deviation = vitals.get("respiratory_rate_deviation")
    if respiratory is not None:
        text = f"夜间呼吸频率 {respiratory:g} 次/分钟"
        if (respiratory_deviation or {}).get("percent") is not None:
            text += f"（较个人基线 {respiratory_deviation['percent']:+.1f}%）"
        parts.append(text)

    temperature = vitals.get("skin_temperature_delta_c")
    if temperature is not None:
        parts.append(f"腕温较设备基线 {temperature:+g} 摄氏度")

    oxygen = vitals.get("oxygen") or {}
    if oxygen.get("status") == "AVAILABLE":
        odi = oxygen.get("odi_events_per_hour")
        minutes = oxygen.get("measured_minutes")
        interpretation = oxygen.get("interpretation")
        if interpretation == "repeated_elevation":
            text = "夜间血氧下降频率连续高于个人水平"
        elif interpretation == "single_night_elevation":
            text = "今夜血氧下降频率偏高，但还没有连续出现"
        else:
            text = "夜间血氧下降频率处于个人平时水平"
        details = []
        median_percent = oxygen.get("median_percent")
        lower_10th = oxygen.get("lower_10th_percent")
        if median_percent is not None:
            details.append(f"中位数 {median_percent:g}%")
        if lower_10th is not None:
            details.append(f"较低一成读数 {lower_10th:g}%")
        if odi is not None:
            details.append(f"每小时下降 {odi:g} 次")
        baseline = oxygen.get("odi_baseline")
        if baseline is not None:
            details.append(f"个人基线 {baseline:g} 次/小时")
        if minutes is not None:
            details.append(f"监测 {minutes} 分钟")
        if details:
            text += f"（{'，'.join(details)}）"
        parts.append(text)
    return "；".join(parts) + "。" if parts else None


def _render_notable_event(payload: dict) -> str | None:
    explanations = {
        "RECOVERY_SUPPRESSED": "多项恢复指标同时偏离个人基线。今天减量或休息比补训练更重要。",
        "SLEEP_DEFICIT": "睡眠已连续低于个人基线。今天先控制训练消耗，不用补偿性加量。",
        "RHR_ELEVATED": "静息心率已连续偏高。今天按较轻安排执行，并观察明天是否恢复。",
        "HRV_DROP": "夜间心率变异性已连续偏低。它不单独代表生病，但今天不适合据此加量。",
        "TRAINING_LOAD_SPIKE": "最近一周训练负荷明显增加。完成今天的计划即可，不再追加训练。",
        "TRAINING_GAP": "过去一周没有训练记录。今天先恢复规律，不需要一次补回缺少的训练。",
        "SLEEP_IMPROVEMENT": "最近睡眠持续改善。保持当前作息，不需要因此额外提高训练强度。",
        "HRV_RECOVERY": "夜间心率变异性持续回到个人正常范围。按计划训练即可，不额外加量。",
    }
    hrv = payload["features"]["hrv"]
    hrv_conflict = hrv.get("corroboration_affects_decision", False)
    current_hrv_direction = (hrv.get("deviation") or {}).get("direction")
    recent_hrv_direction = hrv.get("recent_7d_direction")
    for event in payload.get("events", []):
        if event.get("lifecycle") == "RESOLVED":
            continue
        event_type = event.get("type")
        if hrv_conflict and event_type in {"HRV_DROP", "HRV_RECOVERY"}:
            continue
        if event_type == "HRV_RECOVERY" and (
            current_hrv_direction != "near"
            or recent_hrv_direction not in {None, "near", "unknown"}
        ):
            continue
        if event_type == "HRV_DROP" and (
            current_hrv_direction != "below"
            and recent_hrv_direction != "below"
        ):
            continue
        text = explanations.get(event_type)
        if text:
            return text
    return None


def _render_coach_cautions(payload: dict) -> list[str]:
    cautions = []
    decision = payload["decision"]
    action_plan = decision["action_plan"]
    hrv = payload["features"]["hrv"]
    if payload.get("delivery_metadata", {}).get("sync_degraded"):
        cautions.append("本次同步未完整完成，结论使用的是已经保存的当天数据。")
    if action_plan.get("safety_status") == "LIMITED":
        cautions.append(action_plan["safety_status_label"])
        primary = action_plan.get("primary_session") or {}
        cautions.extend(primary.get("stop_conditions", [])[:1])
    if hrv.get("corroboration_affects_decision"):
        cautions.append(
            "今天的心率变异性证据不够稳定，本次安排主要依据睡眠、夜间心率和近期训练负荷。"
        )
    if decision["action"] == "INSUFFICIENT_DATA":
        cautions.extend(
            payload.get("data_quality", {}).get("missing_required_signal_labels", [])
        )
    return _unique(cautions)


def _minutes_as_hours(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    return f"{hours} 小时 {remainder} 分钟" if remainder else f"{hours} 小时"


def _range(values: list | tuple, unit: str) -> str:
    low, high = values
    return f"{low} {unit}" if low == high else f"{low}–{high} {unit}"


def _clock_minute(value: str) -> str:
    return value[:5]


def _display(value) -> str:
    return "暂无" if value is None else str(value)


def _display_with_unit(value, unit: str) -> str:
    return "暂无" if value is None else f"{value} {unit}"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _render_today_workout_details(training: dict, report_date: str) -> list[str]:
    lines = []
    running = training.get("running")
    if running:
        sessions = [
            item for item in running.get("recent_sessions", [])
            if item.get("date") == report_date
        ]
        for latest in sessions:
            confidence = latest.get("confidence")
            classification = (
                latest.get("classification_label", "课型暂不确定")
                if confidence in {"MODERATE", "HIGH"}
                else "课型暂不下结论"
            )
            metrics = [classification, f"总耗时 {latest['duration_minutes']} 分钟"]
            if latest.get("pause_duration_seconds", 0) > 0:
                metrics.append(
                    f"实际运动 {latest.get('moving_duration_minutes', 0):g} 分钟"
                )
            if latest.get("distance_km") is not None:
                metrics.append(f"{round(latest['distance_km'], 2):g} 公里")
            if latest.get("average_pace_seconds_per_km") is not None:
                metrics.append(f"平均配速 {_pace(latest['average_pace_seconds_per_km'])}/公里")
            if latest.get("median_cadence_spm") is not None:
                metrics.append(f"步频中位数 {latest['median_cadence_spm']:g} 步/分钟")
            if latest.get("average_power_watts") is not None:
                metrics.append(f"平均功率 {latest['average_power_watts']:g} 瓦")
            if latest.get("median_equivalent_pace_seconds_per_km") is not None:
                metrics.append(
                    f"等效配速 {_pace(latest['median_equivalent_pace_seconds_per_km'])}/公里"
                )
            if latest.get("average_heart_rate_bpm") is not None:
                metrics.append(f"平均心率 {latest['average_heart_rate_bpm']:g} 次/分钟")
            if latest.get("maximum_heart_rate_bpm") is not None:
                metrics.append(f"最高心率 {latest['maximum_heart_rate_bpm']:g} 次/分钟")
            if (
                confidence in {"MODERATE", "HIGH"}
                and latest.get("cardiac_drift_percent") is not None
            ):
                metrics.append(f"心率漂移 {latest['cardiac_drift_percent']:+g}%")
            lines.append(f"- **跑步**：{' · '.join(metrics)}")
            zone_text = _zone_distribution(latest.get("heart_rate_zones", []))
            if zone_text:
                lines.append(f"- **跑步心率分布**：{zone_text}。")
            dynamics = []
            if latest.get("median_ground_contact_time_ms") is not None:
                dynamics.append(f"触地时间 {latest['median_ground_contact_time_ms']:g} 毫秒")
            if latest.get("median_vertical_oscillation_mm") is not None:
                dynamics.append(f"垂直振幅 {latest['median_vertical_oscillation_mm']:g} 毫米")
            if latest.get("median_vertical_stride_ratio_percent") is not None:
                dynamics.append(f"垂直步幅比 {latest['median_vertical_stride_ratio_percent']:g}%")
            if latest.get("median_stride_length_cm") is not None:
                dynamics.append(f"步幅中位数 {latest['median_stride_length_cm']:g} 厘米")
            if dynamics:
                lines.append(f"- **跑姿记录**：{' · '.join(dynamics)}。")
            splits = latest.get("kilometer_splits") or []
            if splits:
                split_parts = []
                for split in splits[:10]:
                    text = f"{split['index']} 公里 {_pace(split['average_pace_seconds_per_km'])}"
                    if split.get("average_heart_rate_bpm") is not None:
                        text += f" / {split['average_heart_rate_bpm']:g} 次/分钟"
                    if split.get("elevation_gain_m") is not None:
                        text += f" / 爬升 {split['elevation_gain_m']:g} 米"
                    split_parts.append(text)
                lines.append(f"- **公里分段**：{'；'.join(split_parts)}。")
            baseline = latest.get("comparable_baseline")
            if baseline:
                pace_delta = float(baseline["pace_difference_percent"])
                comparison = (
                    f"配速快 {abs(pace_delta):.1f}%" if pace_delta < 0
                    else f"配速慢 {pace_delta:.1f}%" if pace_delta > 0
                    else "配速相同"
                )
                if baseline.get("heart_rate_difference_bpm") is not None:
                    comparison += f"，平均心率相差 {baseline['heart_rate_difference_bpm']:+g} 次/分钟"
                lines.append(
                    f"- **相近距离对比**：与此前 {baseline['sample_count']} 次相比，{comparison}。"
                )

    strength = training.get("strength")
    if strength:
        sessions = [
            item for item in strength.get("recent_sessions", [])
            if item.get("date") == report_date
        ]
        for latest in sessions:
            metrics = [latest["focus_label"], f"{latest['duration_minutes']} 分钟"]
            if latest.get("total_sets") is not None:
                metrics.append(f"{latest['total_sets']} 组")
            if latest.get("estimated_work_bouts") is not None:
                metrics.append(f"约 {latest['estimated_work_bouts']} 个工作段")
            if latest.get("median_rest_seconds") is not None:
                metrics.append(f"休息中位数 {latest['median_rest_seconds']:g} 秒")
            if latest.get("session_rpe") is not None:
                metrics.append(f"主观用力 {latest['session_rpe']}/10")
            if latest.get("average_heart_rate_bpm") is not None:
                metrics.append(f"平均心率 {latest['average_heart_rate_bpm']:g} 次/分钟")
            if latest.get("maximum_heart_rate_bpm") is not None:
                metrics.append(f"最高心率 {latest['maximum_heart_rate_bpm']:g} 次/分钟")
            lines.append(f"- **力量**：{' · '.join(metrics)}")
            exercises = latest.get("explicit_exercises", [])
            names = [item["exercise_name"] for item in exercises]
            if names:
                lines.append(f"- **动作**：{'、'.join(names)}")
                for exercise in exercises:
                    details = []
                    if exercise.get("sets") is not None:
                        details.append(f"{exercise['sets']} 组")
                    if exercise.get("repetitions"):
                        details.append(str(exercise["repetitions"]))
                    if exercise.get("weight_kg") is not None:
                        details.append(f"{exercise['weight_kg']:g} 千克")
                    if exercise.get("rpe") is not None:
                        details.append(f"RPE {exercise['rpe']:g}")
                    if exercise.get("rir") is not None:
                        details.append(f"余力 {exercise['rir']:g}")
                    if exercise.get("rest_seconds") is not None:
                        details.append(f"休息 {exercise['rest_seconds']} 秒")
                    if details:
                        lines.append(
                            f"- **{exercise['exercise_name']}**：{' · '.join(details)}。"
                        )
            else:
                lines.append("- **动作明细**：当前已同步数据未含明确动作、组次和重量，不能据此复原 App 中的修正项目。")
                hypotheses = [
                    item.get("exercise_name") or item.get("movement_pattern_label")
                    for item in latest.get("hypotheses", [])
                    if item.get("confidence") in {"MODERATE", "HIGH"}
                    and (item.get("exercise_name") or item.get("movement_pattern_label"))
                ]
                if hypotheses:
                    lines.append(
                        f"- **可能的动作模式**：{'、'.join(hypotheses)}（根据训练结构推测）。"
                    )
            zone_text = _zone_distribution(latest.get("heart_rate_zones", []))
            if zone_text:
                lines.append(f"- **力量训练心率分布**：{zone_text}。")
    return lines


def _zone_distribution(zones: list[dict]) -> str | None:
    shares = {
        int(item["zone"]): float(item.get("share_percent", 0))
        for item in zones
        if item.get("zone") is not None
    }
    if not shares:
        return None
    low = shares.get(1, 0) + shares.get(2, 0)
    moderate = shares.get(3, 0)
    high = shares.get(4, 0) + shares.get(5, 0)
    return (
        f"低强度 {low:.1f}% · 中等强度 {moderate:.1f}% · "
        f"阈值附近及以上 {high:.1f}%"
    )


def _pace(seconds: float) -> str:
    rounded = max(int(round(seconds)), 0)
    return f"{rounded // 60}:{rounded % 60:02d}"
