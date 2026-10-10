"""Render report projections and deliver them through configured notification handlers."""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from vitalis.config import settings
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

log = logging.getLogger("vitalis.push")
PUSHPLUS_URL = "https://www.pushplus.plus/send"
PUSHPLUS_QUERY_URL = (
    "https://www.pushplus.plus/api/open/message/sendMessageResult"
)
# PushPlus rejects a body above 20,000 characters (code 999); Markdown counts as
# the HTML it converts to. Reports render to a lower budget to stay clear of it.
PUSHPLUS_CONTENT_LIMIT = 20_000
PUSHPLUS_CONTENT_BUDGET = 18_000
_ALLOWED_TEMPLATES = {"markdown", "html"}


def pushplus_content_length(template: str) -> Callable[[str], int]:
    """How PushPlus counts a body of this template against its content limit."""
    from vitalis.intelligence.report_rendering import markdown_html_length, provider_text_length

    return markdown_html_length if template == "markdown" else provider_text_length


class NotificationSendError(RuntimeError):
    """Transport failure with an explicit remote-outcome classification."""

    def __init__(
        self,
        message: str = "notification delivery failed",
        *,
        ambiguous: bool,
        retryable: bool | None = None,
        provider_id: str | None = None,
        provider_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.retryable = (not ambiguous) if retryable is None else retryable
        self.provider_id = provider_id
        self.provider_status = provider_status


class NotificationQueryError(RuntimeError):
    """A status query failed without changing the already accepted send."""


@dataclass
class PushMessage:
    title: str
    body: str
    user_id: str
    template: str = "markdown"
    timestamp: datetime = field(default_factory=datetime.now)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PushStatus:
    """Provider outcome returned by the PushPlus send or status query."""

    status: str
    provider_id: str | None = None
    provider_status: str | None = None
    send_attempt_id: str | None = None
    poll_attempt_id: str | None = None
    retryable: bool = False
    ambiguous: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider_id": self.provider_id,
            "provider_status": self.provider_status,
            "send_attempt_id": self.send_attempt_id,
            "poll_attempt_id": self.poll_attempt_id,
            "retryable": self.retryable,
            "ambiguous": self.ambiguous,
        }


def _http_status_error_is_ambiguous(exc: httpx.HTTPStatusError) -> bool:
    """Only an explicit 4xx response is safe to classify as rejected."""
    try:
        status_code = int(exc.response.status_code)
    except (AttributeError, TypeError, ValueError):
        return True
    return not 400 <= status_code < 500


def _response_status_error(response: object, *, operation: str) -> NotificationSendError | None:
    """Classify an HTTP response without logging its body."""
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code >= 400:
        ambiguous = not 400 <= status_code < 500
        return NotificationSendError(
            f"{operation} response uncertain" if ambiguous else f"{operation} rejected",
            ambiguous=ambiguous,
            retryable=not ambiguous,
        )
    try:
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
    except httpx.HTTPStatusError as exc:
        ambiguous = _http_status_error_is_ambiguous(exc)
        return NotificationSendError(
            f"{operation} response uncertain" if ambiguous else f"{operation} rejected",
            ambiguous=ambiguous,
            retryable=not ambiguous,
        )
    return None


def _render_metadata(rendered: object) -> dict[str, str]:
    """Read the small render evidence contract without retaining report text."""
    return {
        "template": str(getattr(rendered, "template", "")),
        "renderer_version": str(getattr(rendered, "renderer_version", "")),
        "content_sha256": str(getattr(rendered, "content_sha256", "")),
    }


def _payload_for_extras(briefing: object) -> dict[str, Any]:
    if hasattr(briefing, "model_dump"):
        return briefing.model_dump(mode="json")
    if isinstance(briefing, dict):
        return dict(briefing)
    raise TypeError("briefing must be a mapping or a Pydantic model")


def validate_push_message(message: PushMessage) -> None:
    """Apply the one report renderer contract before any transport call."""
    from vitalis.intelligence.report_rendering import validate_report_content

    if message.template not in _ALLOWED_TEMPLATES:
        raise ValueError("notification template must be markdown or html")
    validate_report_content(message.body, message.template)
    digest = hashlib.sha256(message.body.encode("utf-8")).hexdigest()
    expected = message.extras.get("content_sha256")
    if expected is not None and str(expected) != digest:
        raise ValueError("notification content digest does not match body")


