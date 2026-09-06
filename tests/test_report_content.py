from datetime import date, timedelta

import pytest

from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.monthly_briefing import MonthlyBriefingEngine
from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine
from vitalis.services.push_service import PushService, _render_report_html


TARGET_DATE = date(2026, 9, 5)


def _training_plan():
    return {
        "primary_session": {
            "title": "轻松跑",
            "focus": "补足有氧频次并控制恢复成本",
            "intensity_label": "低强度",
            "total_duration_minutes": [30, 45],
            "steps": [{
                "order": 1,
                "name": "热身",
                "duration_minutes": [6, 8],
                "intensity": "轻松",
                "instructions": ["先快走，再逐渐过渡到慢跑"],
            }],
            "personalization_reasons": ["近 7 天完成跑步 2 次，今天优先维持跑步频次。"],
            "stop_conditions": ["出现疼痛时停止。"],
        },
        "optional_session": {
            "title": "拉类力量训练",
            "focus": "背部和肱二头肌",
            "intensity_label": "中等强度",
            "total_duration_minutes": [40, 60],
            "steps": [],
            "stop_conditions": ["动作失控时停止。"],
        },
        "session_relationship": "ADDITION",
        "session_relationship_label": "可分开完成，至少间隔 6 小时",
        "safety_status": "CLEAR",
        "safety_status_label": "安全门控通过",
    }


