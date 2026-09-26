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
from vitalis.intelligence.report_formatting import as_of_line, unique

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
        log.info("[PUSH] report rendered; template=%s", msg.template)

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


def _report_summary(payload: dict) -> list[str]:
    sections = payload.get("sections") or []
    details = [
        item for section in sections
        for key in ("facts", "interpretation")
        for item in section.get(key) or []
    ]
    return unique([
        item for item in payload.get("summary") or []
        if not any(detail and (item == detail or item.endswith(detail)) for detail in details)
    ])


def _compact_period_metric(line: str) -> str:
    if "本期有效日" not in line and "本期有记录" not in line:
        return line
    parts = line.rstrip("。").split("；")
    selected = [parts[0]]
    current = next((item for item in parts[1:] if item.startswith(("本期有效日", "本期有记录"))), None)
    if current:
        selected.append(current)
    total = next((item for item in parts[1:] if "已记录小计" in item or "合计 " in item or item.startswith("本期 ")), None)
    if total:
        selected.append(total.split("，均值变化")[0].split("，完整日均值变化")[0])
    average = next((item for item in parts[1:] if item.startswith("有记录日均")), None)
    if average:
        selected.append(average)
    return "；".join(selected) + "。" if len(selected) > 1 else line


def _display_facts(section: dict, period: str | None = None) -> list[str]:
    facts = [
        str(item).replace("（Zepp 汇总）", "").replace("（Zepp 厂商汇总）", "")
        for item in section.get("facts") or []
        if not str(item).startswith((
            "设备睡眠评分", "设备准备度", "设备能量评分", "设备压力日记录",
            "设备训练负荷指数", "设备训练负荷 ", "平均压力评分", "压力采样：",
        ))
    ]
    key = section.get("key")

    def first(*prefixes: str) -> str | None:
        return next((item for item in facts if item.startswith(prefixes)), None)

    if key == "sleep":
        timing = [first(prefix) for prefix in ("睡眠时长", "入睡", "醒来", "夜间醒来")]
        headline = "；".join(item.rstrip("。") for item in timing if item)
        comparison = first("睡眠时长与个人参照")
        return [*([headline + "。"] if headline else facts[:1]), *([comparison] if comparison else [])]
    if key in {"yesterday_activity", "today_activity", "activity"}:
        activity = [first(prefix) for prefix in ("步数", "活动距离", "活动时长")]
        headline = "；".join(item.rstrip("。") for item in activity if item)
        energies = unique([item for item in facts if "热量" in item or "总消耗" in item])[:2]
        return unique([*([headline + "。"] if headline else facts[:1]), *energies])
    if key in {"recovery", "sleep_recovery"}:
        if key == "sleep_recovery":
            return facts[:3]
        hrv = first("睡眠 HRV", "HRV")
        missing_hrv = hrv if hrv and "未取得" in hrv else None
        if missing_hrv:
            hrv = None
        heart = first("静息心率", "睡眠静息心率", "夜间心率中位数")
        if hrv or heart:
            return unique([hrv, heart])
        other = next((item for prefix in (
            "夜间血氧中位数", "夜间呼吸频率", "夜间皮肤温度", "夜间最低五分钟心率中位数",
        ) if (item := first(prefix))), None)
        return unique([other, missing_hrv]) if other else facts[:1]
    if key in {"observed_training", "today_plan"}:
        return facts
    if key == "training":
        if period == "weekly":
            return [item for item in (
                first("训练场次"), first("跑步 "), first("力量："),
            ) if item] or facts[:2]
        return [item for item in facts if not item.startswith(("第 ", "跑步心率分布："))]
    if key == "intraday":
        return [item for item in facts if item.startswith("已记录心率")] or facts[:1]
    if key == "training_activity":
        chosen = [first("训练场次", "已记录训练场次"), first("跑步", "已记录跑步"), first("力量：", "已记录力量：")]
        activity = first("步数：", "日常步数：")
        energy = next((item for item in facts if ("热量" in item or "总消耗" in item) and "本期有记录" in item), None)
        selected = [*chosen, _compact_period_metric(activity) if activity else None, _compact_period_metric(energy) if energy else None]
        return unique([item for item in selected if item]) or facts[:2]
    if key == "activity_feedback":
        chosen = [first("活动有效"), next((item for item in facts if "热量" in item or "总消耗" in item), None), first("已记录主观反馈")]
        return unique([_compact_period_metric(item) for item in chosen if item]) or facts[:2]
    if key == "actions":
        return facts[:1]
    return facts[:2]


