from copy import deepcopy
from html.parser import HTMLParser
import logging

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


def test_morning_digest_prioritizes_measured_facts_and_keeps_detail_structured():
    daily = _daily_payload()
    daily["features"]["sleep"].update({"vendor_sleep_score": 88, "deep_minutes": 95})
    daily["features"]["recovery"].update({"vendor_readiness": 83, "vendor_charge": 71})
    daily["features"]["hrv"].update({
        "preferred_device_label": "Zepp 厂商汇总",
        "recent_7d_median_ms": 66, "recent_7d_days": 5,
    })
    daily["features"]["activity"]["steps"].update({
        "observed_at": daily["date"], "unit": "steps",
    })
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    text = _visible_text(message.body)
    sleep = next(section for section in message.extras["sections"] if section["key"] == "sleep")
    recovery = next(section for section in message.extras["sections"] if section["key"] == "recovery")

    assert "睡眠时长" in text and "入睡" in text and "醒来" in text
    assert "步数 8,200 步" in text
    assert "Zepp 汇总" not in text
    assert "设备睡眠评分" not in text and "设备准备度评分" not in text
    assert "设备睡眠评分 88/100" in sleep["facts"]
    assert "设备准备度评分 83/100（厂商参考值）" in recovery["facts"]
    assert any("设备记录深睡" in item for item in sleep["facts"])
    assert any("近 7 日同源" in item for item in recovery["facts"])


def test_morning_separates_same_role_energy_only_when_sources_differ():
    daily = _daily_payload()
    daily["features"]["activity"]["energy"].append({
        "value": 260, "unit": "kcal", "role": "unspecified", "observed_at": daily["date"],
        "provenance": {"source": "other_device", "source_scope": "device"},
    })
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="morning")).body)

    assert "510 千卡（账户记录）" in text
    assert "260 千卡（其他设备记录）" in text
    assert "Zepp 汇总" not in text


def test_morning_displays_sync_limitation_and_recovery_disagreement():
    payload = _daily_payload()
    payload["delivery_metadata"] = {"sync_degraded": True}
    payload["features"]["hrv"].update({"corroboration_status": "conflicting", "corroboration_affects_decision": True})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "部分数据尚未完成更新" in text
    assert text.count("本报告使用已保存") == 1
    assert "HRV 证据存在分歧" in text
    assert "单个 HRV 读数" in text
    assert "Amazfit" not in text


def test_facts_only_morning_shows_oxygen_when_hrv_and_rhr_are_missing():
    daily = _daily_payload()
    daily["delivery_metadata"] = {"facts_only": True}
    daily["features"]["hrv"].update({"value_ms": None, "rhr_bpm": None})
    daily["features"]["overnight_vitals"].update({
        "respiratory_rate": None,
        "oxygen": {"status": "AVAILABLE", "median_percent": 96, "sample_count": 12},
    })

    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    text = _visible_text(message.body)
    assert "夜间血氧中位数 96%" in text
    assert "睡眠 HRV 未取得可用读数" in text
    assert "综合判定" not in text and "今天的安排" not in text


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
    assert "停止条件" in text
    assert "安全限制" not in text
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


def test_evening_stress_only_keeps_a_measured_reference_fact():
    daily = _daily_payload()
    activity = daily["features"]["activity"]
    activity["heart_rate"] = None
    activity["stress"] = None
    activity["stress_summary"] = [{"metric": "stress", "value": 32, "unit": "score"}]

    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)
    assert "压力记录：平均压力评分 32（设备评分，仅作参考）" in text
    assert "设备压力日记录：" not in text
    intraday = next(section for section in message.extras["sections"] if section["key"] == "intraday")
    assert any(item.startswith("设备压力日记录：") for item in intraday["facts"])


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


def test_reports_group_data_notes_without_repeating_summary_or_generic_limits():
    daily = _daily_payload()
    daily["features"]["sleep"]["limitation_labels"] = ["睡眠分期有缺项，已记录时长仍可查看。"]
    morning = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    evening = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    monthly = _sent(lambda service: service.push_monthly_profile("test-user", synthetic_period_fixture("monthly")))
    weekly = _sent(lambda service: service.push_weekly_profile("test-user", synthetic_period_fixture("weekly")))

    for message in (morning, evening, monthly, weekly):
        text = _visible_text(message.body)
        assert "限制：" not in text
        assert "必要限制" not in text
    assert "数据说明" in _visible_text(morning.body)
    assert "睡眠分期有缺项" in _visible_text(morning.body)
    assert _visible_text(morning.body).count("出现疼痛时停止") == 1
    assert _visible_text(evening.body).count("户外跑：") == 1
    assert _visible_text(monthly.body).count("睡眠时长较前一期增加 5.7%") == 1


