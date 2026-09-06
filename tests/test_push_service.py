from copy import deepcopy
from html.parser import HTMLParser

import pytest

from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
from vitalis.services.push_service import PUSHPLUS_URL, PushMessage, PushService, _render_evening
from tests.test_report_content import synthetic_daily_fixture, synthetic_period_fixture


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


def _daily_payload():
    payload = synthetic_daily_fixture("complete")
    payload["features"]["training"]["strength"]["recent_sessions"] = [{
        "date": payload["date"],
        "focus": "PUSH",
        "focus_label": "推类",
        "duration_minutes": 52,
        "total_sets": 9,
        "estimated_work_bouts": 9,
        "explicit_exercises": [{"exercise_name": "卧推", "sets": 4, "repetitions": "6–8 次", "weight_kg": 60, "rpe": 8, "rir": 2, "rest_seconds": 120}],
    }]
    payload["features"]["training"]["running"]["recent_sessions"][0]["confidence"] = "HIGH"
    plan = payload["decision"]["action_plan"]
    plan.update({
        "primary_session": {
            "title": "轻松跑", "focus": "补足有氧频次并控制恢复成本", "intensity_label": "低强度",
            "total_duration_minutes": [30, 45], "steps": [{"order": 1, "name": "热身", "duration_minutes": [6, 8], "intensity": "轻松", "instructions": ["先快走，再逐渐过渡到慢跑"]}],
            "personalization_reasons": ["近 7 天完成跑步 2 次，今天优先维持跑步频次。"],
            "stop_conditions": ["出现疼痛时停止。"],
        },
        "optional_session": {"title": "拉类力量训练", "focus": "背部和肱二头肌", "intensity_label": "中等强度", "total_duration_minutes": [40, 60], "steps": [], "stop_conditions": ["动作失控时停止。"]},
        "session_relationship": "ADDITION",
        "safety_status": "CLEAR",
    })
    return payload


def _sent(service_call):
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service_call(service)
    assert received
    return received[0]


def test_morning_push_uses_complete_sections_and_no_automatic_questionnaire():
    message = _sent(lambda service: service.push_daily_profile("test-user", _daily_payload(), period="morning"))
    text = _visible_text(message.body)
    assert message.title.startswith("Vitalis 晨报 · 2026-09-05")
    assert message.body.startswith('<div style="max-width:680px')
    for heading in ("昨晚睡眠", "今早恢复信号", "今天的安排"):
        assert f">{heading}</h2>" in message.body
    assert "睡眠时长" in text and "入睡" in text and "夜间醒来" in text
    assert "睡眠 HRV" in text and "静息心率" in text
    assert "主要安排：轻松跑" in text and "可选加做：拉类力量训练" in text
    assert "至少间隔 6 小时" in text
    assert "训练后告诉我" not in text
    assert "feedback_prompt" not in text
    assert "Zepp 厂商汇总" not in text
    assert "vendor_readiness" not in text


def test_morning_displays_sync_limitation_and_recovery_disagreement():
    payload = _daily_payload()
    payload["delivery_metadata"] = {"sync_degraded": True}
    payload["features"]["hrv"].update({"corroboration_status": "conflicting", "corroboration_affects_decision": True})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "本次同步未完整完成" in text
    assert "HRV 证据存在分歧" in text
    assert "单个 HRV 读数" in text
    assert "Amazfit" not in text


def test_morning_insufficient_data_does_not_invent_training():
    payload = _daily_payload()
    payload["data_quality"] = {"status": "INSUFFICIENT", "status_label": "数据不足", "missing_required_signal_labels": ["睡眠时长", "心率变异性"]}
    payload["features"]["sleep"].update({"duration_minutes": None, "bedtime": None, "wake_time": None})
    payload["features"]["hrv"].update({"value_ms": None, "rhr_bpm": None})
    payload["decision"].update({"action": "INSUFFICIENT_DATA", "action_label": "数据不足，暂不建议", "limitation_labels": ["恢复决策所需信号不足"]})
    payload["decision"]["action_plan"].update({"primary_session": None, "optional_session": None})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "今天不生成训练建议" in text
    assert "恢复决策所需信号不足" in text
    assert "训练后告诉我" not in text