class PushService:
    """Transport service; report engines remain the single content projection."""

    def __init__(
        self,
        webhook_url: str = "",
        pushplus_token: str | None = None,
        *,
        pushplus_access_key: str | None = None,
        template: str = "markdown",
        query_max_attempts: int | None = None,
        query_interval_seconds: float | None = None,
        send_attempt_id: str | None = None,
    ) -> None:
        if template not in _ALLOWED_TEMPLATES:
            raise ValueError("notification template must be markdown or html")
        self.webhook_url = webhook_url
        self.pushplus_token = settings.pushplus_token if pushplus_token is None else pushplus_token
        self.pushplus_access_key = (
            settings.pushplus_access_key
            if pushplus_access_key is None else pushplus_access_key
        )
        self.template = template
        self.query_max_attempts = (
            settings.pushplus_query_max_attempts
            if query_max_attempts is None else query_max_attempts
        )
        self.query_interval_seconds = (
            settings.pushplus_query_interval_seconds
            if query_interval_seconds is None else query_interval_seconds
        )
        self.send_attempt_id = send_attempt_id
        self._handlers: list[Callable[[PushMessage], object]] = []
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self._handlers.append(self._log_handler)
        if self.webhook_url:
            self._handlers.append(self._webhook_handler)
        if self.pushplus_token:
            self._handlers.append(self._pushplus_handler)

    def add_handler(self, handler: Callable[[PushMessage], object]) -> None:
        self._handlers.append(handler)

    def push(self, msg: PushMessage) -> dict[str, Any]:
        validate_push_message(msg)
        results: dict[str, Any] = {}
        for handler in self._handlers:
            name = getattr(handler, "__name__", handler.__class__.__name__)
            try:
                result = handler(msg)
                if isinstance(result, PushStatus):
                    result = result.as_dict()
                if isinstance(result, dict):
                    results[name] = result.get("status", "ok")
                    if name == "_pushplus_handler":
                        results["_pushplus_result"] = result
                else:
                    results[name] = "ok"
            except NotificationSendError as exc:
                outcome = "uncertain" if exc.ambiguous else "failed"
                results[name] = "error: delivery failed"
                results.setdefault("_delivery_outcome", outcome)
                if name == "_pushplus_handler":
                    results["_pushplus_result"] = PushStatus(
                        outcome,
                        provider_id=exc.provider_id,
                        provider_status=exc.provider_status,
                        send_attempt_id=msg.extras.get("send_attempt_id"),
                        retryable=exc.retryable,
                        ambiguous=exc.ambiguous,
                    ).as_dict()
                log.warning(
                    "push handler failed: handler=%s outcome=%s", name, outcome
                )
            except Exception:  # noqa: BLE001 - isolate user-provided handlers
                results[name] = "error: delivery failed"
                results.setdefault("_delivery_outcome", "failed")
                if name == "_pushplus_handler":
                    results["_pushplus_result"] = PushStatus(
                        "failed",
                        send_attempt_id=msg.extras.get("send_attempt_id"),
                        retryable=True,
                    ).as_dict()
                log.warning("push handler failed: handler=%s outcome=failed", name)
        if self.pushplus_token and "_pushplus_handler" not in results:
            # A custom handler is not evidence that PushPlus delivered anything.
            results["_pushplus_handler"] = "not_configured"
            results["_pushplus_result"] = PushStatus("not_configured").as_dict()
        return results

    def _render_and_push(self, user_id: str, briefing: object) -> dict[str, Any]:
        from vitalis.intelligence.report_rendering import render_report

        rendered = render_report(
            briefing, target=self.template,
            max_length=PUSHPLUS_CONTENT_BUDGET if self.pushplus_token else None,
            measure=pushplus_content_length(self.template),
        )
        metadata = _render_metadata(rendered)
        payload = _payload_for_extras(briefing)
        payload.update(metadata)
        message = PushMessage(
            title=str(getattr(rendered, "title", "Vitalis 报告")),
            body=str(getattr(rendered, "content", "")),
            user_id=user_id,
            template=str(getattr(rendered, "template", self.template)),
            extras={**payload, **metadata, **({"send_attempt_id": self.send_attempt_id} if self.send_attempt_id else {})},
        )
        results = self.push(message)
        results["_render"] = metadata
        return results

    def push_daily_profile(self, user_id: str, profile, period: str = "morning") -> dict:
        if period == "morning":
            briefing = MorningBriefingEngine().build_payload(
                profile,
                (profile if isinstance(profile, dict) else {}).get("delivery_metadata"),
            )
        elif period == "evening":
            briefing = EveningBriefingEngine().build(profile)
        else:
            raise ValueError("period must be morning or evening")
        return self._render_and_push(user_id, briefing)

    def push_morning_briefing(self, user_id: str, briefing) -> dict:
        return self._render_and_push(user_id, briefing)

    def push_weekly_profile(self, user_id: str, profile) -> dict:
        briefing = WeeklyBriefingEngine().build(profile)
        return self._render_and_push(user_id, briefing)

    def push_monthly_profile(self, user_id: str, profile) -> dict:
        """Explicit monthly capability; this method does not schedule monthly delivery."""
        briefing = MonthlyBriefingEngine().build(profile)
        return self._render_and_push(user_id, briefing)

    @staticmethod
    def _log_handler(msg: PushMessage) -> None:
        log.info("[PUSH] report rendered; template=%s", msg.template)

    def _webhook_handler(self, msg: PushMessage) -> None:
        if not self.webhook_url:
            return
        try:
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                response = client.post(self.webhook_url, json={
                    "user_id": msg.user_id, "title": msg.title, "body": msg.body,
                    "template": msg.template, "timestamp": msg.timestamp.isoformat(),
                    "extras": msg.extras,
                })
                error = _response_status_error(response, operation="notification")
                if error is not None:
                    raise error
        except NotificationSendError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise NotificationSendError("notification transport timeout", ambiguous=True) from exc
        except OSError as exc:
            raise NotificationSendError("notification transport failed", ambiguous=True) from exc

    def _pushplus_handler(self, msg: PushMessage) -> dict[str, Any]:
        send_attempt_id = str(
            msg.extras.get("send_attempt_id") or self.send_attempt_id or uuid4().hex
        )
        try:
            if pushplus_content_length(msg.template)(msg.body) > PUSHPLUS_CONTENT_LIMIT:
                raise NotificationSendError(
                    "notification content exceeds provider limit", ambiguous=False, retryable=False
                )
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                response = client.post(PUSHPLUS_URL, json={
                    "token": self.pushplus_token,
                    "title": msg.title,
                    "content": msg.body,
                    "template": msg.template,
                })
                error = _response_status_error(response, operation="notification")
                if error is not None:
                    error.args = (str(error),)
                    raise error
                try:
                    payload = response.json()
                except (TypeError, ValueError) as exc:
                    raise NotificationSendError(
                        "notification response uncertain", ambiguous=True, retryable=False
                    ) from exc
                if not isinstance(payload, dict) or payload.get("code") != 200:
                    raise NotificationSendError(
                        "notification rejected", ambiguous=False, retryable=True
                    )
                provider_id = payload.get("data")
                if not isinstance(provider_id, str) or not provider_id.strip():
                    raise NotificationSendError(
                        "notification response uncertain", ambiguous=True, retryable=False
                    )
                return PushStatus(
                    "accepted",
                    provider_id=provider_id.strip(),
                    provider_status="accepted",
                    send_attempt_id=send_attempt_id,
                ).as_dict()
        except NotificationSendError as exc:
            exc.provider_id = exc.provider_id or None
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise NotificationSendError(
                "notification transport timeout", ambiguous=True, retryable=False
            ) from exc
        except OSError as exc:
            raise NotificationSendError(
                "notification transport failed", ambiguous=True, retryable=False
            ) from exc

    def query_pushplus(self, provider_id: str, *, poll_attempt_id: str | None = None) -> dict[str, Any]:
        """Query an accepted PushPlus message without ever issuing another POST."""
        if not provider_id:
            raise ValueError("provider_id is required for a PushPlus status query")
        if not self.pushplus_access_key:
            return PushStatus("accepted", provider_id=provider_id).as_dict()
        poll_attempt_id = poll_attempt_id or uuid4().hex
        try:
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                response = client.get(
                    f"{PUSHPLUS_QUERY_URL}?shortCode={quote(provider_id, safe='')}",
                    headers={"access-key": self.pushplus_access_key},
                )
                if getattr(response, "status_code", 200) >= 400:
                    return PushStatus(
                        "accepted", provider_id=provider_id,
                        provider_status="accepted", poll_attempt_id=poll_attempt_id,
                    ).as_dict()
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    return PushStatus(
                        "accepted", provider_id=provider_id,
                        provider_status="accepted", poll_attempt_id=poll_attempt_id,
                    ).as_dict()
                if not isinstance(payload, dict) or payload.get("code") != 200:
                    return PushStatus(
                        "accepted", provider_id=provider_id,
                        provider_status="accepted", poll_attempt_id=poll_attempt_id,
                    ).as_dict()
                data = payload.get("data")
                if isinstance(data, dict):
                    raw_status = data.get("status")
                else:
                    raw_status = None
                raw_provider_status = str(raw_status) if raw_status is not None else "unknown"
                provider_status = {
                    "0": "pending", "1": "pending", "2": "delivered", "3": "failed",
                }.get(raw_provider_status, "unknown")
                status = {
                    "0": "accepted", "1": "accepted", "2": "delivered", "3": "failed",
                }.get(raw_provider_status, "accepted")
                return PushStatus(
                    status,
                    provider_id=provider_id,
                    provider_status=provider_status,
                    poll_attempt_id=poll_attempt_id,
                    retryable=status == "failed",
                ).as_dict()
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            # A query timeout never changes accepted into failed and never retries POST.
            return PushStatus(
                "accepted", provider_id=provider_id,
                provider_status="accepted", poll_attempt_id=poll_attempt_id,
            ).as_dict()

    # Explicit spelling used by scheduler code and tests.
    poll_pushplus = query_pushplus