def synthetic_daily_fixture(scenario="complete"):
    """Return one self-consistent synthetic DailyProfile-shaped payload."""
    if scenario not in {"complete", "partial", "missing", "heterogeneous"}:
        raise ValueError(f"unsupported fixture scenario: {scenario}")
    missing = scenario == "missing"
    partial = scenario == "partial"
    heterogeneous = scenario == "heterogeneous"
    quality_status = "INSUFFICIENT" if missing else "PARTIAL" if partial else "SUFFICIENT"
    quality_label = "数据不足" if missing else "部分可用" if partial else "数据完整"
    activity = {
        "status": "INSUFFICIENT_DATA" if missing else "AVAILABLE",
        "steps": None if missing else {"value": 8200, "deviation": {"percent": 8.0, "direction": "above"}, "provenance": {"source": "zepp", "source_scope": "user_fused"}},
        "distance_km": None if missing else {"value": 6.2, "provenance": {"source": "zepp", "source_scope": "device"}},
        "active_minutes": None if missing else {"value": 48, "provenance": {"source": "zepp", "source_scope": "normalized_daily_record"}},
        "energy": [] if missing else [
            {"value": 510, "unit": "kcal", "metric": "calories", "observed_at": TARGET_DATE.isoformat(), "provenance": {"source": "zepp", "source_scope": "user_fused"}, "role": "unspecified", "estimated": True, "source_field": "daily.calories"},
            {"value": 180, "unit": "kcal", "metric": "calories", "observed_at": TARGET_DATE.isoformat(), "provenance": {"source": "zepp", "source_scope": "device"}, "role": "workout", "estimated": True, "source_field": "workout.calories"},
        ],
        "heart_rate": None,
        "stress": None,
    }
    if heterogeneous and not missing:
        activity["energy"].append({
            "value": 260,
            "unit": "kcal",
            "metric": "calories",
            "observed_at": TARGET_DATE.isoformat(),
            "provenance": {"source": "other_device", "source_scope": "device"},
            "role": "unspecified",
            "estimated": True,
            "source_field": "activity.calories",
            "limitations": ["统计范围待确认"],
        })
    training = {
        "today_duration_minutes": None if missing else 45,
        "today_load": None if missing else 62,
        "recent_workouts": [] if missing else [{
            "date": TARGET_DATE.isoformat(),
            "type_label": "户外跑",
            "duration_minutes": 45,
            "distance_km": 7.1,
            "calories_kcal": 180,
            "heart_rate_avg_bpm": 149,
        }],
        "running": {"recent_sessions": [] if missing else [{
            "date": TARGET_DATE.isoformat(),
            "classification": "TEMPO_RUN",
            "classification_label": "节奏跑",
            "confidence": "HIGH",
            "duration_minutes": 45,
            "moving_duration_minutes": 43,
            "distance_km": 7.1,
            "average_pace_seconds_per_km": 380,
            "average_heart_rate_bpm": 149,
            "heart_rate_zones": [],
        }]},
        "strength": {"recent_sessions": []},
    }
    hrv = {
        "status": "INSUFFICIENT_DATA" if missing else "AVAILABLE",
        "preferred_metric": "hrv_rmssd" if heterogeneous else "sleep_hrv",
        "value_ms": None if missing else 71,
        "deviation": None if missing else {"percent": 3.0, "direction": "near"},
        "rhr_bpm": None if missing else 52,
        "rhr_metric": "sleep_rhr",
        "rhr_deviation": None if missing else {"percent": 6.0, "direction": "above"},
        "corroboration_status": "conflicting" if heterogeneous else "consistent",
        "corroboration_affects_decision": heterogeneous,
        "streams": [] if missing else [
            {"metric": "sleep_hrv", "value_ms": 71, "device_id": "balance", "selected": True, "baseline_distinct_days": 28},
            {"metric": "hrv_rmssd", "value_ms": 66, "device_id": "strap", "selected": False, "baseline_distinct_days": 21},
        ],
        "limitations": ["不同来源仅作并列观察，不能直接合并"] if heterogeneous else [],
    }
    return {
        "analysis_run_id": f"fixture-daily-{scenario}",
        "user_id": "fixture-user",
        "date": TARGET_DATE.isoformat(),
        "generated_at": "2026-09-06T08:00:00+00:00",
        "report_context": {
            "as_of": "2026-09-05T22:00:00+00:00",
            "target_day_complete": not partial,
            "heterogeneous_sources": heterogeneous,
        },
        "data_quality": {
            "status": quality_status,
            "status_label": quality_label,
            "missing_required_signal_labels": ["睡眠 HRV"] if missing else [],
        },
        "features": {
            "sleep": {
                "status": "INSUFFICIENT_DATA" if missing else "AVAILABLE",
                "duration_minutes": None if missing else 435,
                "bedtime": None if missing else "23:40:00",
                "wake_time": None if missing else "06:55:00",
                "awake_minutes": None if missing else 28,
                "wake_count": None if missing else 2,
                "duration_deviation": None if missing else {"percent": 2.4, "direction": "near"},
                "regularity_minutes": None if missing else 19,
                "limitations": [],
            },
            "hrv": hrv,
            "overnight_vitals": {
                "status": "INSUFFICIENT_DATA" if missing else "AVAILABLE",
                "respiratory_rate": None if missing else 14.2,
                "respiratory_rate_deviation": None if missing else {"percent": 1.1, "direction": "near"},
                "oxygen": {"status": "INSUFFICIENT"},
            },
            "recovery": {
                "state": "INSUFFICIENT_DATA" if missing else "NORMAL",
                "state_label": "恢复信号不足" if missing else "恢复一般",
                "positive_signal_labels": [] if missing else ["睡眠时长接近个人范围"],
                "negative_signal_labels": [] if missing else ["静息心率高于个人基线"],
                "limitations": [],
            },
            "activity": activity,
            "training": training,
        },
        "events": [],
        "decision": {
            "action": "INSUFFICIENT_DATA" if missing else "TRAIN_LIGHT",
            "action_label": "暂不生成训练建议" if missing else "轻量训练",
            "action_plan": {**({"primary_session": None, "optional_session": None, "session_relationship": "NONE", "safety_status": "LIMITED", "safety_status_label": "数据覆盖有限"} if missing else _training_plan())},
            "evidence": {"facts": [] if missing else [{"label": "静息心率高于个人基线"}]},
            "limitation_labels": ["恢复决策所需信号不足"] if missing else [],
        },
    }