def _section_lines(section: dict, displayed: set[str] | None = None, period: str | None = None) -> list[str]:
    lines = ["", f"## {section.get('title', '分析')}", ""]
    facts = [item for item in _display_facts(section, period) if displayed is None or item not in displayed]
    interpretation = [
        item for item in unique(section.get("interpretation") or [])
        if item not in facts and (displayed is None or item not in displayed)
    ]
    key = section.get("key")
    if key not in {"today_plan", "training"}:
        important = [item for item in interpretation if "HRV" in item and ("分歧" in item or "不一致" in item)]
        interpretation = unique(interpretation[:2] + important)
    shown_interpretation = []
    if facts:
        lines.append(f"**{facts[0]}**")
        lines.extend(f"- {item}" for item in facts[1:])
    else:
        shown_interpretation = interpretation[:1]
        lines.extend(f"- {item}" for item in shown_interpretation)
        interpretation = interpretation[1:]
    lines.extend(f"- {item}" for item in interpretation)
    notes = unique([
        item for item in section.get("limitations") or []
        if item not in facts and item not in interpretation
        and (displayed is None or item not in displayed)
    ])
    if notes:
        lines.extend(["", "### 数据说明", ""])
        lines.extend(f"- {item}" for item in notes)
    if displayed is not None:
        displayed.update([*facts, *shown_interpretation, *interpretation, *notes])
    return lines


def _render_morning(briefing: dict) -> tuple[str, list[str]]:
    date = briefing.get("date", "日期未提供")
    metadata = (briefing.get("report_context") or {}).get("delivery_metadata") or {}
    facts_only = bool(metadata.get("facts_only"))
    if facts_only:
        title = f"Vitalis 晨报 · {date}"
    else:
        plan = briefing.get("action_plan") or {}
        primary = plan.get("primary_session") or {}
        title_label = primary.get("title") or briefing.get("action_label", "今日安排")
        title = f"Vitalis 晨报 · {date} · {title_label}"
    summary = _report_summary(briefing)
    lines = _timing_lines(briefing) + [f"# 晨报 · {date}", "", *summary]
    retrospective = bool(metadata.get("retrospective"))
    sections = [
        section for section in briefing.get("sections", [])
        if (not facts_only or section.get("key") in {
            "sleep", "recovery", "yesterday_activity", "observed_training", "today_activity",
        })
        and (not retrospective or section.get("key") != "today_plan")
    ]
    displayed = set(summary)
    for section in sections:
        lines.extend(_section_lines(section, displayed, period="morning"))
    safety = MorningBriefingEngine.safety_lines(briefing) if not facts_only else []
    cautions = unique([
        item for item in briefing.get("cautions") or []
        if item not in displayed and item not in safety
        and not (metadata.get("sync_degraded") and item.startswith("本次同步"))
    ])
    if cautions:
        lines.extend(["", "## 需要留意", ""])
        lines.extend(f"- {item}" for item in cautions)
    if safety:
        lines.extend(["", "## 停止条件", ""])
        lines.extend(f"- {item}" for item in safety)
    return title, lines


def _render_report_briefing(payload: dict, label: str) -> tuple[str, list[str]]:
    end = payload.get("period_end", payload.get("date", "日期未提供"))
    context = payload.get("report_context") or {}
    metadata = context.get("delivery_metadata") or {}
    suffix = "补发" if metadata.get("retrospective") else ""
    title = f"Vitalis {label}{suffix} · {end}"
    summary = _report_summary(payload)
    lines = _timing_lines(payload) + [f"# {label} · {end}", "", *summary]
    if metadata.get("retrospective"):
        lines.extend(["", "> 本报告仅回顾指定日期范围的已记录事实，不提供当前、今晚或明天的处方。"])
    displayed = set(summary)
    for section in payload.get("sections", []):
        lines.extend(_section_lines(section, displayed, period=payload.get("period")))
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
