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
        "## 今日状态",
        "",
        _daily_conclusion(
            decision, states, action_plan, sleep, hrv, overnight_vitals
        ),
        "",
        "## 昨夜数据",
        "",
    ])
    lines.extend(_render_overnight_summary(sleep, hrv, overnight_vitals))
    lines.extend(["", "## 最近 7 天", ""])
    lines.extend(_render_weekly_training_summary(training))
    lines.extend(["", "## 今天安排", ""])
    lines.extend(_render_coach_actions(action_plan))
    plan_reasons = _render_plan_reasons(training, action_plan)
    if plan_reasons:
        lines.extend(["", "## 为什么这样安排", "", *plan_reasons])
    notable = _render_notable_event(payload)
    if notable:
        lines.extend(["", "## 最近最值得注意", "", notable])
    cautions = _render_coach_cautions(payload)
    if cautions:
        lines.extend(["", "## 必要提醒", "", *(f"- {item}" for item in cautions)])
    return title, lines


def _render_evening(payload: dict) -> tuple[str, list[str]]:
    features = payload["features"]
    training = features["training"]
    report_date = payload["date"]
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
        lines.append("今天没有记录到正式训练，按正常节奏收尾即可，不需要在晚上补课。")

    details = _render_today_workout_details(training, report_date)
    if details:
        lines.extend(["", "## 训练表现", "", *details])

    daily_state = _render_evening_daily_state(payload, training)
    if daily_state:
        lines.extend(["", "## 全天状态", "", *daily_state])

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
        text = f"截至昨天的 7 个完整日，设备训练负荷为 {load:g}"
        if reference is not None and change is not None:
            relation = "高" if change > 0 else "低" if change < 0 else "相同"
            amount = f" {abs(change):.1f}%" if change else ""
            text += f"，比此前 3 周周均（{reference:g}）{relation}{amount}"
        lines.append(
            f"- **近期负荷**：{text}。这表示训练刺激减少，不代表恢复变差。"
        )
    return lines


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
    return "今天没有正式训练。晚上不用补训练，保持正常饮食、补水和入睡时间即可。"


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
            "明天先按低负担方向预留，不连续安排质量跑和腿部力量。"
            "具体内容等今晚睡眠、心率变异性和静息心率到齐后再定。"
        )

    next_focus = (training.get("strength") or {}).get("next_focus_label")
    if next_focus:
        rest_prefix = "" if today else "今天没有训练不需要用明天加量补偿。"
        return (
            f"{rest_prefix}下一次力量训练的候选重点是{next_focus}。是否放在明天，"
            "等今晚睡眠、心率变异性和静息心率到齐后再定。"
        )
    if today:
        return "明天的训练强度等今晚睡眠、心率变异性和静息心率到齐后再确定。"
    return (
        "今天没有训练不需要用明天加量补偿。明早根据整夜睡眠、心率变异性和"
        "静息心率正常安排。"
    )


def _sessions_on_day(feature: dict | None, report_date: str) -> list[dict]:
    return [
        item for item in (feature or {}).get("recent_sessions", [])
        if item.get("date") == report_date
    ]


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


def _metadata(payload: dict) -> list[str]:
    quality = payload.get("data_quality", {}).get("status_label", "数据状态未知")
    return [f"> **数据日期：{payload['date']}** · {quality}"]


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
    if stage_parts:
        lines.append(f"- **睡眠结构**：{'；'.join(stage_parts)}。设备分期主要看长期变化。")

    cardiovascular = []
    if hrv.get("value_ms") is not None:
        text = f"心率变异性 {hrv['value_ms']:g} 毫秒"
        hrv_deviation = hrv.get("deviation") or {}
        if hrv_deviation.get("percent") is not None:
            text += f"（较个人基线 {hrv_deviation['percent']:+.1f}%）"
        cardiovascular.append(text)
    if hrv.get("recent_7d_change_percent") is not None:
        cardiovascular.append(
            f"近 7 天较此前 7 天 {hrv['recent_7d_change_percent']:+.1f}%"
        )
    if hrv.get("rhr_bpm") is not None:
        text = f"静息心率 {hrv['rhr_bpm']:g} 次/分钟"
        rhr_deviation = hrv.get("rhr_deviation") or {}
        if rhr_deviation.get("percent") is not None:
            text += f"（较个人基线 {rhr_deviation['percent']:+.1f}%）"
        cardiovascular.append(text)
    if cardiovascular:
        lines.append(f"- **恢复相关**：{'；'.join(cardiovascular)}。")

    night = hrv.get("nocturnal_heart_rate") or {}
    if night.get("status") == "AVAILABLE":
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
        lines.append(f"- **睡眠中心率**：{'；'.join(parts)}。")

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
            metrics = [classification, f"{latest['duration_minutes']} 分钟"]
            if latest.get("distance_km") is not None:
                metrics.append(f"{round(latest['distance_km'], 2):g} 公里")
            if latest.get("average_pace_seconds_per_km") is not None:
                metrics.append(f"平均配速 {_pace(latest['average_pace_seconds_per_km'])}/公里")
            if latest.get("median_cadence_spm") is not None:
                metrics.append(f"步频中位数 {latest['median_cadence_spm']:g} 步/分钟")
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
            names = [item["exercise_name"] for item in latest.get("explicit_exercises", [])]
            if names:
                lines.append(f"- **动作**：{'、'.join(names)}")
            else:
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