def synthetic_period_fixture(period, scenario="complete"):
    """Return a valid rolling seven- or 28-day synthetic profile payload."""
    if period not in {"weekly", "monthly"}:
        raise ValueError(f"unsupported period: {period}")
    if scenario not in {"complete", "partial", "missing", "heterogeneous"}:
        raise ValueError(f"unsupported fixture scenario: {scenario}")
    days = 7 if period == "weekly" else 28
    period_start = TARGET_DATE - timedelta(days=days - 1)
    missing = scenario == "missing"
    partial = scenario == "partial"
    heterogeneous = scenario == "heterogeneous"
    if missing:
        available = 0
        record_days = 0
        unknown_days = days
    elif partial:
        available = 4 if period == "weekly" else 18
        record_days = available
        unknown_days = days - available
    else:
        available = days
        record_days = days
        unknown_days = 0
    previous_comparable = scenario == "complete"
    current_sleep = None if missing else 428
    current_hrv = None if missing else 69
    current_rhr = None if missing else 53
    previous_sleep = 405 if previous_comparable else None
    previous_hrv = 65 if previous_comparable else None
    previous_rhr = 51 if previous_comparable else None
    current_steps = None if missing else 58000 if period == "weekly" else 232000
    previous_steps = 7600 * days if previous_comparable else None
    current_training = {
        "coverage_status": "UNKNOWN" if missing else "PARTIAL" if partial else "COMPLETE",
        "record_days": record_days,
        "unknown_days": unknown_days,
        "totals_are_partial": missing or partial,
        "workout_count": None if missing else 5 if period == "weekly" else 20,
        "training_days": None if missing else 3 if period == "weekly" else 12,
        "strength_sessions": None if missing else 2 if period == "weekly" else 8,
        "rest_days": None if missing or partial else 4 if period == "weekly" else 16,
        "duration_minutes": None if missing else 210 if period == "weekly" else 840,
        "vendor_load": None if missing else 480 if period == "weekly" else 1920,
        "running_sessions": None if missing else 3 if period == "weekly" else 12,
        "running_distance_km": None if missing else 23.4 if period == "weekly" else 93.6,
        "running_duration_minutes": None if missing else 142 if period == "weekly" else 568,
        "running_classification_counts": {} if missing else ({"EASY_RUN": 2, "TEMPO_RUN": 1} if period == "weekly" else {"EASY_RUN": 8, "TEMPO_RUN": 4}),
        "strength_duration_minutes": None if missing else 68 if period == "weekly" else 272,
        "strength_explicit_sessions": None if missing else 2 if period == "weekly" else 8,
        "strength_sets": None if missing else 18 if period == "weekly" else 72,
        "workout_calories_kcal": None if missing else 920 if period == "weekly" else 3680,
        "workout_calories_sessions": 0 if missing else 4 if period == "weekly" else 16,
        "limitations": [],
    }
    activity_metrics = [] if missing else [
        {"metric": "steps", "unit": "steps", "period_days": days, "available_days": available, "complete_days": available, "previous_available_days": days if previous_comparable else 0, "previous_complete_days": days if previous_comparable else 0, "total": current_steps, "average": current_steps / available if available else None, "previous_total": previous_steps, "previous_average": previous_steps / days if previous_steps else None, "change_percent": 9.0 if previous_comparable else None, "total_change_percent": 9.0 if previous_comparable else None, "totals_are_partial": not (not missing and not partial), "provenance": {"source": "zepp", "source_scope": "user_fused"}, "source_field": "daily.steps"},
        {"metric": "calories", "unit": "kcal", "role": "unspecified", "period_days": days, "available_days": available, "complete_days": available, "previous_available_days": days if previous_comparable else 0, "previous_complete_days": days if previous_comparable else 0, "total": 400 * available, "average": 400, "previous_total": 360 * days if previous_comparable else None, "previous_average": 360 if previous_comparable else None, "change_percent": 11.1 if previous_comparable else None, "total_change_percent": 11.1 if previous_comparable else None, "totals_are_partial": partial or missing, "provenance": {"source": "zepp", "source_scope": "user_fused"}, "source_field": "daily.calories"},
    ]
    if heterogeneous and activity_metrics:
        activity_metrics[0]["provenance"] = {"source": "other_device", "source_scope": "device", "device_id": "other-device"}
        activity_metrics[0]["previous_available_days"] = 0
        activity_metrics[0]["previous_complete_days"] = 0
        activity_metrics[0]["previous_total"] = None
        activity_metrics[0]["previous_average"] = None
        activity_metrics[0]["change_percent"] = None
        activity_metrics[0]["total_change_percent"] = None
        activity_metrics[0]["limitations"] = ["前后窗口来源不同，不能直接比较"]
    recovery_streams = []
    if period == "monthly" and not missing:
        recovery_streams = [{
            "metric": "sleep_hrv", "metric_label": "睡眠 HRV", "source": "other_device" if heterogeneous else "zepp", "source_scope": "device", "device_id": "other-device" if heterogeneous else "balance", "unit": "ms", "available_days": available, "previous_available_days": 0 if heterogeneous else (days - 7 if previous_comparable else 0), "median": current_hrv, "previous_median": None if heterogeneous or not previous_comparable else previous_hrv, "change_percent": None if heterogeneous or not previous_comparable else 6.2,
        }]
    quality = {
        "status": "INSUFFICIENT" if missing else "PARTIAL" if partial else "SUFFICIENT",
        "status_label": "数据不足" if missing else "部分可用" if partial else "数据完整",
        "sleep_days": available,
        "hrv_days": available,
        "activity_days": available,
        "training_record_days": record_days,
        "training_days": current_training["training_days"],
        "limitations": ["来源不同，前后窗口不作直接比较。"] if heterogeneous else [],
    }
    inferences = {
        "key_changes": [] if missing or heterogeneous or not previous_comparable else ["睡眠时长较前一期增加 5.7%。"],
        "limitations": (["部分日期尚未核实。"] if partial else []) + (["前后窗口来源不同，不能直接比较。"] if heterogeneous else []),
        "personal_associations": [],
    }
    recommendations = [] if missing else [{"title": "保持当前结构", "action": "在恢复允许时保持跑步与力量交替，避免连续叠加高强度。", "reasons": ["两期恢复变化仍需结合覆盖。"]}]
    common = {
        "analysis_run_id": f"fixture-{period}-{scenario}",
        "user_id": "fixture-user",
        "period_start": period_start.isoformat(),
        "period_end": TARGET_DATE.isoformat(),
        "generated_at": "2026-09-06T08:00:00+00:00",
        "report_context": {"as_of": "2026-09-05T22:00:00+00:00", "heterogeneous_sources": heterogeneous},
        "data_quality": quality,
        "facts": {
            "sleep": {"available_days": available, "average_minutes": current_sleep, "previous_average_minutes": previous_sleep, "change_percent": 5.7 if previous_comparable else None, "bedtime_regularity_minutes": None if missing else 19},
            "recovery": {"hrv_available_days": available, "hrv_metric": "sleep_hrv", "hrv_metric_label": "睡眠 HRV", "hrv_median_ms": current_hrv, "hrv_previous_median_ms": previous_hrv if previous_comparable else None, "hrv_change_percent": 6.2 if previous_comparable else None, "rhr_available_days": available, "rhr_median_bpm": current_rhr, "rhr_previous_median_bpm": previous_rhr if previous_comparable else None, "rhr_change_percent": 3.9 if previous_comparable else None, "streams": recovery_streams},
            "training": current_training,
            "activity": {"available_days": available, "total_steps": current_steps, "average_steps": current_steps / available if current_steps and available else None, "previous_average_steps": previous_steps / days if previous_steps else None, "steps_change_percent": 9.0 if previous_comparable else None, "active_minutes": None if missing else 330 if period == "weekly" else 1320, "metrics": activity_metrics},
            "feedback": {"response_count": 0 if missing else 3, "average_session_rpe": None if missing else 6.3, "average_physical_fatigue": None if missing else 2.8, "average_mental_state": None, "average_muscle_soreness": None},
        },
        "inferences": inferences,
        "actions": {"recommendations": recommendations},
    }
    if period == "monthly":
        common["inferences"]["personal_associations"] = [] if missing or heterogeneous else [{"summary": "睡眠时长与次日跑步时长呈中等正向关联", "paired_days": 18, "limitations": []}]
    return common


