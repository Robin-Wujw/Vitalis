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
        payload = (
            profile.model_dump(mode="json")
            if hasattr(profile, "model_dump")
            else profile
        )
        if period == "morning":
            title, body_lines = _render_morning(payload)
        elif period == "evening":
            title, body_lines = _render_evening(payload)
        else:
            raise ValueError("period must be morning or evening")

        body_lines.extend(_render_limitations(payload))
        msg = PushMessage(
            title=title,
            body="\n".join(body_lines),
            user_id=user_id,
            extras=payload,
        )
        return self.push(msg)

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


def _render_morning(payload: dict) -> tuple[str, list[str]]:
    decision = payload["decision"]
    features = payload["features"]
    states = payload.get("states", {})
    sleep = features["sleep"]
    hrv = features["hrv"]
    recovery = features["recovery"]
    report_date = payload["date"]
    title = f"Vitalis 晨报 · {report_date} · {decision['action_label']}"
    lines = _metadata(payload)
    lines.extend([
        "",
        "## 晨间结论",
        "",
        f"- **恢复判断**：{states.get('recovery_label') or recovery.get('state_label', '数据不足')}",
        f"- **睡眠判断**：{states.get('sleep_label') or sleep.get('status_label', '数据不足')}",
        f"- **负荷判断**：{states.get('training_load_label') or features['training'].get('load_state_label', '数据不足')}",
        f"- **今日安排**：{decision['action_label']} · {decision['intensity_label']}",
        f"- **结论把握**：{decision['confidence_label']}",
        "",
        "## 恢复解读",
        "",
        f"- **积极信号**：{_join_labels(recovery.get('positive_signal_labels'))}",
        f"- **需关注信号**：{_join_labels(recovery.get('negative_signal_labels'))}",
    ])
    if recovery.get("vendor_readiness") is not None:
        lines.append(f"- **厂商准备度（参考）**：{recovery['vendor_readiness']}")
    if recovery.get("vendor_charge") is not None:
        lines.append(f"- **身体电量（参考）**：{recovery['vendor_charge']}")
    lines.extend([
        "",
        "## 睡眠详情",
        "",
        f"- **睡眠**：{_display_with_unit(sleep.get('duration_minutes'), '分钟')}",
    ])
    if sleep.get("duration_deviation"):
        lines.append(
            f"  - 个人基线：{_deviation_text(sleep['duration_deviation'])}"
        )
    clock_parts = []
    if sleep.get("bedtime"):
        clock_parts.append(f"入睡 {sleep['bedtime']}")
    if sleep.get("wake_time"):
        clock_parts.append(f"醒来 {sleep['wake_time']}")
    if clock_parts:
        lines.append(f"- **睡眠时段**：{' · '.join(clock_parts)}")
    stage_parts = [
        f"深睡 {_display_with_unit(sleep.get('deep_minutes'), '分钟')}",
        f"快速眼动睡眠 {_display_with_unit(sleep.get('rem_minutes'), '分钟')}",
        f"清醒 {_display_with_unit(sleep.get('awake_minutes'), '分钟')}",
    ]
    if any(sleep.get(key) is not None for key in ("deep_minutes", "rem_minutes", "awake_minutes")):
        lines.append(f"- **睡眠结构**：{' · '.join(stage_parts)}")
    if sleep.get("regularity_minutes") is not None:
        lines.append(
            f"- **近 7 日入睡规律性**：典型偏差 {sleep['regularity_minutes']} 分钟"
        )
    if sleep.get("vendor_sleep_score") is not None:
        lines.append(f"- **厂商睡眠评分（参考）**：{sleep['vendor_sleep_score']}")
    lines.extend(["", "## 多设备心率融合", ""])
    lines.extend(_render_hrv_fusion(hrv))
    lines.append(
        f"- **心率变异性（HRV）**：{_display_with_unit(hrv.get('value_ms'), '毫秒')}"
        + (f" · {hrv['preferred_device_label']}" if hrv.get("preferred_device_label") else "")
    )
    if hrv.get("deviation"):
        lines.append(f"  - HRV 个人基线：{_deviation_text(hrv['deviation'])}")
    if hrv.get("rhr_bpm") is not None:
        lines.append(
            f"- **静息心率（RHR）**：{_display_with_unit(hrv['rhr_bpm'], '次/分钟')}"
        )
        if hrv.get("rhr_deviation"):
            lines.append(
                f"  - RHR 个人基线：{_deviation_text(hrv['rhr_deviation'])}"
            )
    coverage_lines = _render_heart_rate_coverage(hrv)
    if coverage_lines:
        lines.extend(["", "### 高频心率覆盖", "", *coverage_lines])
    training = features["training"]
    lines.extend([
        "",
        "## 近期负荷与背景",
        "",
        (
            "- **近 7 日训练**："
            f"{_display_with_unit(training.get('duration_7d'), '分钟')} · "
            f"负荷 {_display(training.get('load_7d'))}"
        ),
        f"- **近 28 日负荷**：{_display(training.get('load_28d'))}",
        (
            "- **训练结构**："
            f"有氧 {_display_with_unit(training.get('aerobic_minutes_7d'), '分钟')} · "
            f"力量 {_display(training.get('strength_sessions_7d'))} 次"
        ),
    ])
    lines.extend(_render_trends_and_events(payload))
    lines.extend(
        [
            "",
            "## 训练安排",
            "",
            f"**{decision['action_label']} · {decision['intensity_label']}**",
        ]
    )
    if decision.get("suggested_type_labels"):
        lines.extend(
            [
                "",
                f"- **建议类型**：{_join_labels(decision['suggested_type_labels'])}",
            ]
        )
    if decision.get("duration_minutes"):
        low, high = decision["duration_minutes"]
        lines.append(f"- **建议时长**：{low}–{high} 分钟")
    if decision.get("prescription_guidance"):
        lines.extend(["", decision["prescription_guidance"]])
    if decision.get("prescriptions"):
        lines.extend(["", "## 具体方案", ""])
        lines.extend(_render_prescriptions(decision["prescriptions"]))
    lines.extend(_render_drivers(decision))
    return title, lines


