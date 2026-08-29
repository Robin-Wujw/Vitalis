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
            "intensity": "moderate",
            "intensity_label": "中等强度",
            "suggested_type_labels": ["有氧训练"],
            "duration_minutes": [45, 60],
            "drivers": ["RECOVERY_NORMAL"],
            "driver_labels": ["恢复状态一般"],
            "limitations": ["session_rpe_unavailable"],
            "limitation_labels": ["尚未记录主观用力程度"],
            "prescription_guidance": "按以下结构完成本次训练。",
            "prescriptions": [{
                "title": "二区有氧跑",
                "goal": "补充有氧基础训练",
                "total_duration_minutes": [45, 60],
                "steps": [{
                    "order": 1,
                    "name": "热身",
                    "duration_minutes": [8, 10],
                    "intensity": "轻松",
                    "instructions": ["先快走，再逐渐过渡到慢跑"],
                }],
                "progression": [],
                "cautions": ["出现胸闷或头晕时停止训练"],
            }],
        },
    }


def test_morning_push_uses_chinese_labels_and_renders_prescription():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", _profile_payload(), period="morning")

    message = received[0]
    assert message.title == "Vitalis 晨报 · 2026-08-28 · 正常训练"
    assert message.body.startswith("> **数据日期：2026-08-28** · 数据完整")
    assert "\n## 晨间结论\n" in message.body
    assert "- **恢复判断**：恢复良好" in message.body
    assert "- **积极信号**：HRV 高于个人基线" in message.body
    assert "个人基线：高于 28 天基线（+6.5%）" in message.body
    assert "HRV 个人基线：高于 28 天基线（+8.2%）" in message.body
    assert "RHR 个人基线：接近 28 天基线（-1%）" in message.body
    assert "## 多设备心率融合" in message.body
    assert "Amazfit Helio Strap（上臂）" in message.body
    assert "2 台设备相对各自 28 天基线方向一致" in message.body
    assert "近 28 日 164.5 小时/27 天 · 仅覆盖索引，数值尚未解码" in message.body
    assert "## 睡眠详情" in message.body
    assert "深睡 92 分钟 · 快速眼动睡眠 106 分钟 · 清醒 24 分钟" in message.body
    assert "## 近期负荷与背景" in message.body
    assert "有氧 142 分钟 · 力量 2 次" in message.body
    assert "\n## 训练安排\n" in message.body
    assert "- **结论把握**：中等" in message.body
    assert "\n## 判断依据\n" in message.body
    assert "\n## 数据限制\n" in message.body
    assert "\n## 具体方案\n" in message.body
    assert "### 二区有氧跑 · 45–60 分钟" in message.body
    assert "1. **热身** · 8–10 分钟 · 轻松" in message.body
    assert message.body.rfind("## 数据限制") > message.body.rfind("## 判断依据")
    assert message.body.rstrip().endswith("- 尚未记录主观用力程度")
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

    assert (
        "> **同步提醒**：本次新同步未完整完成，报告使用已存储且通过完整性校验的当天数据。"
        in received[0].body
    )


def test_evening_push_names_exact_workout_mode_in_chinese():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("test-user", _profile_payload(), period="evening")

    message = received[0]
    assert message.title == "Vitalis 晚报 · 2026-08-28 · 正常训练"
    assert "## 晚间结论" in message.body
    assert "## 今日训练" in message.body
    assert (
        "- **户外跑** · 35 分钟 · 厂商负荷 62 · 平均心率 148 次/分钟 · "
        "识别置信度较高"
    ) in message.body
    assert "- **近 7 日训练**：180 分钟 · 负荷 260" in message.body
    assert "- **积极信号**：HRV 高于个人基线" in message.body
    assert "- **训练负荷（7 日）**：上升 · 中等置信度" in message.body
    assert "- **训练负荷上升**：近 7 日训练负荷持续上升。（提醒，持续中）" in message.body
    assert message.body.rfind("## 数据限制") > message.body.rfind("## 趋势与事件")
    assert message.body.rstrip().endswith("- 尚未记录主观用力程度")


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
    assert "- **睡眠**：暂无" in body
    assert "- **心率变异性（HRV）**：暂无" in body
    assert "静息心率（RHR）" not in body
    for dangling in ("暂无 分钟", "暂无 毫秒", "暂无 次/分钟"):
        assert dangling not in body


def test_pushplus_delivery_keeps_token_in_json_body(monkeypatch):
    requests = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功"}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10.0}

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
