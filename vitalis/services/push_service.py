"""Push already-rendered daily health messages to configured transports."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import httpx

log = logging.getLogger("vitalis.push")
PUSHPLUS_URL = "https://www.pushplus.plus/send"


@dataclass
class PushMessage:
    """一条推送消息。"""

    title: str
    body: str
    user_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    extras: dict = field(default_factory=dict)


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
        payload = profile.model_dump(mode="json") if hasattr(profile, "model_dump") else profile
        decision = payload["decision"]
        features = payload["features"]
        if period == "evening":
            training = features["training"]
            title = f"Vitalis 晚间总结 · {decision['action_label']}"
            body_lines = [
                f"今日运动：{_display(training.get('today_duration_minutes'))} 分钟",
                f"今日负荷：{_display(training.get('today_load'))}",
                f"近 7 日负荷：{_display(training.get('load_7d'))}",
                f"负荷状态：{training.get('load_state_label', '数据不足')}",
                f"恢复状态：{features['recovery'].get('state_label', '数据不足')}",
            ]
            today = [
                workout for workout in training.get("recent_workouts", [])
                if workout.get("date") == payload.get("date")
            ]
            for workout in today:
                body_lines.append(
                    f"训练记录：{workout['sport_mode_label']}，"
                    f"{workout['duration_minutes']} 分钟，"
                    f"类型识别置信度{workout['recognition_confidence_label']}"
                )
        else:
            sleep = features["sleep"]
            hrv = features["hrv"]
            title = f"Vitalis 晨间建议 · {decision['action_label']}"
            body_lines = [
                f"恢复状态：{features['recovery'].get('state_label', '数据不足')}",
                f"睡眠：{_display(sleep.get('duration_minutes'))} 分钟",
                f"心率变异性（HRV）：{_display(hrv.get('value_ms'))} 毫秒",
                f"静息心率（RHR）：{_display(hrv.get('rhr_bpm'))} 次/分钟",
                f"训练建议：{decision['action_label']}，{decision['intensity_label']}",
                f"建议置信度：{decision['confidence_label']}",
            ]
        if decision.get("driver_labels"):
            body_lines.append("判断依据：" + "；".join(decision["driver_labels"]))
        if decision.get("limitation_labels"):
            body_lines.append("数据限制：" + "；".join(decision["limitation_labels"]))
        if decision.get("prescriptions"):
            body_lines.append(decision.get("prescription_guidance") or "训练方案：")
            body_lines.extend(_render_prescriptions(decision["prescriptions"]))
        msg = PushMessage(
            title=title,
            body="\n".join(body_lines),
            user_id=user_id,
            extras=payload,
        )
        return self.push(msg)

    # ---- 内置处理器 ----

    @staticmethod
    def _log_handler(msg: PushMessage) -> None:
        log.info("[PUSH] user=%s title=%s\n%s", msg.user_id, msg.title, msg.body)

    def _webhook_handler(self, msg: PushMessage) -> None:
        if not self.webhook_url:
            return
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    self.webhook_url,
                    json={
                        "user_id": msg.user_id,
                        "title": msg.title,
                        "body": msg.body,
                        "timestamp": msg.timestamp.isoformat(),
                        "extras": msg.extras,
                    },
                )
        except Exception:
            log.warning("webhook push failed")

    def _pushplus_handler(self, msg: PushMessage) -> None:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                PUSHPLUS_URL,
                json={
                    "token": self.pushplus_token,
                    "title": msg.title,
                    "content": msg.body,
                    "template": "markdown",
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


def _display(value) -> str:
    return "暂无" if value is None else str(value)


def _render_prescriptions(prescriptions: list[dict]) -> list[str]:
    lines: list[str] = []
    for prescription in prescriptions:
        duration = prescription.get("total_duration_minutes")
        duration_text = f"（{duration[0]}–{duration[1]} 分钟）" if duration else ""
        lines.append(f"训练方案：{prescription['title']}{duration_text}")
        lines.append(f"训练目标：{prescription['goal']}")
        for step in prescription.get("steps", []):
            details = []
            if step.get("duration_minutes"):
                low, high = step["duration_minutes"]
                details.append(f"{low}–{high} 分钟")
            if step.get("sets"):
                details.append(f"{step['sets']} 组")
            if step.get("repetitions"):
                details.append(step["repetitions"])
            if step.get("rest_seconds"):
                low, high = step["rest_seconds"]
                details.append(f"组间休息 {low}–{high} 秒")
            if step.get("intensity"):
                details.append(step["intensity"])
            suffix = "，".join(details)
            lines.append(f"{step['order']}. {step['name']}：{suffix}".rstrip("："))
            lines.extend(f"   {instruction}" for instruction in step.get("instructions", []))
        lines.extend(f"进阶：{item}" for item in prescription.get("progression", []))
        lines.extend(f"注意：{item}" for item in prescription.get("cautions", []))
    return lines
