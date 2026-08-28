"""推送服务：支持 Webhook、Server 酱等渠道推送每日健康分析。

当前阶段先实现本地日志推送，后续可接入：
  - 企业微信机器人
  - Server 酱 / PushPlus
  - Webhook 回调
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import httpx

log = logging.getLogger("vitalis.push")


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

    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url
        self._handlers: list[Callable[[PushMessage], None]] = []
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """注册默认推送处理器。"""
        self._handlers.append(self._log_handler)
        if self.webhook_url:
            self._handlers.append(self._webhook_handler)

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
            title = f"Vitalis Evening · {decision['action']}"
            body_lines = [
                f"今日运动：{training.get('today_duration_minutes')} min",
                f"今日负荷：{training.get('today_load')}",
                f"7日负荷：{training.get('load_7d')}",
                f"恢复状态：{features['recovery']['state']}",
            ]
        else:
            sleep = features["sleep"]
            hrv = features["hrv"]
            title = f"Vitalis Morning · {decision['action']}"
            body_lines = [
                f"恢复状态：{features['recovery']['state']}",
                f"睡眠：{sleep.get('duration_minutes')} min",
                f"HRV：{hrv.get('value_ms')} ms",
                f"训练建议：{decision['action']} / {decision['intensity']}",
            ]
        if decision.get("drivers"):
            body_lines.append("依据：" + ", ".join(decision["drivers"]))
        if decision.get("limitations"):
            body_lines.append("限制：" + ", ".join(decision["limitations"]))
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
