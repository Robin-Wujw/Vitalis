from copy import deepcopy
from html.parser import HTMLParser
from math import log

from vitalis.services.push_service import PUSHPLUS_URL, PushMessage, PushService


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _visible_text(body: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(body)
    return "".join(parser.parts)


def _profile_payload():
    return {
        "date": "2026-08-28",
        "data_quality": {
            "status": "SUFFICIENT",
            "status_label": "数据完整",
            "missing_required_signal_labels": [],
        },
        "facts": {
            "steps": [{
                "value": 8632,
                "provenance": {"source_scope": "user_fused"},
            }],
            "stress": [{
                "value": 31,
                "provenance": {"source_scope": "device"},
            }],
            "stress_relaxed_pct": [{
                "value": 62,
                "provenance": {"source_scope": "device"},
            }],
            "stress_high_pct": [{
                "value": 2,
                "provenance": {"source_scope": "device"},
            }],
        },
        "features": {
            "sleep": {
                "status": "AVAILABLE",
                "status_label": "可用",
                "duration_minutes": 447,
                "bedtime": "00:48:00",
                "wake_time": "08:15:00",
                "deep_minutes": 92,
                "rem_minutes": 106,
                "awake_minutes": 24,
                "regularity_minutes": 18.0,
                "vendor_sleep_score": 84,
                "duration_deviation": {"direction": "above", "percent": 6.5},
            },
            "hrv": {
                "value_ms": 71,
                "deviation": {"direction": "above", "percent": 8.2},
                "preferred_device_label": "Amazfit Helio Strap",
                "fusion_confidence_label": "较高",
                "fusion_summary": "2 台设备相对各自 28 天基线方向一致：高于基线",
                "corroboration_status": "consistent",
                "corroborating_stream_count": 1,
                "corroboration_affects_decision": False,
                "daily_curve": {
                    "metric": "hrv_rmssd",
                    "bin_minutes": 1,
                    "device_id": "helio",
                    "device_label": "Amazfit Helio Strap",
                    "selection_basis": "widest_target_day_coverage",
                    "sample_count": 12,
                    "covered_minutes": 10,
                    "first_sample_time": "00:31",
                    "last_sample_time": "17:58",
                    "points": [
                        {"time": "01:00", "median_ms": 60, "sample_count": 3},
                        {"time": "01:01", "median_ms": 40, "sample_count": 3},
                        {"time": "17:00", "median_ms": 80, "sample_count": 3},
                        {"time": "17:01", "median_ms": 50, "sample_count": 3},
                    ],
                },
                "sleep_hrv_daily_trend": {
                    "metric": "sleep_hrv",
                    "aggregation": "daily_median",
                    "device_label": "Amazfit Balance 2",
                    "today_value_ms": 73.0,
                    "today_sample_count": 1,
                    "points": [
                        {"date": "2026-08-22", "value_ms": 58.0, "sample_count": 1},
                        {"date": "2026-08-23", "value_ms": 57.0, "sample_count": 1},
                        {"date": "2026-08-24", "value_ms": 69.0, "sample_count": 1},
                        {"date": "2026-08-25", "value_ms": 66.0, "sample_count": 1},
                        {"date": "2026-08-26", "value_ms": 66.0, "sample_count": 1},
                        {"date": "2026-08-27", "value_ms": 70.0, "sample_count": 1},
                        {"date": "2026-08-28", "value_ms": 73.0, "sample_count": 1},
                    ],
                },
                "streams": [{
                    "device_label": "Amazfit Helio Strap",
                    "measurement_site": "upper_arm",
                    "value_ms": 71,
                    "sample_count_today": 432,
                    "baseline_distinct_days": 28,
                    "deviation": {"direction": "above", "percent": 8.2},
                    "selected": True,
                }, {
                    "device_label": "Amazfit Balance 2",
                    "measurement_site": "wrist",
                    "value_ms": 68,
                    "sample_count_today": 301,
                    "baseline_distinct_days": 28,
                    "deviation": {"direction": "above", "percent": 5.1},
                    "selected": False,
                }],
                "heart_rate_coverage": [{
                    "device_label": "Amazfit Helio Strap",
                    "today_coverage_minutes": 580.0,
                    "coverage_hours_28d": 164.5,
                    "covered_days_28d": 27,
                    "payload_decoded": False,
                }],
                "rhr_bpm": 47,
                "rhr_metric": "sleep_rhr",
                "rhr_source_scope": "user_fused",
                "rhr_deviation": {"direction": "near", "percent": -1.0},
            },
            "overnight_vitals": {
                "status": "AVAILABLE",
                "respiratory_rate": 13.6,
                "respiratory_rate_deviation": {"direction": "near", "percent": 1.2},
                "skin_temperature_delta_c": 0.05,
                "oxygen": {
                    "status": "AVAILABLE",
                    "measured_minutes": 443,
                    "odi_events_per_hour": 2.84,
                    "odi_baseline": 3.1,
                    "interpretation": "within_personal_range",
                },
                "outlier_labels": [],
            },
            "recovery": {
                "state_label": "恢复良好",
                "positive_signal_labels": ["HRV 高于个人基线"],
                "negative_signal_labels": [],
                "vendor_readiness": 78,
                "vendor_charge": 72,
            },
            "training": {
                "today_duration_minutes": 35,
                "today_load": 62,
                "duration_7d": 180,
                "load_7d": 260,
                "load_7d_reference": 240,
                "load_7d_change_percent": 8.3,
                "load_28d": 945,
                "aerobic_minutes_7d": 142,
                "strength_sessions_7d": 2,
                "load_state_label": "近期负荷正常",
                "running": {
                    "sessions_7d": 2,
                    "duration_minutes_7d": 82,
                    "distance_km_7d": 14.2,
                    "sessions_28d": 9,
                    "duration_minutes_28d": 375,
                    "distance_km_28d": 63.8,
                    "recent_sessions": [{
                        "date": "2026-08-28",
                        "classification": "TEMPO_RUN",
                        "classification_label": "节奏跑",
                        "confidence": "HIGH",
                        "duration_minutes": 35,
                        "distance_km": 6.4,
                        "average_pace_seconds_per_km": 328,
                        "median_cadence_spm": 174,
                        "average_heart_rate_bpm": 148,
                        "maximum_heart_rate_bpm": 171,
                        "cardiac_drift_percent": 3.2,
                        "heart_rate_zones": [
                            {"zone": 1, "share_percent": 10},
                            {"zone": 2, "share_percent": 20},
                            {"zone": 3, "share_percent": 30},
                            {"zone": 4, "share_percent": 35},
                            {"zone": 5, "share_percent": 5},
                        ],
                    }],
                },
                "strength": {
                    "sessions_7d": 2,
                    "sessions_28d": 8,
                    "explicit_session_coverage": 0.75,
                    "detected_split_label": "推、拉、腿三分化",
                    "next_focus_label": "拉类",
                    "recent_sessions": [{
                        "date": "2026-08-28",
                        "focus": "PUSH",
                        "focus_label": "推类",
                        "duration_minutes": 52,
                        "total_sets": 9,
                        "estimated_work_bouts": 9,
                        "median_rest_seconds": 105,
                        "session_rpe": 7,
                        "explicit_exercises": [
                            {"exercise_name": "卧推"},
                            {"exercise_name": "肩推"},
                        ],
                    }],
                },
                "recent_workouts": [{
                    "date": "2026-08-28",
                    "sport_mode_label": "户外跑",
                    "duration_minutes": 35,
                    "vendor_load": 62,
                    "heart_rate_avg_bpm": 148,
                    "recognition_confidence_label": "较高",
                }],
            },
        },
        "states": {
            "sleep": "ABOVE_BASELINE",
            "recovery": "GOOD",
            "training_load": "NORMAL",
            "sleep_label": "睡眠充足",
            "recovery_label": "恢复良好",
            "training_load_label": "近期负荷正常",
        },
        "trends": [{
            "metric_label": "训练负荷",
            "window_days": 7,
            "status": "AVAILABLE",
            "direction_label": "上升",
            "confidence_label": "中等",
        }],
        "events": [{
            "type": "TRAINING_LOAD_SPIKE",
            "type_label": "训练负荷上升",
            "summary": "近 7 日训练负荷持续上升。",
            "severity_label": "提醒",
            "lifecycle": "PERSISTING",
            "lifecycle_label": "持续中",
        }],
        "decision": {
            "action": "TRAIN_NORMAL",
            "action_label": "正常训练",
            "confidence": "MODERATE",
            "confidence_label": "中等",
            "drivers": ["RECOVERY_NORMAL"],
            "driver_labels": ["恢复状态一般"],
            "limitations": ["session_rpe_unavailable"],
            "limitation_labels": ["尚未记录主观用力程度"],
            "evidence": {
                "facts": [
                    {"code": "SLEEP_ABOVE_BASELINE", "label": "睡眠时长高于个人基线"},
                    {"code": "HRV_ABOVE_BASELINE", "label": "HRV 高于个人基线"},
                ],
                "gates": [],
            },
            "action_plan": {
                "goal_label": "综合健康优先，跑步与力量并行",
                "expires_at": "2026-08-29T00:00:00+08:00",
                "safety_status": "UNKNOWN",
                "safety_status_label": "未记录疼痛或伤病限制",
                "weekly_balance": {
                    "running_completed_7d": 2,
                    "running_target_7d": 3,
                    "running_completed_28d": 9,
                    "running_target_28d": 12,
                    "strength_completed_7d": 2,
                    "strength_target_7d": 3,
                    "strength_completed_28d": 8,
                    "strength_target_28d": 12,
                },
                "primary_session": {
                    "role_label": "主要训练",
                    "session_type": "RUNNING",
                    "title": "轻松跑",
                    "focus": "补足有氧频次并控制恢复成本",
                    "goal": "维持跑步连续性",
                    "intensity_label": "低强度",
                    "total_duration_minutes": [30, 45],
                    "steps": [{
                    "order": 1,
                    "name": "热身",
                    "duration_minutes": [6, 8],
                    "intensity": "轻松",
                    "instructions": ["先快走，再逐渐过渡到慢跑"],
                    }],
                    "evidence": ["近 7 天完成跑步 2 次。"],
                    "progression": ["恢复稳定后增加 5 分钟。"],
                    "stop_conditions": ["出现胸闷、头晕或疼痛时停止。"],
                },
                "optional_session": {
                    "role_label": "可选训练",
                    "session_type": "STRENGTH",
                    "title": "拉类力量训练",
                    "focus": "背部和肱二头肌",
                    "goal": "维持力量训练频次",
                    "intensity_label": "中等强度",
                    "total_duration_minutes": [40, 60],
                    "steps": [{
                        "order": 1,
                        "name": "水平拉",
                        "sets": 3,
                        "repetitions": "6–10 次",
                        "rest_seconds": [120, 120],
                        "intensity": "每组保留 2 次余力",
                        "instructions": ["选择无痛、可控制的动作"],
                    }],
                    "evidence": ["下一训练重点为拉类。"],
                    "progression": [],
                    "stop_conditions": ["动作失控或疼痛时停止。"],
                },
                "session_relationship_label": "可分开完成，至少间隔 6 小时",
                "session_relationship": "ADDITION",
                "conflict_checks": ["未发现高强度跑与腿部力量在 48 小时内冲突。"],
                "missing_input_gates": ["未设置单次可用时长。"],
            },
        },
    }


def test_morning_push_renders_actionable_coach_brief_without_audit_noise():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", _profile_payload(), period="morning")

    message = received[0]
    text = _visible_text(message.body)
    assert message.title == "Vitalis 晨报 · 2026-08-28 · 轻松跑"
    assert message.template == "html"
    assert message.body.startswith('<div style="max-width:680px')
    for heading in ("昨晚睡眠与身体状态", "今天做什么", "为什么", "注意", "训练后告诉我"):
        assert f">{heading}</h2>" in message.body
    for heading in ("今日状态", "昨夜数据", "最近 7 天", "今天安排", "开放洞察"):
        assert f">{heading}</h2>" not in message.body
    assert "睡眠时长高于个人基线" in text
    assert "HRV 高于个人基线" in text
    assert "主要：轻松跑 · 30–45 分钟 · 低强度" in text
    assert "热身：6–8 分钟 · 轻松。先快走，再逐渐过渡到慢跑" in text
    assert "有余力时，晚些时候可再做拉类力量训练；不做也不影响今天的主要安排。" in text
    assert "可选：拉类力量训练 · 40–60 分钟 · 中等强度" in text
    assert "近 7 日训练负荷持续上升。" in text
    assert "完成后记录：是否完成、主观用力 RPE" in text
    for raw_text in (
        "睡眠：7 小时 27 分钟",
        "快速眼动睡眠 106 分钟",
        "静息心率 47 次/分钟",
        "每小时下降 2.84 次",
        "构成：跑步 2 次、82 分钟、14.2 公里",
    ):
        assert raw_text not in text
    for audit_text in (
        "安全状态",
        "积极信号",
        "需关注信号",
        "多设备心率融合",
        "Amazfit Helio Strap",
        "厂商准备度",
        "身体电量",
        "趋势与事件",
        "进阶条件",
        "停止条件",
        "专项依据",
        "冲突检查",
        "规划边界",
        "数据限制",
        "至少间隔 6 小时",
    ):
        assert audit_text not in text
    for internal_code in ("TRAIN_NORMAL", "MODERATE", "RECOVERY_NORMAL", "session_rpe_unavailable"):
        assert internal_code not in message.title + text


def test_push_report_discloses_degraded_fresh_sync():
    received = []
    payload = _profile_payload()
    payload["delivery_metadata"] = {
        "sync_degraded": True,
        "sync_status": "transient_error",
        "sync_detail": "同步超时",
    }
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    text = _visible_text(received[0].body)
    assert ">注意</h2>" in received[0].body
    assert "本次同步未完整完成，结论使用的是已经保存的当天数据。" in text


def test_evening_push_names_exact_workout_mode_in_chinese():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", _profile_payload(), period="evening")

    message = received[0]
    text = _visible_text(message.body)
    assert message.title == "Vitalis 晚报 · 2026-08-28 · 户外跑"
    for heading in ("今日回顾", "训练表现", "全天状态", "今晚恢复", "明天衔接"):
        assert f">{heading}</h2>" in message.body
    assert "户外跑 · 35 分钟 · 设备训练负荷 62 · 平均心率 148 次/分钟" in text
    assert "平均配速 5:28/公里" in text
    assert "步频中位数 174 步/分钟" in text
    assert "心率漂移 +3.2%" in text
    assert "低强度 30.0% · 中等强度 30.0% · 阈值附近及以上 40.0%" in text
    assert "今天走了 8,632 步" in text
    assert "设备记录的平均压力 31；放松状态占 62%；高压力占 2%" in text
    assert "近 7 天睡眠 HRV" in text
    assert '<svg viewBox="0 0 600 170"' in message.body
    assert "&lt;svg" not in message.body
    assert "8/22" in text and "8/28" in text
    assert "Amazfit Balance 2 近 7 天有 7 晚记录，昨晚 73 毫秒" in text
    assert message.body.count('stroke="#2f9e44"') == 8
    assert "睡眠 HRV 时间线" not in text
    assert "01:00" not in text and "17:00" not in text
    assert "12 个样本" not in text
    assert "覆盖 10 分钟" not in text
    assert "▁" not in text
    curve_explanation_end = message.body.index("昨晚 73 毫秒")
    following_html = message.body[curve_explanation_end:]
    assert "<li style=" in following_html
    assert following_html.index("<li style=") < following_html.index("最近 7 天")
    assert following_html.index("最近 7 天") < following_html.index("近期负荷")
    assert "截至今天的 7 天" in text
    assert "这表示近期训练刺激增加，不代表恢复变差" in text
    assert "训练刺激减少" not in text
    assert "今晚不再追加跑步或腿部力量" in text
    assert "今天的训练记录提示明天避免连续安排质量跑和腿部力量" in text
    assert "趋势与事件" not in text
    assert "数据限制" not in text


def test_evening_rest_day_reports_activity_without_treating_rest_as_a_problem():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["training"]["recent_workouts"] = []
    payload["features"]["training"]["running"]["recent_sessions"] = []
    payload["features"]["training"]["strength"]["recent_sessions"] = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    message = received[0]
    assert message.title == "Vitalis 晚报 · 2026-08-28 · 日常活动回顾"
    assert "今天没有正式训练记录；这只是对当前记录的观察，不能据此推断休息状态" in message.body
    assert "今天走了 8,632 步" in message.body
    assert "今晚不据此判断恢复状态，也不安排补训练" in message.body
    assert "今天没有训练记录，不需要用明天加量补偿" in message.body
    assert "恢复受抑制" not in message.body


def test_evening_treats_small_weekly_load_change_as_stable():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["training"].update({
        "load_7d": 287,
        "load_7d_reference": 293,
        "load_7d_change_percent": -2.0,
    })
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    text = _visible_text(received[0].body)
    assert "截至今天的 7 天，设备训练负荷为 287" in text
    assert "与此前 3 周周均（293）基本相同（-2.0%）" in text
    assert "近期训练节奏总体稳定" in text
    assert "训练刺激减少" not in text


def test_evening_ignores_rmssd_timeline_and_uses_weekly_sleep_hrv():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["hrv"]["daily_curve"]["points"] = [
        {"time": "02:01", "median_ms": 60, "sample_count": 1},
        {"time": "09:22", "median_ms": 70, "sample_count": 1},
        {"time": "09:36", "median_ms": 55, "sample_count": 1},
        {"time": "09:39", "median_ms": 57, "sample_count": 1},
        {"time": "09:43", "median_ms": 58, "sample_count": 1},
        {"time": "15:19", "median_ms": 52, "sample_count": 1},
        {"time": "15:24", "median_ms": 54, "sample_count": 1},
        {"time": "15:29", "median_ms": 57, "sample_count": 1},
        {"time": "15:34", "median_ms": 59, "sample_count": 1},
        {"time": "15:39", "median_ms": 55, "sample_count": 1},
        {"time": "15:44", "median_ms": 58, "sample_count": 1},
        {"time": "15:49", "median_ms": 56, "sample_count": 1},
        {"time": "15:54", "median_ms": 60, "sample_count": 1},
        {"time": "15:59", "median_ms": 61, "sample_count": 1},
        {"time": "16:04", "median_ms": 59, "sample_count": 1},
        {"time": "16:06", "median_ms": 62, "sample_count": 1},
    ]
    payload["features"]["sleep"]["bedtime"] = "02:00:00"
    payload["features"]["sleep"]["wake_time"] = "09:23:00"
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    text = _visible_text(received[0].body)
    assert "近 7 天睡眠 HRV" in text
    assert "睡眠 HRV 时间线" not in text
    assert "02:01" not in text
    assert "15:19" not in text
    assert "16:06" not in text


def test_evening_low_confidence_run_does_not_name_workout_type_or_drift():
    received = []
    payload = deepcopy(_profile_payload())
    run = payload["features"]["training"]["running"]["recent_sessions"][0]
    run["confidence"] = "LOW"
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    body = received[0].body
    assert "课型暂不下结论" in body
    assert "节奏跑" not in body
    assert "心率漂移" not in body


def test_html_report_escapes_user_controlled_values():
    received = []
    payload = deepcopy(_profile_payload())
    exercises = payload["features"]["training"]["strength"]["recent_sessions"][0][
        "explicit_exercises"
    ]
    exercises[0]["exercise_name"] = '<img src="x" onerror="alert(1)">卧推'
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    body = received[0].body
    assert '&lt;img src="x" onerror="alert(1)"&gt;卧推' in body
    assert '<img src="x"' not in body


def test_missing_values_do_not_render_dangling_units():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["sleep"]["duration_minutes"] = None
    payload["features"]["hrv"]["value_ms"] = None
    payload["features"]["hrv"]["rhr_bpm"] = None
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    body = received[0].body
    for dangling in ("暂无 分钟", "暂无 毫秒", "暂无 次/分钟"):
        assert dangling not in body


def test_open_health_rendering_uses_localized_shadow_wording():
    payload = deepcopy(_profile_payload())
    payload["open_health_insights"] = {
        "readiness": {
            "status": "AVAILABLE",
            "drivers": ["lnRMSSD delta=-0.123456", "SWC=0.042"],
            "payload": {
                "state": "suppressed",
                "ln_rmssd": log(63),
                "baseline_ln_rmssd": log(65.5),
                "swc": 0.03,
                "prior_nights": 7,
            },
        },
        "anomaly": {
            "status": "AVAILABLE",
            "drivers": ["ln_rmssd:|z|=2.731"],
            "payload": {"flagged": True},
        },
        "sleep": {"status": "PARTIAL", "payload": {}},
        "training_load": {
            "status": "PARTIAL",
            "payload": {
                "lower_bound": True,
                "atl": 20.0,
                "ctl": 15.0,
                "tsb": -5.0,
                "daily_points": [{
                    "date": "2026-08-28", "status": "SCORED", "trimp": 42.5,
                }],
            },
        },
    }
    morning = []
    service = PushService(pushplus_token="")
    service.add_handler(morning.append)
    service.push_daily_profile("test-user", payload, period="morning")
    morning_text = _visible_text(morning[0].body)
    for raw_text in (
        "实验性 RMSSD 观察",
        "Zepp 夜间汇总 63.0 毫秒",
        "此前 7 夜基线约 65.5 毫秒",
        "动态参考范围",
        "多指标观察",
        "lnRMSSD delta",
        "ln_rmssd",
    ):
        assert raw_text not in morning_text

    evening = []
    service = PushService(pushplus_token="")
    service.add_handler(evening.append)
    service.push_daily_profile("test-user", payload, period="evening")
    evening_text = _visible_text(evening[0].body)
    assert "当日 TRIMP：42.5" in evening_text
    assert "下界估计，上游同步覆盖尚未完全验证" in evening_text
    assert "恢复良好" not in evening_text


def test_morning_push_mentions_device_disagreement_only_when_consequential():
    received = []
    payload = _profile_payload()
    payload["features"]["hrv"]["corroboration_status"] = "conflicting"
    payload["features"]["hrv"]["corroboration_affects_decision"] = True
    payload["events"] = [{
        "type": "HRV_RECOVERY",
        "lifecycle": "PERSISTING",
    }]
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    assert "今天的心率变异性证据不够稳定" in received[0].body
    assert "心率变异性持续回到个人正常范围" not in received[0].body
    assert "Amazfit Helio Strap" not in received[0].body


def test_morning_push_omits_stale_hrv_recovery_event_when_hrv_is_above_baseline():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["hrv"]["deviation"] = {"direction": "above"}
    payload["features"]["hrv"]["recent_7d_direction"] = "above"
    payload["events"] = [{
        "type": "HRV_RECOVERY",
        "lifecycle": "PERSISTING",
    }]
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    assert "心率变异性持续回到个人正常范围" not in received[0].body


def test_morning_push_explains_nocturnal_pattern_and_personalized_session():
    received = []
    payload = _profile_payload()
    payload["features"]["hrv"].update({
        "recent_7d_direction": "above",
        "recent_7d_change_percent": 12.5,
        "rhr_metric": "nocturnal_heart_rate",
        "nocturnal_heart_rate": {
            "status": "AVAILABLE",
            "device_label": "Amazfit Helio Strap",
            "median_bpm": 50,
            "low_5m_bpm": 46,
            "deviation_percent": 2.0,
            "direction": "near",
            "second_minus_first_bpm": -1,
        },
    })
    payload["decision"]["action_plan"]["primary_session"]["personalization_reasons"] = [
        "最近一次跑步为稳定跑，33 分钟，平均配速 6:39/公里，自然步频约 185 步/分钟。",
        "最近一次可解释的心率漂移为 +8.1%。",
    ]
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    body = received[0].body
    text = _visible_text(body)
    assert "最近一次跑步为稳定跑，33 分钟" in text
    for raw_text in (
        "近 7 天较此前 7 天 +12.5%",
        "分设备整夜心率：中位数 50 次/分钟",
        "稳定 5 分钟低点 46 次/分钟",
        "后半夜较前半夜回落 1 次/分钟",
        "Amazfit Helio Strap",
    ):
        assert raw_text not in text
    reasons = body.split(">为什么</h2>", 1)[1].split(">注意</h2>", 1)[0]
    assert reasons.count("<li") <= 3


def test_morning_push_prefers_vendor_sleep_rhr_and_keeps_device_detail_hidden():
    received = []
    payload = _profile_payload()
    payload["features"]["hrv"].update({
        "fusion_method": "vendor_fused_with_device_audit",
        "value_ms": 65,
        "rhr_bpm": 48,
        "rhr_metric": "sleep_rhr",
        "rhr_source_scope": "user_fused",
        "nocturnal_heart_rate": {
            "status": "AVAILABLE",
            "device_label": "未识别设备 C",
            "median_bpm": 51,
            "low_5m_bpm": 47,
            "second_minus_first_bpm": -4,
        },
    })
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    text = _visible_text(received[0].body)
    for raw_text in (
        "Zepp 睡眠心率变异性 65 毫秒",
        "Zepp 睡眠静息心率 48 次/分钟",
        "分设备整夜心率",
        "51 次/分钟",
    ):
        assert raw_text not in text


def test_morning_push_does_not_invent_training_when_data_is_insufficient():
    received = []
    payload = deepcopy(_profile_payload())
    payload["data_quality"] = {
        "status": "INSUFFICIENT",
        "status_label": "数据不足",
        "missing_required_signal_labels": ["睡眠时长", "心率变异性"],
    }
    payload["decision"].update({
        "action": "INSUFFICIENT_DATA",
        "action_label": "数据不足，暂不建议",
        "limitation_labels": ["恢复决策所需信号不足"],
    })
    payload["decision"]["action_plan"].update({
        "primary_session": None,
        "optional_session": None,
        "session_relationship": "NONE",
    })
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    text = _visible_text(received[0].body)
    assert "今天不生成训练建议。" in text
    assert "恢复决策所需信号不足" in text
    assert "睡眠时长" in text and "心率变异性" in text
    assert "训练后告诉我" not in text
    assert "先按正常生活节奏活动" not in text


def test_pushplus_delivery_keeps_token_in_json_body(monkeypatch):
    requests = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10.0, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr("vitalis.services.push_service.httpx.Client", Client)
    service = PushService(pushplus_token="private-token")
    result = service.push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))

    assert result["_pushplus_handler"] == "ok"
    assert requests == [(
        PUSHPLUS_URL,
        {"json": {
            "token": "private-token",
            "title": "晨间日报",
            "content": "数据完整",
                "template": "html",
        }},
    )]
    assert "private-token" not in requests[0][0]


