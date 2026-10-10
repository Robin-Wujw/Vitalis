from copy import deepcopy
from html.parser import HTMLParser
import logging

import httpx
import markdown
import pytest

from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
from vitalis.adapters.notifications import (
    PUSHPLUS_CONTENT_BUDGET, PUSHPLUS_CONTENT_LIMIT, PUSHPLUS_QUERY_URL, PUSHPLUS_URL, PushMessage, PushService,
)
from vitalis.intelligence.report_rendering import markdown_html_length, provider_text_length, render_report
from tests.test_report_content import synthetic_daily_fixture, synthetic_period_fixture
from tests.test_report_rendering import large_briefing


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _visible_text(body: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(markdown.markdown(body))
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
        "explicit_exercises": [{"exercise_name": "卧推", "sets": 4, "repetitions": 8, "weight_kg": 60, "rpe": 8, "rir": 2, "rest_seconds": 120}],
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


def _sent(service_call, *, template="markdown"):
    received = []
    service = PushService(pushplus_token="", template=template)
    service.add_handler(received.append)
    service_call(service)
    assert received
    return received[0]


def test_morning_push_uses_complete_report_fields_and_no_automatic_questionnaire():
    message = _sent(lambda service: service.push_daily_profile("test-user", _daily_payload(), period="morning"))
    text = _visible_text(message.body)
    assert message.template == "markdown"
    assert message.title.startswith("Vitalis 晨报 ·")
    assert "2026-09-05" not in message.title
    assert message.body.startswith("# 晨报 ·")
    assert "2026-09-05 · 数据截至" in text
    assert "睡眠时长" in text and "入睡" in text and "醒来" in text
    assert "HRV" in text and "静息心率" in text
    assert "主要安排：轻松跑" in text and "可选安排：拉类力量训练" in text
    assert "至少间隔 6 小时" not in text
    assert "训练后告诉我" not in text
    assert "feedback_prompt" not in text
    assert "Zepp 厂商汇总" not in text
    assert "vendor_readiness" not in text
    assert message.extras["metrics"]
    assert isinstance(message.extras["findings"], list)
    assert isinstance(message.extras["training"], list)
    assert message.extras["suggestions"]
    assert isinstance(message.extras["alerts"], list)
    assert all(section["display"] is False for section in message.extras["sections"])


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
    assert [metric["label"] for metric in message.extras["metrics"]] == ["睡眠时长", "静息心率", "HRV"]
    assert "步数 8,200 步" not in text
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
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    text = _visible_text(message.body)
    activity = next(section for section in message.extras["sections"] if section["key"] == "today_activity")

    assert "设备估算热量（统计范围待确认） 510 千卡（账户记录）" in activity["facts"]
    assert "设备估算热量（统计范围待确认） 260 千卡（其他设备记录）" in activity["facts"]
    assert "510 千卡（账户记录）" not in text
    assert "260 千卡（其他设备记录）" not in text
    assert "Zepp 汇总" not in text


def test_morning_displays_sync_limitation_and_recovery_disagreement():
    payload = _daily_payload()
    payload["delivery_metadata"] = {"sync_degraded": True}
    payload["features"]["hrv"].update({"corroboration_status": "conflicting", "corroboration_affects_decision": True})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "本次同步未完整完成" not in text
    assert text.count("HRV 记录存在来源分歧") == 1
    assert any(item.startswith("HRV 证据存在分歧") for item in message.extras["cautions"])
    assert "HRV 记录存在来源分歧，分别保留，不合并比较。" in message.extras["findings"]
    assert any(
        "不同记录不能直接混成一个值比较" in item
        for item in message.extras["sections"][1]["interpretation"]
    )
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
    assert "夜间血氧中位数｜96 %" in text
    assert "睡眠 HRV 未取得可用读数" not in text
    assert "综合判定" not in text and "今天的安排" not in text
    assert message.extras["summary"] == [
        "部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"
    ]
    assert message.extras["suggestions"] == []


def test_morning_insufficient_data_does_not_invent_training():
    payload = _daily_payload()
    payload["data_quality"] = {"status": "INSUFFICIENT", "status_label": "数据不足", "missing_required_signal_labels": ["睡眠时长", "心率变异性"]}
    payload["features"]["sleep"].update({"duration_minutes": None, "bedtime": None, "wake_time": None})
    payload["features"]["hrv"].update({"value_ms": None, "rhr_bpm": None})
    payload["decision"].update({"action": "INSUFFICIENT_DATA", "action_label": "数据不足，暂不建议", "limitation_labels": ["恢复决策所需信号不足"]})
    payload["decision"]["action_plan"].update({"primary_session": None, "optional_session": None})
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="morning"))
    text = _visible_text(message.body)
    assert "昨晚睡眠记录尚未同步" in text
    assert "今天不生成训练建议" not in text
    plan = next(section for section in message.extras["sections"] if section["key"] == "today_plan")
    assert "今天不生成训练建议。" in plan["facts"]
    assert "恢复决策所需信号不足" in plan["interpretation"]
    assert message.extras["metrics"][0]["value"] is None
    assert message.extras["metrics"][0]["gap"] == "昨晚睡眠记录尚未同步。"
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
    assert "安全限制" not in text
    assert "疼痛时停止" in text and "症状加重时寻求评估" in text and "动作失控时停止" in text
    assert message.extras["suggestions"] == [
        "主要安排：轻松跑 · 30–45 分钟 · 低强度 · 疼痛时停止。 · 症状加重时寻求评估。",
        "可选安排：拉类力量训练 · 40–60 分钟 · 中等强度 · 动作失控时停止。",
    ]


