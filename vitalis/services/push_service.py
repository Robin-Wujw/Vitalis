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
            with httpx.Client(timeout=10.0, trust_env=False) as client:
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
        with httpx.Client(timeout=10.0, trust_env=False) as client:
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
    action_plan = decision["action_plan"]
    primary = action_plan.get("primary_session")
    features = payload["features"]
    states = payload.get("states", {})
    sleep = features["sleep"]
    hrv = features["hrv"]
    overnight_vitals = features["overnight_vitals"]
    training = features["training"]
    report_date = payload["date"]
    action_title = primary["title"] if primary else decision["action_label"]
    title = f"Vitalis 晨报 · {report_date} · {action_title}"
    lines = _metadata(payload)
    lines.extend([
        "",
        "## 今日结论",
        "",
        _daily_conclusion(decision, states, action_plan),
        "",
        "## 今天做什么",
        "",
    ])
    lines.extend(_render_coach_actions(action_plan))
    lines.extend(["", "## 为什么", ""])
    lines.extend(_render_coach_reasons(
        sleep, hrv, overnight_vitals, training, action_plan
    ))
    notable = _render_notable_event(payload)
    if notable:
        lines.extend(["", "## 最近最值得注意", "", notable])
    cautions = _render_coach_cautions(payload)
    if cautions:
        lines.extend(["", "## 必要提醒", "", *(f"- {item}" for item in cautions)])
    return title, lines