def test_pushplus_application_error_is_failed_delivery(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 500, "msg": "sensitive upstream response"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            return Response()

    monkeypatch.setattr("vitalis.services.push_service.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(
        PushMessage(title="晨间日报", body="数据完整", user_id="user")
    )

    assert result["_pushplus_handler"] == "error: PushPlus rejected delivery with code 500"
    assert "private-token" not in result["_pushplus_handler"]
    assert "sensitive upstream response" not in result["_pushplus_handler"]


def test_morning_push_uses_gate_evidence_for_insufficient_data_reason():
    received = []
    payload = deepcopy(_profile_payload())
    payload["decision"].update({
        "action": "INSUFFICIENT_DATA",
        "action_label": "数据不足，暂不建议",
        "evidence": {
            "facts": [],
            "gates": [{
                "code": "DECISION.TRAINING_HISTORY_COVERAGE_INSUFFICIENT",
                "label": "此前训练记录不足，暂不处方",
                "triggered": True,
            }],
        },
    })
    payload["decision"]["action_plan"].update({
        "primary_session": None,
        "optional_session": None,
        "session_relationship": "NONE",
    })
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    text = _visible_text(received[0].body)
    assert "此前训练记录不足，暂不处方" in text
    assert "恢复决策所需信号不足，今天不生成训练建议" not in text


def test_morning_push_leads_with_numeric_overnight_observations_and_reports_baseline_gap():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["sleep"]["duration_deviation"] = None
    payload["features"]["hrv"]["deviation"] = None
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    text = _visible_text(received[0].body)
    assert "昨晚睡眠与身体状态" in text
    assert "昨晚睡眠 447 分钟；个人基线不足，暂不比较" in text
    assert "设备 心率变异性 71 毫秒；个人基线不足，暂不比较" in text
    assert "综合身体状态：恢复良好。" in text
    assert received[0].extras["report_context"]["device_recovery_readiness"] == 78
    assert text.index("昨晚睡眠与身体状态") < text.index("今天做什么")


def test_morning_push_renders_all_hard_safety_stop_conditions_separately():
    received = []
    payload = deepcopy(_profile_payload())
    plan = payload["decision"]["action_plan"]
    plan["safety_status"] = "LIMITED"
    plan["safety_status_label"] = "存在疼痛或伤病限制"
    plan["primary_session"]["stop_conditions"] = [
        "疼痛时停止。", "症状加重时寻求评估。"
    ]
    plan["optional_session"]["stop_conditions"] = ["动作失控时停止。"]
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="morning")

    body = received[0].body
    assert ">安全限制</h2>" in body
    assert "疼痛时停止。" in body
    assert "症状加重时寻求评估。" in body
    assert "动作失控时停止。" in body