def test_evening_push_uses_top_metrics_and_actual_training_facts():
    message = _sent(lambda service: service.push_daily_profile("test-user", _daily_payload(), period="evening"))
    text = _visible_text(message.body)
    assert message.template == "markdown"
    assert message.title.startswith("Vitalis 晚报 ·")
    assert "2026-09-05" not in message.title
    assert message.body.startswith("# 晚报 ·")
    assert "2026-09-05 · 数据截至" in text
    assert "户外跑" in text and "45 分钟" in text and "7.10 公里" in text
    training_section = next(section for section in message.extras["sections"] if section["key"] == "training")
    assert any("节奏跑" in item for item in training_section["facts"])
    assert "节奏跑" not in text
    assert "卧推" in text and "4 组" in text and "60 kg" in text
    assert "步数" in text and "活动距离" in text and "活动时长" in text
    assert "设备估算热量（统计范围待确认）" in text and "本次训练估算热量" in text
    assert "训练后告诉我" not in text
    assert [metric["label"] for metric in message.extras["metrics"]] == [
        "睡眠时长", "步数", "力量训练时长",
    ]
    assert len(message.extras["training"]) == 2
    assert message.extras["suggestions"] == []
    assert message.extras["alerts"] == []


def test_evening_stress_only_keeps_a_measured_reference_fact():
    daily = _daily_payload()
    activity = daily["features"]["activity"]
    activity["heart_rate"] = None
    activity["stress"] = None
    activity["stress_summary"] = [{"metric": "stress", "value": 32, "unit": "score"}]

    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)
    assert "压力记录：平均压力评分 32（设备评分，仅作参考）" not in text
    assert "设备压力日记录：" not in text
    intraday = next(section for section in message.extras["sections"] if section["key"] == "intraday")
    assert "压力记录：平均压力评分 32（设备评分，仅作参考）。" in intraday["facts"]
    assert any(item.startswith("设备压力日记录：") for item in intraday["facts"])


def test_evening_low_confidence_run_does_not_name_classification():
    payload = _daily_payload()
    run = payload["features"]["training"]["running"]["recent_sessions"][0]
    run["confidence"] = "LOW"
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    training_section = next(section for section in message.extras["sections"] if section["key"] == "training")
    assert any("跑步课型暂不确定" in item for item in training_section["facts"])
    assert "跑步课型暂不确定" not in text
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
    training_section = next(section for section in message.extras["sections"] if section["key"] == "training")
    assert "没有已记录的正式训练场次" not in text
    assert "不等同于已确认休息日" not in text
    assert training_section["facts"] == ["当天没有已记录的正式训练场次；这不等同于已确认休息日。"]
    assert "补训练" not in text


def test_evening_strength_detail_gap_is_explicit_and_work_bouts_are_not_sets():
    payload = _daily_payload()
    payload["features"]["training"]["strength"]["recent_sessions"][0]["explicit_exercises"] = []
    message = _sent(lambda service: service.push_daily_profile("test-user", payload, period="evening"))
    text = _visible_text(message.body)
    training_section = next(section for section in message.extras["sections"] if section["key"] == "training")
    assert any("逐组动作、重复次数和重量" in item for item in training_section["facts"])
    assert any("工作段不能替代明确组数" in item for item in training_section["limitations"])
    assert "逐组动作、重复次数和重量" not in text