@pytest.fixture
def complete_report_fixtures():
    return {
        "morning": synthetic_daily_fixture("complete"),
        "evening": synthetic_daily_fixture("complete"),
        "weekly": synthetic_period_fixture("weekly", "complete"),
        "monthly": synthetic_period_fixture("monthly", "complete"),
    }


@pytest.mark.parametrize("scenario", ["complete", "partial", "missing", "heterogeneous"])
def test_four_report_scenarios_have_full_sections(scenario):
    daily = synthetic_daily_fixture(scenario)
    morning = MorningBriefingEngine().build_payload(daily)
    evening = EveningBriefingEngine().build(daily)
    weekly = WeeklyBriefingEngine().build(synthetic_period_fixture("weekly", scenario))
    monthly = MonthlyBriefingEngine().build(synthetic_period_fixture("monthly", scenario))
    assert [item["key"] for item in morning["sections"]] == ["sleep", "recovery", "today_plan"]
    assert [item.key for item in evening.sections] == ["training", "activity", "intraday", "recovery"]
    assert len(weekly.sections) == 5 and len(monthly.sections) == 5
    assert weekly.period_start == date.fromisoformat(synthetic_period_fixture("weekly", scenario)["period_start"])
    assert (weekly.period_end - weekly.period_start).days == 6
    assert (monthly.period_end - monthly.period_start).days == 27
    for report in (morning, evening.model_dump(), weekly.model_dump(), monthly.model_dump()):
        text = str(report)
        assert "训练后告诉我" not in text
        assert "feedback_prompt" not in text
        assert "UNKNOWN" not in text
        assert "Zepp 厂商汇总" not in text