def test_morning_renders_all_hard_safety_conditions():
    payload = _daily_payload()
    plan = payload["decision"]["action_plan"]
    plan["safety_status"] = "LIMITED"
    plan["safety_status_label"] = "存在疼痛或伤病限制"
    plan["primary_session"]["stop_conditions"] = ["疼痛时停止。", "症状加重时寻求评估。"]
    plan["optional_session"]["stop_conditions"] = ["动作失控时停止。"]
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "安全限制" in text
    assert "疼痛时停止" in text and "症状加重时寻求评估" in text and "动作失控时停止" in text


def test_evening_push_uses_complete_sections_and_actual_training_facts():
    message = _sent(lambda service: service.push_daily_profile("test-user", _daily_payload(), period="evening"))
    text = _visible_text(message.body)
    assert message.title.startswith("Vitalis 晚报 · 2026-09-05")
    for heading in ("逐场训练", "日常活动与能量", "日内心率与压力覆盖", "恢复背景与当天训练"):
        assert f">{heading}</h2>" in message.body
    assert "户外跑" in text and "45 分钟" in text and "7.10 公里" in text
    assert "节奏跑" in text
    assert "卧推" in text and "4 组" in text and "60 千克" in text
    assert "步数" in text and "活动距离" in text and "活动时长" in text
    assert "设备估算热量（统计范围待确认）" in text and "本次训练估算热量" in text
    assert "训练后告诉我" not in text


def test_evening_low_confidence_run_does_not_name_classification():
    payload = _daily_payload()
    run = payload["features"]["training"]["running"]["recent_sessions"][0]
    run["confidence"] = "LOW"
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    assert "跑步课型暂不确定" in text
    assert "节奏跑" not in text


def test_evening_missing_training_does_not_call_it_rest_or_add_compensation():
    payload = _daily_payload()
    payload["features"]["training"]["recent_workouts"] = []
    payload["features"]["training"]["running"]["recent_sessions"] = []
    payload["features"]["training"]["strength"]["recent_sessions"] = []
    payload["features"]["training"]["today_duration_minutes"] = None
    payload["features"]["training"]["today_load"] = None
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    assert "没有已记录的正式训练场次" in text
    assert "不等同于已确认休息日" in text
    assert "补训练" not in text


def test_evening_strength_detail_gap_is_explicit_and_work_bouts_are_not_sets():
    payload = _daily_payload()
    payload["features"]["training"]["strength"]["recent_sessions"][0]["explicit_exercises"] = []
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    assert "逐组动作、重复次数和重量" in text
    assert "工作段不能替代明确组数" in text


def test_html_report_escapes_user_controlled_values():
    payload = _daily_payload()
    payload["features"]["training"]["strength"]["recent_sessions"][0]["explicit_exercises"][0]["exercise_name"] = '<img src="x" onerror="alert(1)">卧推'
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    assert '&lt;img src="x" onerror="alert(1)"&gt;卧推' in message.body
    assert '<img src="x"' not in message.body


def test_missing_values_do_not_render_dangling_units():
    payload = _daily_payload()
    payload["features"]["sleep"].update({"duration_minutes": None, "bedtime": None, "wake_time": None})
    payload["features"]["hrv"].update({"value_ms": None, "rhr_bpm": None})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    for dangling in ("暂无 分钟", "暂无 毫秒", "暂无 次/分钟"):
        assert dangling not in text


def test_evening_separates_energy_roles_without_double_counting():
    payload = _daily_payload()
    payload["features"]["activity"]["energy"].append({"metric": "calories", "value": 120, "unit": "kcal", "observed_at": payload["date"], "provenance": {"source": "zepp"}, "role": "unspecified", "estimated": True, "source_field": "calories"})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    assert "设备估算热量（统计范围待确认）" in text and "本次训练估算热量" in text
    assert "统计范围待确认" in text
    assert "daily.calories" not in text and "activity.calories" not in text and "workout.calories" not in text
    assert "user_fused" not in text and "source_scope" not in text
    assert "热量赤字" not in text