def test_html_report_escapes_user_controlled_values():
    payload = _daily_payload()
    payload["features"]["training"]["strength"]["recent_sessions"][0]["explicit_exercises"][0]["exercise_name"] = '<img src="x" onerror="alert(1)">卧推'
    message = _sent(
        lambda service: service.push_daily_profile("test-user", payload, period="evening"),
        template="html",
    )
    assert "&lt;img src=&quot;x&quot; onerror=&quot;alert(1)&quot;&gt;卧推" in message.body
    assert '<img src="x"' not in message.body
    assert message.template == "html"
    assert message.extras["template"] == "html"
    assert message.extras["renderer_version"] == "4.0"
    assert message.extras["content_sha256"]


def test_explicit_html_render_cross_fields_match_rendered_report():
    payload = _daily_payload()
    message = _sent(
        lambda service: service.push_daily_profile("test-user", payload, period="evening"),
        template="html",
    )
    rendered = render_report(EveningBriefingEngine().build(payload), target="html")

    assert message.title == rendered.title
    assert message.body == rendered.content
    assert message.template == rendered.template == "html"
    assert message.extras["renderer_version"] == rendered.renderer_version
    assert message.extras["content_sha256"] == rendered.content_sha256
    assert message.body.startswith("<div style=\"max-width:680px")


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
    assert "睡眠" in text and "下周重点" in text
    internal = "\n".join(
        fact for section in message.extras["sections"] for fact in section["facts"]
    )
    limitations = "\n".join(
        note for section in message.extras["sections"] for note in section["limitations"]
    )
    assert "训练记录覆盖" in str(message.extras["sections"])
    assert "轻松跑" in internal and "节奏或阈值跑" in internal
    assert "EASY_RUN" not in internal and "TEMPO_RUN" not in internal
    assert "睡眠时长较前一期增加" not in text
    assert "来源不同，前后窗口不作直接比较。" in limitations
    assert "保持当前结构" in text
    assert label in internal
    assert "daily.calories" not in text and "daily.calories" not in internal
    assert "UNKNOWN" not in text and "UNKNOWN" not in internal
    assert "user_fused" not in text and "source_scope" not in internal


def test_monthly_push_uses_same_report_sections_and_noncausal_association():
    profile = synthetic_period_fixture("monthly", "complete")
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_monthly_profile("test-user", profile)
    message = received[0]
    text = _visible_text(message.body)
    assert message.extras["period"] == "monthly"
    assert "睡眠" in text and "下月重点" in text
    titles = [section["title"] for section in message.extras["sections"]]
    assert titles == ["训练与睡眠覆盖", "持续恢复变化", "训练结构、活动与能量", "个人数据关联", "下月建议"]
    assert "个人数据关联" in str(message.extras["sections"])
    assert "个人数据关联" in text and "不表示因果" in text


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
    assert "数据说明" not in _visible_text(morning.body)
    assert "睡眠分期有缺项" not in _visible_text(morning.body)
    assert _visible_text(morning.body).count("出现疼痛时停止") == 0
    evening_training = next(section for section in evening.extras["sections"] if section["key"] == "training")
    assert any(item.startswith("户外跑：") for item in evening_training["facts"])
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

    assert "数据说明" not in text
    assert all(note not in text for note in notes)
    sleep = next(section for section in _sent(
        lambda service: service.push_daily_profile("test-user", daily, period="morning")
    ).extras["sections"] if section["key"] == "sleep")
    assert sleep["limitations"] == notes


def test_facts_only_morning_mentions_unknown_history_once():
    daily = _daily_payload()
    daily["delivery_metadata"] = {"facts_only": True}
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    text = _visible_text(message.body)

    assert "部分运动记录来源还未查全" not in text
    assert message.extras["summary"].count(
        "部分运动记录来源还未查全；已记录的训练照常展示，今天暂不生成训练安排。"
    ) == 1
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
    assert sum(item["duration_minutes"] == 52 for item in message.extras["training"]) == 2
    assert "本次训练估算热量 220 千卡" in text


def test_evening_preserves_two_identical_workouts_as_two_observations():
    daily = _daily_payload()
    workout = daily["features"]["training"]["recent_workouts"][0]
    daily["features"]["training"]["recent_workouts"] = [deepcopy(workout), deepcopy(workout)]
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)

    assert [item["title"] for item in message.extras["training"]].count("户外跑") == 2
    assert text.count("户外跑") == 2