def test_morning_keeps_all_distinct_coverage_and_safety_notes():
    daily = _daily_payload()
    notes = [
        "运动记录覆盖尚未核实。", "夜间设备来源存在差异。",
        "睡眠分期有缺项。", "训练记录时段不完整。",
        "症状变化时停止训练并寻求评估。",
    ]
    daily["features"]["sleep"]["limitation_labels"] = notes
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="morning")).body)

    assert "数据说明" in text
    assert all(text.count(note) == 1 for note in notes)


def test_facts_only_morning_mentions_unknown_history_once():
    daily = _daily_payload()
    daily["delivery_metadata"] = {"facts_only": True}
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="morning")).body)

    assert text.count("部分运动记录来源还未查全") == 1
    assert "今天的安排" not in text


def test_evening_uses_supplied_energy_unit_and_only_observed_heart_rate_zones():
    daily = _daily_payload()
    daily["features"]["activity"]["energy"][0]["unit"] = "kJ"
    daily["features"]["training"]["running"]["recent_sessions"][0]["heart_rate_zones"] = [
        {"zone": 3, "label": "中等强度", "lower_bpm": 130, "upper_bpm": 150, "share_percent": 18},
    ]
    daily["features"]["training"]["recent_workouts"].append({
        "date": daily["date"], "type_label": "力量训练", "sport_mode_label": "力量训练",
        "training_family": "strength", "duration_minutes": 52, "distance_km": 0,
        "calories_kcal": 220, "heart_rate_avg_bpm": 110,
    })
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)
    detail = "\n".join(message.extras["sections"][0]["facts"])

    assert "510 kJ" in text and "510 千卡" not in text
    assert "中等强度（130–150 次/分钟） 18%" not in text
    assert "中等强度（130–150 次/分钟） 18%" in detail
    assert "低强度 0%" not in detail
    assert "力量训练：0.00 公里" not in text
    assert text.count("52 分钟") == 1
    assert "本次训练估算热量 220 千卡" in text


def test_evening_preserves_two_identical_workouts_as_two_observations():
    daily = _daily_payload()
    workout = daily["features"]["training"]["recent_workouts"][0]
    daily["features"]["training"]["recent_workouts"] = [deepcopy(workout), deepcopy(workout)]
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="evening")).body)

    assert text.count("户外跑：") == 2


def test_evening_matched_strength_keeps_heart_rate_missing_from_specialist():
    daily = _daily_payload()
    daily["features"]["training"]["recent_workouts"].append({
        "date": daily["date"], "type_label": "力量训练", "training_family": "strength",
        "duration_minutes": 52, "heart_rate_avg_bpm": 110,
    })
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="evening")).body)

    assert text.count("52 分钟") == 1
    assert "力量训练：平均心率 110 次/分钟" in text


def test_evening_matches_distinct_specialist_sessions_without_repeating_doses():
    daily = _daily_payload()
    first = daily["features"]["training"]["recent_workouts"][0]
    second = {**first, "duration_minutes": 30, "distance_km": 4.2, "calories_kcal": None}
    daily["features"]["training"]["recent_workouts"] = [first, second]
    run = daily["features"]["training"]["running"]["recent_sessions"][0]
    daily["features"]["training"]["running"]["recent_sessions"] = [run, {**run, "duration_minutes": 30, "distance_km": 4.2}]
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="evening")).body)

    assert text.count("总耗时 45 分钟") == 1
    assert text.count("总耗时 30 分钟") == 1
    assert text.count("距离 7.10 公里") == 1
    assert text.count("距离 4.20 公里") == 1
    assert "户外跑：30 分钟" not in text


def test_morning_does_not_repeat_a_caution_as_a_stop_condition():
    daily = _daily_payload()
    daily["events"] = [{"type": "RECOVERY_SUPPRESSED", "lifecycle": "ACTIVE", "summary": "出现疼痛时停止。"}]
    text = _visible_text(_sent(lambda service: service.push_daily_profile("test-user", daily, period="morning")).body)

    assert text.count("出现疼痛时停止") == 1
    assert "停止条件" in text


def test_four_complete_fixture_entrypoints_build_the_same_sections():
    daily = _daily_payload()
    morning = MorningBriefingEngine().build_payload(daily)
    evening = EveningBriefingEngine().build(daily)
    weekly = WeeklyBriefingEngine().build(synthetic_period_fixture("weekly"))
    monthly = MonthlyBriefingEngine().build(synthetic_period_fixture("monthly"))
    assert [section["key"] for section in morning["sections"]] == ["sleep", "recovery", "today_activity", "today_plan"]
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


def test_log_handler_never_logs_identity_or_health_report(caplog):
    with caplog.at_level(logging.INFO, logger="vitalis.push"):
        result = PushService(pushplus_token="").push(PushMessage(
            title="Private morning report", body="Sensitive health value 42",
            user_id="private-person",
        ))

    assert result["_log_handler"] == "ok"
    assert "report rendered" in caplog.text
    assert "private-person" not in caplog.text
    assert "Private morning report" not in caplog.text
    assert "Sensitive health value 42" not in caplog.text


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