@pytest.mark.parametrize("role, label", [
    ("unspecified", "设备估算热量（统计范围待确认）"),
    ("daily_total", "全天总消耗（设备估算）"),
])
def test_weekly_push_renders_sections_coverage_changes_and_recommendations(role, label):
    profile = synthetic_period_fixture("weekly", "heterogeneous")
    profile["facts"]["activity"]["metrics"] = [{
        "metric": "calories", "unit": "kcal", "role": role, "source_field": "daily.calories",
        "period_days": 7, "available_days": 6, "complete_days": 5, "previous_available_days": 6,
        "previous_complete_days": 5, "total": 2400, "average": 400, "previous_total": 2200,
        "previous_average": 366.7, "change_percent": 9.1, "totals_are_partial": True,
    }]
    profile["report_context"]["as_of"] = "2026-09-05T23:30:00+00:00"
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_weekly_profile("test-user", profile)
    message = received[0]
    text = _visible_text(message.body)
    assert message.extras["period"] == "weekly"
    for heading in ("两期七日覆盖", "睡眠与恢复变化", "跑步与力量结构", "活动、能量与反馈", "既有门控建议"):
        assert heading in text
    assert "轻松跑" in text and "节奏或阈值跑" in text
    assert "EASY_RUN" not in text and "TEMPO_RUN" not in text
    assert "睡眠时长较前一期增加" not in text
    assert "前后窗口来源不同" in text
    assert "保持当前结构" in text
    assert label in text
    assert "daily.calories" not in text
    assert "UNKNOWN" not in text
    assert "user_fused" not in text and "source_scope" not in text


def test_monthly_push_uses_same_report_sections_and_noncausal_association():
    profile = synthetic_period_fixture("monthly", "complete")
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_monthly_profile("test-user", profile)
    message = received[0]
    text = _visible_text(message.body)
    assert message.extras["period"] == "monthly"
    for heading in ("两期二十八日覆盖", "持续恢复变化", "训练结构、活动与能量", "合格的个人关联", "阶段建议"):
        assert heading in text
    assert "不表示因果" in text


def test_four_complete_fixture_entrypoints_build_the_same_sections():
    daily = _daily_payload()
    morning = MorningBriefingEngine().build_payload(daily)
    evening = EveningBriefingEngine().build(daily)
    weekly = WeeklyBriefingEngine().build(synthetic_period_fixture("weekly"))
    monthly = MonthlyBriefingEngine().build(synthetic_period_fixture("monthly"))
    assert [section["key"] for section in morning["sections"]] == ["sleep", "recovery", "today_plan"]
    assert [section.key for section in evening.sections] == ["training", "activity", "intraday", "recovery"]
    assert weekly.sections and monthly.sections


def test_retrospective_evening_has_no_current_or_future_prescription():
    payload = _daily_payload()
    payload["delivery_metadata"] = {"retrospective": True}
    title, lines = _render_evening(payload)
    text = "\n".join(lines)
    assert "晚报补发" in title
    assert "仅回顾指定日期范围" in text
    assert "## 今晚恢复" not in text and "## 明天衔接" not in text
    assert "主要安排" not in text


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
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "ok"
    assert requests[0][0] == PUSHPLUS_URL
    assert requests[0][1]["json"]["token"] == "private-token"


def test_pushplus_application_error_is_failed_delivery_without_sensitive_response(monkeypatch):
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
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "error: PushPlus rejected delivery with code 500"
    assert "sensitive upstream response" not in result["_pushplus_handler"]
    assert "private-token" not in result["_pushplus_handler"]


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
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "error: network unavailable"
    assert "private-token" not in result["_pushplus_handler"]