def test_evening_matched_strength_keeps_heart_rate_missing_from_specialist():
    daily = _daily_payload()
    daily["features"]["training"]["recent_workouts"].append({
        "date": daily["date"], "type_label": "力量训练", "training_family": "strength",
        "duration_minutes": 52, "heart_rate_avg_bpm": 110,
    })
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)

    assert [item["title"] for item in message.extras["training"]] == ["户外跑", "力量训练", "推类"]
    generic = message.extras["training"][1]
    assert generic["duration_minutes"] == 52
    assert "平均心率 110 次/分钟" in generic["facts"]
    assert "力量训练：平均心率 110 次/分钟" not in text


def test_evening_matches_distinct_specialist_sessions_without_repeating_doses():
    daily = _daily_payload()
    first = daily["features"]["training"]["recent_workouts"][0]
    second = {**first, "duration_minutes": 30, "distance_km": 4.2, "calories_kcal": None}
    daily["features"]["training"]["recent_workouts"] = [first, second]
    run = daily["features"]["training"]["running"]["recent_sessions"][0]
    daily["features"]["training"]["running"]["recent_sessions"] = [run, {**run, "duration_minutes": 30, "distance_km": 4.2}]
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="evening"))
    text = _visible_text(message.body)
    training = message.extras["training"]
    internal_training = "\n".join(
        fact for section in message.extras["sections"] if section["key"] == "training"
        for fact in section["facts"]
    )

    assert internal_training.count("总耗时 45 分钟") == 1
    assert internal_training.count("总耗时 30 分钟") == 1
    assert [(item["title"], item["duration_minutes"]) for item in training[:4]] == [
        ("户外跑", 45.0), ("户外跑", 30.0), ("节奏跑", 45.0), ("节奏跑", 30.0),
    ]
    assert "户外跑：30 分钟" not in text


def test_morning_does_not_repeat_a_caution_as_a_stop_condition():
    daily = _daily_payload()
    daily["events"] = [{"type": "RECOVERY_SUPPRESSED", "lifecycle": "ACTIVE", "summary": "出现疼痛时停止。"}]
    message = _sent(lambda service: service.push_daily_profile("test-user", daily, period="morning"))
    text = _visible_text(message.body)

    assert text.count("出现疼痛时停止") == 1
    assert "停止条件" not in text
    assert message.extras["cautions"] == ["出现疼痛时停止。"]


def test_four_complete_fixture_entrypoints_build_the_same_sections():
    daily = _daily_payload()
    morning = MorningBriefingEngine().build_payload(daily)
    evening = EveningBriefingEngine().build(daily)
    weekly = WeeklyBriefingEngine().build(synthetic_period_fixture("weekly"))
    monthly = MonthlyBriefingEngine().build(synthetic_period_fixture("monthly"))
    assert [section["key"] for section in morning["sections"]] == ["sleep", "recovery", "today_activity", "today_plan"]
    assert [section.key for section in evening.sections[:4]] == ["training", "activity", "intraday", "recovery"]
    assert all(not section.display for section in evening.sections[:4])
    assert all(section.display for section in evening.sections[4:])
    assert weekly.sections and monthly.sections


def test_retrospective_evening_has_no_current_or_future_prescription():
    payload = _daily_payload()
    payload["delivery_metadata"] = {"retrospective": True}
    rendered = render_report(EveningBriefingEngine().build(payload), target="markdown")
    text = rendered.content
    title = rendered.title
    assert title.startswith("Vitalis 晚报 ·")
    assert "2026-09-05" not in title
    assert "2026-09-05 · 数据截至" in text
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
            return {"code": 200, "msg": "请求成功", "data": "synthetic-short-code"}

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

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "accepted"
    assert requests[0][0] == PUSHPLUS_URL
    assert requests[0][1]["json"]["token"] == "private-token"


