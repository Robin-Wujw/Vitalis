"""Render and optionally deliver already-computed Vitalis report projections."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Callable

import httpx
from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
from vitalis.intelligence.report_formatting import as_of_line

log = logging.getLogger("vitalis.push")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


@dataclass
class PushMessage:
    title: str
    body: str
    user_id: str
    template: str = "html"
    timestamp: datetime = field(default_factory=datetime.now)
    extras: dict = field(default_factory=dict)


class PushService:
    """Transport service; report engines remain the single content projection."""

    def __init__(self, webhook_url: str = "", pushplus_token: str | None = None):
        self.webhook_url = webhook_url
        self.pushplus_token = os.getenv("PUSHPLUS_TOKEN", "") if pushplus_token is None else pushplus_token
        self._handlers: list[Callable[[PushMessage], None]] = []
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self._handlers.append(self._log_handler)
        if self.webhook_url:
            self._handlers.append(self._webhook_handler)
        if self.pushplus_token:
            self._handlers.append(self._pushplus_handler)

    def add_handler(self, handler: Callable[[PushMessage], None]) -> None:
        self._handlers.append(handler)

    def push(self, msg: PushMessage) -> dict:
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
        if period == "morning":
            briefing = MorningBriefingEngine().build_payload(profile, (profile if isinstance(profile, dict) else {}).get("delivery_metadata"))
            title, lines = _render_morning(briefing)
            extras = briefing
        elif period == "evening":
            briefing = EveningBriefingEngine().build(profile)
            title, lines = _render_evening(briefing.model_dump(mode="json"))
            extras = briefing.model_dump(mode="json")
        else:
            raise ValueError("period must be morning or evening")
        return self.push(PushMessage(title=title, body=_render_report_html(lines), user_id=user_id, extras=extras))

    def push_morning_briefing(self, user_id: str, briefing) -> dict:
        payload = briefing.model_dump(mode="json") if hasattr(briefing, "model_dump") else dict(briefing)
        title, lines = _render_morning(payload)
        return self.push(PushMessage(title=title, body=_render_report_html(lines), user_id=user_id, extras=payload))

    def push_weekly_profile(self, user_id: str, profile) -> dict:
        briefing = WeeklyBriefingEngine().build(profile)
        payload = briefing.model_dump(mode="json")
        title, lines = _render_report_briefing(payload, "周报")
        return self.push(PushMessage(title=title, body=_render_report_html(lines), user_id=user_id, extras=payload))

    def push_monthly_profile(self, user_id: str, profile) -> dict:
        """Explicit monthly capability; this method does not schedule monthly delivery."""
        briefing = MonthlyBriefingEngine().build(profile)
        payload = briefing.model_dump(mode="json")
        title, lines = _render_report_briefing(payload, "月报")
        return self.push(PushMessage(title=title, body=_render_report_html(lines), user_id=user_id, extras=payload))

    @staticmethod
    def _log_handler(msg: PushMessage) -> None:
        log.info("[PUSH] user=%s title=%s\n%s", msg.user_id, msg.title, msg.body)

    def _webhook_handler(self, msg: PushMessage) -> None:
        if not self.webhook_url:
            return
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.post(self.webhook_url, json={
                "user_id": msg.user_id, "title": msg.title, "body": msg.body,
                "template": msg.template, "timestamp": msg.timestamp.isoformat(), "extras": msg.extras,
            })
            response.raise_for_status()

    def _pushplus_handler(self, msg: PushMessage) -> None:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            response = client.post(PUSHPLUS_URL, json={
                "token": self.pushplus_token, "title": msg.title,
                "content": msg.body, "template": msg.template,
            })
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("PushPlus returned an invalid response") from exc
            if not isinstance(payload, dict) or payload.get("code") != 200:
                raise RuntimeError(f"PushPlus rejected delivery with code {payload.get('code') if isinstance(payload, dict) else 'invalid'}")


_REPORT_STYLES = {
    "blockquote": "margin:0 0 18px;padding:10px 12px;border-left:4px solid #0f766e;background:#f0fdfa;color:#475569",
    "h2": "margin:24px 0 10px;padding:8px 10px;border-left:4px solid #0f766e;border-radius:4px;background:#f1f5f9;color:#0f4c5c;font-size:19px;line-height:1.35",
    "h3": "margin:18px 0 8px;color:#243b53;font-size:16px;line-height:1.4",
    "p": "margin:8px 0;color:#334155;line-height:1.75",
    "ul": "margin:8px 0 12px;padding-left:21px;color:#334155",
    "li": "margin:6px 0;line-height:1.7",
    "strong": "color:#111827;font-weight:650",
}


class _ReportStyleTreeprocessor(Treeprocessor):
    def run(self, root):
        for element in root.iter():
            if element.tag in _REPORT_STYLES:
                element.set("style", _REPORT_STYLES[element.tag])
        return root


class _ReportStyleExtension(Extension):
    def extendMarkdown(self, markdown):
        markdown.treeprocessors.register(_ReportStyleTreeprocessor(markdown), "vitalis_report_style", 5)


def _render_report_html(lines: list[str]) -> str:
    source = "\n".join(str(line).replace("&", "&amp;").replace("<", "&lt;") for line in lines)
    fragment = Markdown(extensions=[_ReportStyleExtension()]).convert(source)
    return '<div style="max-width:680px;margin:0 auto;padding:14px 14px 22px;box-sizing:border-box;border:1px solid #dbe4e8;border-radius:8px;background:#ffffff;color:#1f2937;font-family:Arial,sans-serif;font-size:15px;line-height:1.65;letter-spacing:0;word-break:break-word">' + fragment + "</div>"


def _render_morning(briefing: dict) -> tuple[str, list[str]]:
    date = briefing.get("date", "日期未提供")
    plan = briefing.get("action_plan") or {}
    primary = plan.get("primary_session") or {}
    title_label = primary.get("title") or briefing.get("action_label", "今日安排")
    title = f"Vitalis 晨报 · {date} · {title_label}"
    lines = _timing_lines(briefing) + [f"# 晨报 · {date}", "", *briefing.get("summary", [])]
    retrospective = bool((briefing.get("report_context") or {}).get("delivery_metadata", {}).get("retrospective"))
    for section in briefing.get("sections", []):
        if retrospective and section.get("key") == "today_plan":
            continue
        lines.extend(["", f"## {section.get('title', '分析')}", ""])
        lines.extend(f"- {item}" for item in section.get("facts", []))
        lines.extend(f"- {item}" for item in section.get("interpretation", []))
        lines.extend(f"- 限制：{item}" for item in section.get("limitations", []))
    if briefing.get("cautions"):
        lines.extend(["", "## 必要限制", ""])
        lines.extend(f"- {item}" for item in briefing["cautions"])
    safety = MorningBriefingEngine.safety_lines(briefing)
    if safety:
        lines.extend(["", "## 安全限制", ""])
        lines.extend(f"- {item}" for item in safety)
    return title, lines


def _render_report_briefing(payload: dict, label: str) -> tuple[str, list[str]]:
    period = "月报" if label == "月报" else "周报"
    end = payload.get("period_end", payload.get("date", "日期未提供"))
    context = payload.get("report_context") or {}
    metadata = context.get("delivery_metadata") or {}
    suffix = "补发" if metadata.get("retrospective") else ""
    title = f"Vitalis {label}{suffix} · {end}"
    lines = _timing_lines(payload) + [f"# {label} · {end}", "", *payload.get("summary", [])]
    if metadata.get("retrospective"):
        lines.extend(["", "> 本报告仅回顾指定日期范围的已记录事实，不提供当前、今晚或明天的处方。"])
    for section in payload.get("sections", []):
        lines.extend(["", f"## {section.get('title', '分析')}", ""])
        lines.extend(f"- {item}" for item in section.get("facts", []))
        lines.extend(f"- {item}" for item in section.get("interpretation", []))
        lines.extend(f"- 限制：{item}" for item in section.get("limitations", []))
    return title, lines


def _render_evening(payload: dict) -> tuple[str, list[str]]:
    if payload.get("period") != "evening" or not payload.get("sections"):
        payload = EveningBriefingEngine().build(payload).model_dump(mode="json")
    return _render_report_briefing(payload, "晚报")


def _timing_lines(payload: dict) -> list[str]:
    context = payload.get("report_context") or {}
    timing = as_of_line(context)
    lines = [f"> {timing} 不代表所有指标在此时测量。"] if timing else ["> 分析截止时刻未提供；日期汇总不代表实时测量。"]
    metadata = context.get("delivery_metadata") or {}
    if metadata.get("sync_degraded"):
        notice = "本次同步等待超时" if metadata.get("sync_status") == "timeout" else "部分数据尚未完成更新"
        lines.extend(["", f"> {notice}，本报告使用已保存且符合日期要求的数据。"])
    return lines


# Kept as a small public-compatible helper for callers that render coach steps.
def _render_coach_actions(action_plan: dict) -> list[str]:
    primary = action_plan.get("primary_session") or {}
    if not primary:
        return ["今天没有生成训练安排。"]
    lines = [f"### 主要：{primary.get('title', '训练')} · {primary.get('intensity_label', '')}", "", primary.get("focus", "")]
    for step in primary.get("steps", []):
        details = []
        if step.get("duration_minutes"):
            details.append(f"{step['duration_minutes']} 分钟")
        if step.get("sets"):
            details.append(f"{step['sets']} 组")
        if step.get("repetitions"):
            details.append(str(step["repetitions"]))
        lines.append(f"{step.get('order', '')}. {step.get('name', '步骤')}" + (f"：{'；'.join(details)}" if details else ""))
    return lines