def _render_evening(payload: dict) -> tuple[str, list[str]]:
    decision = payload["decision"]
    features = payload["features"]
    states = payload.get("states", {})
    recovery = features["recovery"]
    training = features["training"]
    report_date = payload["date"]
    today = [
        workout
        for workout in training.get("recent_workouts", [])
        if workout.get("date") == report_date
    ]
    title = f"Vitalis 晚报 · {report_date} · {decision['action_label']}"
    lines = _metadata(payload)
    lines.extend([
        "",
        "## 晚间结论",
        "",
        (
            f"- **今日完成**：{len(today)} 次训练 · "
            f"{_display_with_unit(training.get('today_duration_minutes'), '分钟')}"
        ),
        (
            "- **负荷判断**："
            f"{states.get('training_load_label') or training.get('load_state_label', '数据不足')}"
        ),
        f"- **恢复判断**：{states.get('recovery_label') or recovery.get('state_label', '数据不足')}",
        f"- **今晚安排**：{decision['action_label']} · {decision['intensity_label']}",
        "",
        "## 今日训练",
        "",
    ])
    if today:
        for workout in today:
            details = [
                _display_with_unit(workout.get("duration_minutes"), "分钟"),
                f"厂商负荷 {_display(workout.get('vendor_load'))}",
            ]
            if workout.get("heart_rate_avg_bpm") is not None:
                details.append(f"平均心率 {workout['heart_rate_avg_bpm']} 次/分钟")
            details.append(f"识别置信度{workout['recognition_confidence_label']}")
            lines.append(
                f"- **{workout['sport_mode_label']}** · {' · '.join(details)}"
            )
    else:
        lines.append("- 今天未记录到训练")
    lines.extend([
        "",
        "## 负荷回顾",
        "",
        f"- **今日负荷**：{_display(training.get('today_load'))}",
        (
            "- **近 7 日训练**："
            f"{_display_with_unit(training.get('duration_7d'), '分钟')} · "
            f"负荷 {_display(training.get('load_7d'))}"
        ),
        f"- **当前状态**：{training.get('load_state_label', '数据不足')}",
        "",
        "## 恢复信号",
        "",
        f"- **积极信号**：{_join_labels(recovery.get('positive_signal_labels'))}",
        f"- **需关注信号**：{_join_labels(recovery.get('negative_signal_labels'))}",
    ])
    if features.get("hrv", {}).get("fusion_summary"):
        lines.append(
            f"- **多设备 HRV**：{features['hrv']['fusion_summary']}"
        )
    trend_lines = [
        (
            f"- **{trend['metric_label']}（{trend['window_days']} 日）**："
            f"{trend['direction_label']} · {trend['confidence_label']}置信度"
        )
        for trend in payload.get("trends", [])
        if trend.get("status") == "AVAILABLE"
    ]
    event_lines = [
        (
            f"- **{event['type_label']}**：{event['summary']}"
            f"（{event['severity_label']}，{event['lifecycle_label']}）"
        )
        for event in payload.get("events", [])
        if event.get("lifecycle") != "RESOLVED"
    ]
    lines.extend(["", "## 趋势与事件", ""])
    lines.extend(trend_lines or ["- 暂无可用趋势"])
    lines.extend(event_lines or ["- 暂无进行中的健康事件"])
    if decision.get("prescriptions"):
        lines.extend(["", "## 今晚安排", ""])
        lines.extend(_render_prescriptions(decision["prescriptions"]))
    lines.extend(_render_drivers(decision))
    return title, lines


def _metadata(payload: dict) -> list[str]:
    quality = payload.get("data_quality", {}).get("status_label", "数据状态未知")
    lines = [f"> **数据日期：{payload['date']}** · {quality}"]
    delivery = payload.get("delivery_metadata", {})
    if delivery.get("sync_degraded"):
        lines.append(
            "> **同步提醒**：本次新同步未完整完成，报告使用已存储且通过完整性校验的当天数据。"
        )
    return lines


