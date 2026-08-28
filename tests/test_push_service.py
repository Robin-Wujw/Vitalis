from vitalis.services.push_service import PUSHPLUS_URL, PushMessage, PushService


def _profile_payload():
    return {
        "date": "2026-08-28",
        "features": {
            "sleep": {"duration_minutes": 447},
            "hrv": {"value_ms": 71, "rhr_bpm": 47},
            "recovery": {"state_label": "恢复良好"},
            "training": {
                "today_duration_minutes": 35,
                "today_load": 62,
                "load_7d": 260,
                "load_state_label": "近期负荷正常",
                "recent_workouts": [{
                    "date": "2026-08-28",
                    "sport_mode_label": "户外跑",
                    "duration_minutes": 35,
                    "recognition_confidence_label": "较高",
                }],
            },
        },
        "decision": {
            "action": "TRAIN_NORMAL",
            "action_label": "正常训练",
            "confidence": "MODERATE",
            "confidence_label": "中等",
            "intensity": "moderate",
            "intensity_label": "中等强度",
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

    service.push_daily_profile("001", _profile_payload(), period="morning")

    message = received[0]
    assert message.title == "Vitalis 晨间建议 · 正常训练"
    assert "建议置信度：中等" in message.body
    assert "训练方案：二区有氧跑" in message.body
    assert "1. 热身：8–10 分钟，轻松" in message.body
    for internal_code in ("TRAIN_NORMAL", "MODERATE", "RECOVERY_NORMAL", "session_rpe_unavailable"):
        assert internal_code not in message.title + message.body


def test_evening_push_names_exact_workout_mode_in_chinese():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)

    service.push_daily_profile("001", _profile_payload(), period="evening")

    assert "训练记录：户外跑，35 分钟，类型识别置信度较高" in received[0].body


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