def test_complete_fixture_has_real_plan_and_sufficient_period_data():
    daily = synthetic_daily_fixture("complete")
    assert daily["decision"]["action_plan"]["primary_session"] is not None
    weekly = synthetic_period_fixture("weekly", "complete")
    monthly = synthetic_period_fixture("monthly", "complete")
    assert weekly["facts"]["training"]["record_days"] == 7
    assert weekly["facts"]["training"]["unknown_days"] == 0
    assert monthly["facts"]["training"]["record_days"] == 28
    assert monthly["facts"]["training"]["unknown_days"] == 0
    assert weekly["facts"]["sleep"]["average_minutes"] is not None


def test_missing_fixture_has_no_fake_values_or_rest_claims():
    daily = synthetic_daily_fixture("missing")
    period = synthetic_period_fixture("monthly", "missing")
    assert daily["features"]["sleep"]["duration_minutes"] is None
    assert daily["features"]["activity"]["energy"] == []
    assert daily["decision"]["action_plan"]["primary_session"] is None
    assert period["facts"]["sleep"]["bedtime_regularity_minutes"] is None
    assert period["facts"]["training"]["rest_days"] is None
    assert period["facts"]["activity"]["total_steps"] is None


def test_partial_fixture_keeps_current_facts_but_blocks_cross_period_comparison():
    period = synthetic_period_fixture("weekly", "partial")
    assert period["facts"]["training"]["record_days"] == 4
    assert period["facts"]["training"]["unknown_days"] == 3
    assert period["facts"]["sleep"]["previous_average_minutes"] is None
    assert period["facts"]["recovery"]["hrv_previous_median_ms"] is None
    briefing = WeeklyBriefingEngine().build(period)
    text = str(briefing.model_dump())
    assert "变化 5.7%" not in text
    assert "部分核实" in text


def test_heterogeneous_fixture_does_not_make_invalid_cross_source_comparison():
    period = synthetic_period_fixture("monthly", "heterogeneous")
    metric = period["facts"]["activity"]["metrics"][0]
    assert metric["provenance"]["source"] == "other_device"
    assert metric["previous_average"] is None
    assert metric["change_percent"] is None
    assert period["facts"]["recovery"]["streams"][0]["previous_median"] is None
    briefing = MonthlyBriefingEngine().build(period)
    text = str(briefing.model_dump())
    assert "睡眠时长较前一期增加" not in text
    assert "前后窗口来源不同" in text


def test_morning_preserves_recovery_disagreement_and_does_not_judge_single_value():
    result = MorningBriefingEngine().build_payload(synthetic_daily_fixture("heterogeneous"))
    text = str(result)
    assert "存在分歧" in text
    assert "71 毫秒" in text
    assert "绝对" not in text


def test_evening_separates_energy_roles_and_shows_activity():
    result = EveningBriefingEngine().build(synthetic_daily_fixture()).model_dump()
    text = str(result)
    assert "设备估算热量（统计范围待确认）" in text
    assert "本次训练估算热量" in text
    assert "步数" in text and "活动距离" in text and "活动时长" in text
    assert "daily.calories" not in text and "activity.calories" not in text and "workout.calories" not in text
    assert "user_fused" not in text and "source_scope" not in text
    assert "相加" not in text


def test_weekly_maps_running_classifications_and_consumes_inferences():
    result = WeeklyBriefingEngine().build(synthetic_period_fixture("weekly", "heterogeneous"))
    text = str(result)
    assert "轻松跑" in text and "节奏或阈值跑" in text
    assert "EASY_RUN" not in text and "TEMPO_RUN" not in text
    assert "睡眠时长较前一期增加" not in text
    assert "前后窗口来源不同" in text


def test_monthly_association_is_explicitly_non_causal():
    result = MonthlyBriefingEngine().build(synthetic_period_fixture("monthly", "complete"))
    text = str(result)
    assert "个人关联" in text
    assert "不表示因果" in text