def _render_hrv_fusion(hrv: dict) -> list[str]:
    lines = []
    if hrv.get("fusion_summary"):
        confidence = hrv.get("fusion_confidence_label") or "未知"
        lines.append(f"- **融合结论**：{hrv['fusion_summary']} · {confidence}把握")
    for stream in hrv.get("streams", []):
        label = stream.get("device_label") or "来源未标识设备"
        site = {
            "upper_arm": "上臂",
            "wrist": "腕部",
            "unknown": "位置未知",
        }.get(stream.get("measurement_site"), "位置未知")
        selected = " · 首选展示" if stream.get("selected") else ""
        details = [
            _display_with_unit(stream.get("value_ms"), "毫秒"),
            f"当天 {stream.get('sample_count_today', 0)} 个样本",
            f"基线 {stream.get('baseline_distinct_days', 0)} 天",
        ]
        if stream.get("deviation"):
            details.append(_deviation_text(stream["deviation"]))
        lines.append(
            f"- **{label}（{site}）**：{' · '.join(details)}{selected}"
        )
    if not lines:
        lines.append("- 暂无可融合的设备级 HRV")
    return lines


def _render_heart_rate_coverage(hrv: dict) -> list[str]:
    lines = []
    for item in hrv.get("heart_rate_coverage", []):
        state = "数值已解码" if item.get("payload_decoded") else "仅覆盖索引，数值尚未解码"
        lines.append(
            f"- **{item['device_label']}**：今日 {item.get('today_coverage_minutes', 0):g} 分钟 · "
            f"近 28 日 {item.get('coverage_hours_28d', 0):g} 小时/"
            f"{item.get('covered_days_28d', 0)} 天 · {state}"
        )
    return lines


def _render_trends_and_events(payload: dict) -> list[str]:
    trend_lines = [
        (
            f"- **{trend['metric_label']}（{trend['window_days']} 日）**："
            f"{trend['direction_label']} · {trend['confidence_label']}置信度"
        )
        for trend in payload.get("trends", [])
        if trend.get("status") == "AVAILABLE"
    ]
    event_lines = [
        f"- **{event['type_label']}**：{event['summary']}"
        for event in payload.get("events", [])
        if event.get("lifecycle") != "RESOLVED"
    ]
    return [
        "",
        "### 趋势与事件",
        "",
        *(trend_lines[:6] or ["- 暂无可用趋势"]),
        *(event_lines[:4] or ["- 暂无进行中的健康事件"]),
    ]


def _render_drivers(decision: dict) -> list[str]:
    labels = decision.get("driver_labels", [])
    if not labels:
        return []
    return ["", "## 判断依据", "", *(f"- {item}" for item in labels)]


def _render_limitations(payload: dict) -> list[str]:
    quality = payload.get("data_quality", {})
    decision = payload["decision"]
    labels = _unique([
        *quality.get("missing_required_signal_labels", []),
        *decision.get("limitation_labels", []),
    ])
    return [
        "",
        "## 数据限制",
        "",
        *(f"- {item}" for item in labels or ["暂无额外数据限制"]),
    ]


def _display(value) -> str:
    return "暂无" if value is None else str(value)


def _display_with_unit(value, unit: str) -> str:
    return "暂无" if value is None else f"{value} {unit}"


def _join_labels(values: list[str] | None) -> str:
    return "；".join(values) if values else "暂无明确识别信号"


def _deviation_text(deviation: dict) -> str:
    direction = {
        "above": "高于 28 天基线",
        "near": "接近 28 天基线",
        "below": "低于 28 天基线",
        "unknown": "暂无法与 28 天基线比较",
    }.get(deviation.get("direction"), "暂无法与 28 天基线比较")
    percent = deviation.get("percent")
    return f"{direction}（{percent:+g}%）" if percent is not None else direction


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _render_prescriptions(prescriptions: list[dict]) -> list[str]:
    lines: list[str] = []
    for prescription in prescriptions:
        duration = prescription.get("total_duration_minutes")
        duration_text = f" · {duration[0]}–{duration[1]} 分钟" if duration else ""
        lines.append(f"### {prescription['title']}{duration_text}")
        lines.extend(["", f"**目标**：{prescription['goal']}", ""])
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
            suffix = " · ".join(details)
            summary = f"{step['order']}. **{step['name']}**"
            lines.append(f"{summary} · {suffix}" if suffix else summary)
            lines.extend(
                f"   - {instruction}"
                for instruction in step.get("instructions", [])
            )
        if prescription.get("progression"):
            lines.extend(["", "**进阶条件**"])
            lines.extend(f"- {item}" for item in prescription["progression"])
        if prescription.get("cautions"):
            lines.extend(["", "**注意事项**"])
            lines.extend(f"- {item}" for item in prescription["cautions"])
        lines.append("")
    return lines