@pytest.mark.parametrize(
    ("status_code", "outcome"),
    [(400, "failed"), (500, "uncertain")],
)
def test_webhook_http_status_classifies_remote_outcome(monkeypatch, status_code, outcome):
    webhook_url = "https://example.test/vitalis-hook"

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10.0, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            return httpx.Response(
                status_code,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(webhook_url=webhook_url, pushplus_token="").push(
        PushMessage(title="晨间日报", body="数据完整", user_id="user")
    )

    assert result["_webhook_handler"] == "error: delivery failed"
    assert result["_delivery_outcome"] == outcome


@pytest.mark.parametrize(
    ("status_code", "outcome"),
    [(400, "failed"), (500, "uncertain")],
)
def test_pushplus_http_status_classifies_remote_outcome(monkeypatch, status_code, outcome):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10.0, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            return httpx.Response(
                status_code,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(
        PushMessage(title="晨间日报", body="数据完整", user_id="user")
    )

    assert result["_pushplus_handler"] == "error: delivery failed"
    assert result["_delivery_outcome"] == outcome


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

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "error: delivery failed"
    assert "sensitive upstream response" not in result["_pushplus_handler"]
    assert "private-token" not in result["_pushplus_handler"]


def test_push_handler_error_does_not_echo_untrusted_exception(caplog):
    from vitalis.adapters.notifications import NotificationSendError

    service = PushService(pushplus_token="")

    def fail(_message):
        raise NotificationSendError("synthetic-token-private-health", ambiguous=True)

    service.add_handler(fail)
    result = service.push(PushMessage(title="synthetic", body="synthetic body", user_id="owner"))
    assert result["fail"] == "error: delivery failed"
    assert result["_delivery_outcome"] == "uncertain"
    assert "synthetic-token-private-health" not in caplog.text
    assert "synthetic-token-private-health" not in str(result)


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

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(PushMessage(title="晨间日报", body="数据完整", user_id="user"))
    assert result["_pushplus_handler"] == "error: delivery failed"
    assert result["_delivery_outcome"] == "uncertain"
    assert "private-token" not in result["_pushplus_handler"]


def test_pushplus_code_200_without_provider_id_is_uncertain(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "accepted"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            return Response()

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(pushplus_token="private-token").push(
        PushMessage(title="晨间日报", body="数据完整", user_id="user")
    )
    assert result["_pushplus_result"]["status"] == "uncertain"
    assert result["_delivery_outcome"] == "uncertain"


@pytest.mark.parametrize(("remote_status", "expected"), [(0, "accepted"), (1, "accepted"), (2, "delivered"), (3, "failed")])
def test_pushplus_status_query_maps_provider_states(monkeypatch, remote_status, expected):
    requests = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "data": {"status": remote_status}}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10.0, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(
        pushplus_token="private-token", pushplus_access_key="access-key"
    ).query_pushplus("short-code", poll_attempt_id="poll-1")
    assert result["status"] == expected
    assert result["provider_id"] == "short-code"
    assert result["poll_attempt_id"] == "poll-1"
    assert requests[0][0].endswith("?shortCode=short-code")
    assert requests[0][1]["headers"] == {"access-key": "access-key"}


def test_pushplus_query_timeout_keeps_accepted_without_post(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            calls.append(("get", url))
            raise httpx.ReadTimeout("query timeout")

        def post(self, url, **kwargs):
            calls.append(("post", url))
            pytest.fail("status timeout must not re-send")

    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", Client)
    result = PushService(
        pushplus_token="private-token", pushplus_access_key="access-key"
    ).query_pushplus("short-code")
    assert result["status"] == "accepted"
    assert calls == [("get", f"{PUSHPLUS_QUERY_URL}?shortCode=short-code")]


def _accepting_pushplus_client(requests):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 200, "msg": "请求成功", "data": "synthetic-short-code"}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    return Client


def test_pushplus_report_is_budgeted_below_the_provider_limit(monkeypatch):
    requests = []
    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", _accepting_pushplus_client(requests))
    briefing = large_briefing()
    assert markdown_html_length(render_report(briefing, "markdown").content) > PUSHPLUS_CONTENT_LIMIT

    result = PushService(pushplus_token="private-token").push_morning_briefing("user", briefing)

    assert result["_pushplus_handler"] == "accepted"
    content = requests[0][1]["json"]["content"]
    # PushPlus counts Markdown as the HTML it converts to, which is longer than the source.
    assert provider_text_length(markdown.markdown(content)) <= PUSHPLUS_CONTENT_BUDGET < PUSHPLUS_CONTENT_LIMIT
    assert "本节另有" in content


@pytest.mark.parametrize("body", [
    "数" * (PUSHPLUS_CONTENT_LIMIT + 1),
    # Under the limit as Markdown source, over it once every paragraph becomes <p>…</p>.
    "\n\n".join(["数据"] * 2500),
], ids=["one-long-paragraph", "short-paragraphs-converted"])
def test_pushplus_refuses_content_above_provider_limit_without_request(monkeypatch, body):
    requests = []
    monkeypatch.setattr("vitalis.adapters.notifications.httpx.Client", _accepting_pushplus_client(requests))

    result = PushService(pushplus_token="private-token").push(PushMessage(title="晚报", body=body, user_id="user"))

    assert requests == []
    assert result["_pushplus_handler"] == "error: delivery failed"
    assert result["_pushplus_result"]["status"] == "failed"
    assert result["_pushplus_result"]["retryable"] is False
