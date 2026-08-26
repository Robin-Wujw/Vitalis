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

    def push_daily_summary(self, user_id: str, summary: dict) -> dict:
        """推送每日健康摘要。"""
        score = summary.get("overall_score", "N/A")
        sleep = summary.get("recovery_level", "unknown")
        training = summary.get("training_readiness", "unknown")
        stress = summary.get("stress_level", "unknown")
        explanation = summary.get("explanation", "")

        title = f"🏃 今日健康报告 · 恢复分 {score}"
        body_lines = [
            f"睡眠：{sleep}",
            f"训练就绪：{training}",
            f"压力：{stress}",
            "",
            explanation[:200] + "..." if len(explanation) > 200 else explanation,
        ]
        msg = PushMessage(
            title=title,
            body="\n".join(body_lines),
            user_id=user_id,
            extras=summary,
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