def _render_evening(payload: dict) -> tuple[str, list[str]]:
    features = payload["features"]
    states = payload.get("states", {})
    training = features["training"]
    report_date = payload["date"]
    today = [
        workout
        for workout in training.get("recent_workouts", [])
        if workout.get("date") == report_date
    ]
    title_label = today[0]["sport_mode_label"] if len(today) == 1 else "今日训练回顾"
    title = f"Vitalis 晚报 · {report_date} · {title_label}"
    lines = _metadata(payload)
    lines.extend([
        "",
        "## 今天完成了什么",
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
            lines.append(
                f"- **{workout['sport_mode_label']}** · {' · '.join(details)}"
            )
    else:
        lines.append("今天没有记录到正式训练，不需要在晚上补训练。")

    details = _render_today_workout_details(training, report_date)
    if details:
        lines.extend(["", "## 训练质量", "", *details])

    load_label = states.get("training_load_label") or training.get(
        "load_state_label", "负荷数据不足"
    )
    lines.extend(["", "## 今晚怎么收尾", ""])
    if today:
        if training.get("load_state") == "ELEVATED":
            lines.append(
                f"今天已经完成训练，当前{load_label}。今晚不再追加跑步或力量，正常进食、补水并按时睡觉。"
            )
        else:
            lines.append(
                f"今天已经完成训练，当前{load_label}。今晚不再补训练，正常进食、补水并按时睡觉。"
            )
        lines.extend([
            "",
            "训练后的主观用力（1–10 分）和主要酸痛部位还没有记录；这两项会直接用于调整下一次训练剂量。",
        ])
    else:
        lines.append(f"当前{load_label}。按正常作息休息，明早再根据整夜数据安排训练。")
    return title, lines


def _metadata(payload: dict) -> list[str]:
    quality = payload.get("data_quality", {}).get("status_label", "数据状态未知")
    return [f"> **数据日期：{payload['date']}** · {quality}"]


def _daily_conclusion(decision: dict, states: dict, action_plan: dict) -> str:
    action = decision["action"]
    recovery = states.get("recovery_label") or "恢复状态暂不明确"
    primary = action_plan.get("primary_session")
    if action in {"TRAIN_HARD", "TRAIN_NORMAL", "TRAIN_LIGHT"} and primary:
        return (
            f"{recovery}，今天做{primary['title']}，"
            f"按{primary['intensity_label']}执行。"
        )
    conclusions = {
        "RECOVERY": f"{recovery}，今天以恢复活动为主。",
        "REST": f"{recovery}，今天休息，不安排正式训练。",
        "INSUFFICIENT_DATA": "恢复数据还不足，今天不根据设备数据安排强度训练。",
    }
    return conclusions[action]


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
    duration_text = f" · {duration[0]}–{duration[1]} 分钟" if duration else ""
    lines = [
        f"### {role}：{session['title']}{duration_text} · {session['intensity_label']}",
        "",
        session["focus"],
    ]
    for step in session.get("steps", []):
        details = []
        if step.get("duration_minutes"):
            low, high = step["duration_minutes"]
            details.append(f"{low}–{high} 分钟")
        if step.get("sets"):
            details.append(f"{step['sets']} 组")
        if step.get("repetitions"):
            details.append(step["repetitions"])
        if step.get("load_kg") is not None:
            details.append(f"{step['load_kg']:g} 千克")
        if step.get("rest_seconds"):
            low, high = step["rest_seconds"]
            details.append(f"休息 {low}–{high} 秒")
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


def _render_coach_reasons(
    sleep: dict,
    hrv: dict,
    overnight_vitals: dict,
    training: dict,
    action_plan: dict,
) -> list[str]:
    lines = []
    sleep_parts = []
    if sleep.get("duration_minutes") is not None:
        sleep_parts.append(f"昨晚睡了 {_minutes_as_hours(sleep['duration_minutes'])}")
    if _interpretable_deviation(sleep.get("duration_deviation")):
        sleep_parts.append(_plain_deviation(sleep["duration_deviation"], "睡眠时长"))
    if sleep.get("bedtime") and sleep.get("wake_time"):
        sleep_parts.append(
            f"睡眠时段 {_clock_minute(sleep['bedtime'])}–{_clock_minute(sleep['wake_time'])}"
        )
    if sleep_parts:
        lines.append(f"- {'；'.join(sleep_parts)}。")

    recovery_parts = []
    if (
        not hrv.get("corroboration_affects_decision")
        and _interpretable_deviation(hrv.get("deviation"))
    ):
        recovery_parts.append(_plain_deviation(hrv["deviation"], "夜间心率变异性"))
    if (
        not hrv.get("corroboration_affects_decision")
        and hrv.get("recent_7d_direction") != "unknown"
        and hrv.get("recent_7d_change_percent") is not None
    ):
        change = hrv["recent_7d_change_percent"]
        trend = "上升" if change > 0 else "下降" if change < 0 else "持平"
        recovery_parts.append(f"近 7 天中位数较此前 7 天{trend} {abs(change):.1f}%")

    night = hrv.get("nocturnal_heart_rate") or {}
    if night.get("status") == "AVAILABLE":
        night_parts = [
            f"睡眠中心率中位数 {night['median_bpm']:g} 次/分钟",
            f"稳定 5 分钟低点 {night['low_5m_bpm']:g} 次/分钟",
        ]
        if night.get("low_5m_time"):
            night_parts[-1] += f"（{night['low_5m_time']}）"
        if night.get("deviation_percent") is not None:
            change = night["deviation_percent"]
            baseline_nights = night.get("baseline_nights") or 28
            if night.get("direction") == "near":
                night_parts.append(
                    f"接近近 {baseline_nights} 晚个人水平（{change:+.1f}%）"
                )
            else:
                relation = "高" if change > 0 else "低" if change < 0 else "一致"
                amount = f" {abs(change):.1f}%" if change else ""
                night_parts.append(f"比近 {baseline_nights} 晚个人水平{relation}{amount}")
        if night.get("second_minus_first_bpm") is not None:
            delta = night["second_minus_first_bpm"]
            movement = "回落" if delta < 0 else "升高" if delta > 0 else "基本不变"
            amount = f" {abs(delta):g} 次/分钟" if delta else ""
            night_parts.append(f"后半夜较前半夜{movement}{amount}")
        recovery_parts.extend(night_parts)
    vitals_reason = _render_overnight_vitals_reason(overnight_vitals)
    if vitals_reason:
        recovery_parts.append(vitals_reason.rstrip("。"))
    if recovery_parts:
        lines.append(f"- {'；'.join(recovery_parts)}。")

    balance = action_plan["weekly_balance"]
    primary = action_plan.get("primary_session")
    optional = action_plan.get("optional_session")
    for reason in (primary or {}).get("personalization_reasons", []):
        if len(lines) >= 4:
            break
        lines.append(f"- {reason}")
    if len(lines) < 4:
        plan_text = (
            f"近 7 天跑步 {balance['running_completed_7d']}/{balance['running_target_7d']} 次、"
            f"力量 {balance['strength_completed_7d']}/{balance['strength_target_7d']} 次"
        )
        if primary:
            plan_text += f"；因此今天先做{primary['title']}"
        if optional:
            plan_text += f"，{optional['title']}只作为可选"
        lines.append(f"- {plan_text}。")
    return lines[:4]


def _render_overnight_vitals_reason(vitals: dict) -> str | None:
    if vitals.get("status") != "AVAILABLE":
        return None
    parts = []
    respiratory = vitals.get("respiratory_rate")
    respiratory_deviation = vitals.get("respiratory_rate_deviation")
    if respiratory is not None:
        text = f"夜间呼吸频率 {respiratory:g} 次/分钟"
        if _interpretable_deviation(respiratory_deviation):
            text += "，" + _plain_deviation(respiratory_deviation, "").strip()
        parts.append(text)

    temperature = vitals.get("skin_temperature_delta_c")
    if temperature is not None and abs(temperature) >= 0.2:
        relation = "高" if temperature > 0 else "低"
        parts.append(f"腕温较厂商基线{relation} {abs(temperature):g} 摄氏度")

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
            text = "夜间血氧下降频率处于个人常态"
        details = []
        if odi is not None:
            details.append(f"{odi:g} 次/小时")
        if minutes is not None:
            details.append(f"覆盖 {minutes} 分钟")
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


def _clock_minute(value: str) -> str:
    return value[:5]


def _plain_deviation(deviation: dict, subject: str) -> str:
    direction = {
        "above": "高于你的个人基线",
        "near": "接近你的个人基线",
        "below": "低于你的个人基线",
        "unknown": "暂时无法和个人基线比较",
    }.get(deviation.get("direction"), "暂时无法和个人基线比较")
    return f"{subject}{direction}"


def _interpretable_deviation(deviation: dict | None) -> bool:
    return bool(deviation and deviation.get("direction") in {"above", "near", "below"})


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
            metrics = [latest["classification_label"], f"{latest['duration_minutes']} 分钟"]
            if latest.get("distance_km") is not None:
                metrics.append(f"{latest['distance_km']} 公里")
            if latest.get("average_pace_seconds_per_km") is not None:
                metrics.append(f"平均配速 {_pace(latest['average_pace_seconds_per_km'])}/公里")
            if latest.get("median_cadence_spm") is not None:
                metrics.append(f"步频中位数 {latest['median_cadence_spm']} 步/分钟")
            if latest.get("cardiac_drift_percent") is not None:
                metrics.append(f"心率漂移 {latest['cardiac_drift_percent']:+g}%")
            lines.append(f"- **跑步**：{' · '.join(metrics)}")

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
            lines.append(f"- **力量**：{' · '.join(metrics)}")
            names = [item["exercise_name"] for item in latest.get("explicit_exercises", [])]
            if names:
                lines.append(f"- **动作**：{'、'.join(names)}")
    return lines


def _pace(seconds: float) -> str:
    rounded = max(int(round(seconds)), 0)
    return f"{rounded // 60}:{rounded % 60:02d}"
