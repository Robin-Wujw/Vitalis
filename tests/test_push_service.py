from copy import deepcopy

from vitalis.services.push_service import PUSHPLUS_URL, PushMessage, PushService


def _profile_payload():
    return {
        "date": "2026-08-28",
        "data_quality": {
            "status": "SUFFICIENT",
            "status_label": "数据完整",
            "missing_required_signal_labels": [],
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
                        "classification_label": "节奏跑",
                        "duration_minutes": 35,
                        "distance_km": 6.4,
                        "average_pace_seconds_per_km": 328,
                        "median_cadence_spm": 174,
                        "cardiac_drift_percent": 3.2,
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
    assert message.title == "Vitalis 晨报 · 2026-08-28 · 轻松跑"
    assert message.body.startswith("> **数据日期：2026-08-28** · 数据完整")
    assert "\n## 今日结论\n" in message.body
    assert "恢复良好，今天做轻松跑，按低强度执行。" in message.body
    assert "\n## 今天做什么\n" in message.body
    assert "### 主要：轻松跑 · 30–45 分钟 · 低强度" in message.body
    assert "1. **热身**：6–8 分钟 · 轻松。先快走，再逐渐过渡到慢跑" in message.body
    assert "有余力时，晚些时候可再做拉类力量训练；不做也不影响今天的主要安排。" in message.body
    assert "### 可选：拉类力量训练 · 40–60 分钟 · 中等强度" in message.body
    assert "昨晚睡了 7 小时 27 分钟" in message.body
    assert "夜间心率变异性高于你的个人基线" in message.body
    assert "近 7 天跑步 2/3 次、力量 2/3 次" in message.body
    assert "最近一周训练负荷明显增加。完成今天的计划即可，不再追加训练。" in message.body
    for audit_text in (
        "安全状态",
        "积极信号",
        "需关注信号",
        "多设备心率融合",
        "Amazfit Helio Strap",
        "厂商准备度",
        "身体电量",
        "睡眠结构",
        "趋势与事件",
        "进阶条件",
        "停止条件",
        "专项依据",
        "冲突检查",
        "规划边界",
        "数据限制",
        "至少间隔 6 小时",
    ):
        assert audit_text not in message.body
    for internal_code in ("TRAIN_NORMAL", "MODERATE", "RECOVERY_NORMAL", "session_rpe_unavailable"):
        assert internal_code not in message.title + message.body


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

    assert "## 必要提醒" in received[0].body
    assert "本次同步未完整完成，结论使用的是已经保存的当天数据。" in received[0].body


def test_evening_push_names_exact_workout_mode_in_chinese():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", _profile_payload(), period="evening")

    message = received[0]
    assert message.title == "Vitalis 晚报 · 2026-08-28 · 户外跑"
    assert "## 今天完成了什么" in message.body
    assert (
        "- **户外跑** · 35 分钟 · 厂商负荷 62 · 平均心率 148 次/分钟"
    ) in message.body
    assert "## 训练质量" in message.body
    assert "平均配速 5:28/公里" in message.body
    assert "步频中位数 174 步/分钟" in message.body
    assert "心率漂移 +3.2%" in message.body
    assert "## 今晚怎么收尾" in message.body
    assert "今晚不再补训练" in message.body
    assert "主观用力（1–10 分）和主要酸痛部位" in message.body
    assert "趋势与事件" not in message.body
    assert "数据限制" not in message.body


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
    assert "近 7 天中位数较此前 7 天上升 12.5%" in body
    assert "睡眠中心率中位数 50 次/分钟" in body
    assert "稳定 5 分钟低点 46 次/分钟" in body
    assert "后半夜较前半夜回落 1 次/分钟" in body
    assert "接近近 28 晚个人水平（+2.0%）" in body
    assert "最近一次跑步为稳定跑，33 分钟" in body
    assert "Amazfit Helio Strap" not in body
    reasons = body.split("## 为什么", 1)[1].split("## 最近最值得注意", 1)[0]
    assert reasons.count("\n-") <= 4


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
            "template": "markdown",
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