def test_evening_push_reports_confirmed_strength_exercise_load_details():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["training"]["strength"]["recent_sessions"][0][
        "explicit_exercises"
    ] = [{
        "exercise_name": "卧推",
        "sets": 4,
        "repetitions": "6–8 次",
        "weight_kg": 60,
        "rpe": 8,
        "rir": 2,
        "rest_seconds": 120,
    }]
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    text = _visible_text(received[0].body)
    assert "卧推：4 组 · 6–8 次 · 60 千克 · RPE 8 · 余力 2 · 休息 120 秒" in text


def test_evening_rest_observation_does_not_claim_rest_or_predict_tomorrow_recovery():
    received = []
    payload = deepcopy(_profile_payload())
    payload["features"]["training"]["recent_workouts"] = []
    payload["features"]["training"]["running"]["recent_sessions"] = []
    payload["features"]["training"]["strength"]["recent_sessions"] = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", payload, period="evening")

    text = _visible_text(received[0].body)
    assert "今天没有正式训练记录；这只是对当前记录的观察，不能据此推断休息状态" in text
    assert "下一次力量训练的候选重点是拉类" in text
    assert "明天按计划和身体感受安排" not in text
    assert "明早根据整夜睡眠" not in text


def test_weekly_push_renders_asof_coverage_facts_trends_and_recommendations():
    received = []
    profile = {
        "period_start": "2026-08-22",
        "period_end": "2026-08-28",
        "report_context": {
            "as_of": "2026-08-28T23:30:00+00:00",
            "training_coverage": {
                "coverage_status": "PARTIAL",
                "record_days": 3,
                "unknown_days": 4,
            },
        },
        "data_quality": {"status_label": "部分可用"},
        "facts": {
            "sleep": {"available_days": 5, "average_minutes": 420},
            "training": {
                "record_days": 3,
                "unknown_days": 4,
                "coverage_status": "PARTIAL",
                "totals_are_partial": True,
                "workout_count": 1,
                "training_days": 1,
                "duration_minutes": 30,
                "vendor_load": 20,
                "rest_days": 2,
                "aerobic_minutes": 30,
                "strength_sessions": None,
            },
            "activity": {"available_days": 4, "total_steps": 30000},
        },
        "inferences": {
            "key_changes": ["睡眠时长较前一周上升 5.0%。"],
            "trends": [],
            "limitations": ["训练历史覆盖为 PARTIAL。"],
        },
        "actions": {"recommendations": [{
            "title": "先补齐周期记录",
            "action": "先完成后续同步。",
            "reasons": ["仍有 4 天未知。"],
        }]},
    }
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_weekly_profile("test-user", profile)

    text = _visible_text(received[0].body)
    assert "滚动周期：2026-08-22 至 2026-08-28" in text
    assert "分析截至：2026-08-28 23:30 +0000" in text
    assert "训练历史：清单部分已核实；已核实 3 天；未知 4 天" in text
    assert "睡眠时长较前一周上升 5.0%。" in text
    assert "先补齐周期记录" in text


def test_pushplus_transport_error_is_failed_delivery_without_token(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            raise OSError("network unavailable")

    monkeypatch.setattr("vitalis.services.push_service.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(
        PushMessage(title="晨间日报", body="数据完整", user_id="user")
    )

    assert result["_pushplus_handler"] == "error: network unavailable"
    assert "private-token" not in result["_pushplus_handler"]


def test_evening_explains_when_app_strength_corrections_are_not_in_cloud_data():
    payload = _profile_payload()
    for session in payload["features"]["training"]["strength"]["recent_sessions"]:
        session["explicit_exercises"] = []
        session["hypotheses"] = []
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_daily_profile("test-user", payload, period="evening")
    assert "不能据此复原 App 中的修正项目" in _visible_text(received[0].body)