def test_html_escapes_untrusted_fact_text():
    html = _render_report_html(["## 事实", "", "恶意 <script>alert(1)</script>"])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_push_monthly_profile_uses_same_sections_without_scheduling():
    received = []
    service = PushService(pushplus_token="")
    service.add_handler(received.append)
    service.push_monthly_profile("fixture-user", synthetic_period_fixture("monthly", "complete"))
    assert received
    assert "持续恢复变化" in received[0].body
    assert received[0].extras["period"] == "monthly"


@pytest.mark.parametrize("period", ["weekly", "monthly"])
def test_period_activity_metrics_render_human_units_without_raw_metric_names(period):
    payload = synthetic_period_fixture(period, "complete")
    payload["facts"]["activity"]["metrics"] = [
        {
            "metric": "steps",
            "unit": "steps",
            "total": 58_000,
            "average": 8_285.7,
            "available_days": 7,
            "complete_days": 7,
            "previous_available_days": 7,
            "previous_complete_days": 7,
        },
        {
            "metric": "distance_km",
            "unit": "km",
            "total": 42.5,
            "average": 6.1,
            "available_days": 7,
            "complete_days": 7,
            "previous_available_days": 7,
            "previous_complete_days": 7,
        },
        {
            "metric": "active_minutes",
            "unit": "min",
            "total": 330,
            "average": 47.1,
            "available_days": 7,
            "complete_days": 7,
            "previous_available_days": 7,
            "previous_complete_days": 7,
        },
        {
            "metric": "calories",
            "unit": "kcal",
            "role": "unspecified",
            "total": 2_800,
            "average": 400,
            "available_days": 7,
            "complete_days": 7,
            "previous_available_days": 7,
            "previous_complete_days": 7,
        },
        {
            "metric": "vendor_internal_activity",
            "unit": "steps",
            "total": 10,
            "average": 2,
            "available_days": 7,
            "complete_days": 7,
            "previous_available_days": 7,
            "previous_complete_days": 7,
        },
    ]
    engine = WeeklyBriefingEngine() if period == "weekly" else MonthlyBriefingEngine()
    text = str(engine.build(payload).model_dump())
    total_prefix = "本期" if period == "weekly" else "合计"
    assert f"{total_prefix} 58,000 步" in text and "日均 8,285.7 步" in text
    assert f"{total_prefix} 42.5 公里" in text and "日均 6.1 公里" in text
    assert f"{total_prefix} 330 分钟" in text and "日均 47.1 分钟" in text
    calories_total = f"{total_prefix} 2,800 千卡"
    assert calories_total in text and "日均 400 千卡" in text
    assert "vendor_internal_activity" not in text
    assert "其他观测" in text


@pytest.mark.parametrize("period", ["weekly", "monthly"])
def test_period_activity_metric_limitations_survive_partial_report_projection(period):
    payload = synthetic_period_fixture(period, "partial")
    limitation = "周期存在未观测日，总量为已记录下界。"
    payload["facts"]["activity"]["metrics"][0]["limitations"] = [limitation]
    engine = WeeklyBriefingEngine() if period == "weekly" else MonthlyBriefingEngine()
    report = engine.build(payload)
    section_key = "activity_feedback" if period == "weekly" else "training_activity"
    section = next(item for item in report.sections if item.key == section_key)
    assert limitation in section.limitations
    assert section.limitations.count(limitation) == 1
    assert payload["facts"]["activity"]["metrics"][0]["totals_are_partial"] is True


@pytest.mark.parametrize("period", ["weekly", "monthly"])
@pytest.mark.parametrize("unit, shown", [("kJ", "kJ"), (None, "单位未提供")])
def test_period_energy_does_not_infer_kcal_from_metric_name(period, unit, shown):
    payload = synthetic_period_fixture(period, "complete")
    payload["facts"]["activity"]["metrics"] = [{
        "metric": "calories", "role": "unspecified", "unit": unit,
        "total": 123, "average": 41,
    }]
    engine = WeeklyBriefingEngine() if period == "weekly" else MonthlyBriefingEngine()
    report = engine.build(payload)
    section_key = "activity_feedback" if period == "weekly" else "training_activity"
    section = next(item for item in report.sections if item.key == section_key)
    energy_line = next(item for item in section.facts if "统计范围待确认" in item)
    assert f"123 {shown}" in energy_line
    assert f"41 {shown}" in energy_line
    assert "千卡" not in energy_line
