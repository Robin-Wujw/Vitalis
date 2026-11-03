"""Synthetic reader summaries: saved evidence and plans stay authoritative."""
from copy import deepcopy

import pytest

from vitalis.intelligence.report_summary import reader_briefing_summary


def _payload():
    return {
        "features": {
            "hrv": {
                "status": "AVAILABLE", "value_ms": 65,
                "deviation": {"direction": "above", "percent": 12},
                "fusion_direction": "above", "corroboration_status": "consistent",
                "rhr_bpm": 51, "rhr_deviation": {"direction": "below"},
            },
            "sleep": {
                "status": "AVAILABLE", "duration_minutes": 450,
                "duration_deviation": {"direction": "near"},
            },
            "recovery": {"state": "GOOD", "readiness_score": 86, "negative_signals": []},
            "training": {"load_state": "NORMAL"},
            "activity": {"steps": {"value": 10000, "deviation": {"percent": 49}}},
        },
        "decision": {
            "action": "TRAIN_NORMAL",
            "action_plan": {
                "safety_status": "CLEAR",
                "primary_session": {"session_type": "RUNNING", "intensity": "moderate",
                                    "stop_conditions": ["出现明显不适时停止。"]},
            },
        },
        "data_quality": {"status": "SUFFICIENT", "status_label": "数据完整"},
        "report_context": {},
        "events": [{"type": "ACTIVITY_SURGE", "summary": "步数 +49%"}],
    }


def _text(payload, **kwargs):
    lines = reader_briefing_summary(payload, **kwargs)
    assert isinstance(lines, list)
    assert len(lines) <= 3
    assert all(isinstance(line, str) and line.endswith("。") for line in lines)
    return "\n".join(lines)


def _spike(payload):
    payload["events"].append({
        "type": "TRAINING_LOAD_SPIKE", "lifecycle": "DETECTED",
        "deviation_percent": 35, "summary": "旧摘要：7日负荷+35%，注意疲劳。",
    })


@pytest.mark.parametrize("metric,direction,expected", [
    ("hrv", "above", "HRV 高于个人参照"),
    ("hrv", "below", "HRV 低于个人参照"),
    ("hrv", "near", "HRV 接近个人参照"),
    ("rhr", "above", "静息心率高于个人参照"),
    ("rhr", "below", "静息心率低于个人参照"),
    ("rhr", "near", "静息心率接近个人参照"),
    ("sleep", "above", "睡眠时长多于个人参照"),
    ("sleep", "below", "睡眠时长少于个人参照"),
    ("sleep", "near", "睡眠时长接近个人参照"),
])
def test_saved_directions_are_explained(metric, direction, expected):
    payload = _payload()
    if metric == "hrv":
        payload["features"]["hrv"]["deviation"]["direction"] = direction
        payload["features"]["hrv"]["fusion_direction"] = direction
    elif metric == "rhr":
        payload["features"]["hrv"]["rhr_deviation"]["direction"] = direction
    else:
        payload["features"]["sleep"]["duration_deviation"]["direction"] = direction
    assert expected in _text(payload)


def test_favorable_directions_do_not_become_warning_or_universal_clearance():
    payload = _payload()
    payload["events"].append({"type": "RECOVERY_SUPPRESSED", "summary": "多个恢复信号偏离基线。"})
    text = _text(payload)
    assert "HRV 高于" in text and "静息心率低于" in text
    for unwanted in ("偏弱", "谨慎", "恢复变差", "无风险", "绝对安全", "步数", "49%", "数据完整", "多个恢复信号偏离"):
        assert unwanted not in text


def test_one_negative_direction_does_not_create_rest_or_override_plan():
    payload = _payload()
    payload["features"]["hrv"]["fusion_direction"] = "below"
    payload["features"]["recovery"]["negative_signals"] = ["HRV_BELOW_BASELINE"]
    text = _text(payload)
    assert "HRV 低于" in text
    assert "今天按计划正常训练" in text
    assert "休息" not in text and "全休" not in text
    payload["decision"]["action_plan"]["primary_session"]["intensity"] = "low"
    assert "轻量训练" in _text(payload)
    assert "正常训练" not in _text(payload)


def test_negative_evidence_explains_existing_rest_without_new_thresholds():
    payload = _payload()
    payload["features"]["hrv"]["fusion_direction"] = "below"
    payload["features"]["hrv"]["rhr_deviation"]["direction"] = "above"
    payload["features"]["sleep"]["duration_deviation"]["direction"] = "below"
    payload["features"]["recovery"].update(state="SUPPRESSED", readiness_score=52)
    payload["decision"]["action"] = "REST"
    text = _text(payload)
    assert "HRV 低于" in text and "静息心率高于" in text and "睡眠时长少于" in text
    assert "偏弱" in text and "休息" in text


def test_saved_score_state_is_not_reclassified_from_score_or_favorable_signals():
    payload = _payload()
    recovery = payload["features"]["recovery"]
    recovery.update(state="SUPPRESSED", readiness_score=52)
    text = _text(payload)
    assert "准备度 52/100" in text and "偏弱" in text
    assert "HRV 高于" in text and "静息心率低于" in text
    recovery["state"] = "NORMAL"
    assert "偏弱" not in _text(payload)


def test_saved_negative_signal_can_supply_direction_without_recomputing_baseline():
    payload = _payload()
    payload["features"]["hrv"].update(fusion_direction="unknown", deviation=None)
    payload["features"]["recovery"]["negative_signals"] = ["HRV_BELOW_BASELINE"]
    assert "HRV 低于个人参照" in _text(payload)

    # A short night may match a user's usual short nights: keep both saved facts.
    payload["features"]["recovery"]["negative_signals"] = ["SLEEP_SHORT_DURATION"]
    text = _text(payload)
    assert "睡眠时长接近个人参照" in text and "睡眠时长不足" in text
    assert "睡眠时长少于" not in text
    payload["states"] = {"sleep": "BELOW_BASELINE"}
    payload["features"]["sleep"]["duration_deviation"] = {"direction": "unknown"}
    text = _text(payload)
    assert "睡眠时长不足" in text and "睡眠时长少于" not in text


def test_conflicting_negative_code_cannot_turn_high_hrv_into_low_hrv():
    payload = _payload()
    payload["features"]["recovery"]["negative_signals"] = ["HRV_BELOW_BASELINE", "RHR_ABOVE_BASELINE"]
    text = _text(payload)
    assert "HRV 高于" in text and "静息心率低于" in text
    assert "HRV 低于" not in text and "静息心率高于" not in text


@pytest.mark.parametrize("conflict", ["conflicting", "insufficient"])
def test_unqualified_hrv_does_not_erase_rhr_or_sleep(conflict):
    payload = _payload()
    payload["features"]["hrv"]["corroboration_status"] = conflict
    text = _text(payload)
    assert "HRV 高于" not in text
    assert "HRV" in text and "无法判断" in text
    assert "静息心率低于" in text and "睡眠时长接近" in text


def test_missing_sleep_only_limits_sleep_even_if_global_quality_is_sufficient():
    payload = _payload()
    payload["features"]["sleep"].update(status="INSUFFICIENT_DATA", duration_minutes=None)
    text = _text(payload)
    assert "睡眠记录缺失" in text and "无法判断睡眠" in text
    assert "HRV 高于" in text and "静息心率低于" in text
    assert "无法判断全部" not in text and "数据完整" not in text


def test_missing_baseline_is_not_inferred_from_percent_or_raw_value():
    payload = _payload()
    payload["features"]["sleep"]["duration_deviation"] = {"percent": -90, "direction": "unknown"}
    text = _text(payload)
    assert "睡眠时长少于" not in text
    assert "睡眠个人参照不足" in text


def test_spike_without_fatigue_observation_has_both_branches_and_no_extra_dose():
    payload = _payload()
    _spike(payload)
    payload["decision"]["action"] = "TRAIN_LIGHT"
    text = _text(payload)
    assert "负荷" in text and "不等于已经疲劳" in text
    assert "无明显疲劳" in text and "热身正常" in text
    assert "按计划轻量训练" in text and "不额外加量" in text
    assert "明显疲劳或热身异常" in text and "减量或停止要求" in text
    assert "旧摘要" not in text


@pytest.mark.parametrize("restriction", ["REST", "LIMITED", "PRIMARY_REST", "RECOVERY"])
@pytest.mark.parametrize("kind", ["morning", "evening"])
def test_rest_or_medical_restriction_wins_over_favorable_load_branch(restriction, kind):
    payload = _payload()
    _spike(payload)
    plan = payload["decision"]["action_plan"]
    if restriction == "LIMITED":
        plan["safety_status"] = "LIMITED"
    elif restriction == "PRIMARY_REST":
        plan["primary_session"]["session_type"] = "REST"
    else:
        payload["decision"]["action"] = restriction
    text = _text(payload, kind=kind)
    assert ("训练限制" if restriction == "LIMITED" else "恢复活动" if restriction == "RECOVERY" else "休息") in text
    assert "即使主观不累" in text
    if restriction == "LIMITED":
        assert "出现明显不适时停止" in text
    assert "若无明显疲劳且热身正常" not in text
    assert "可加量" not in text and "可以高强度" not in text


def test_evening_never_reuses_today_plan_as_tomorrow_permission():
    payload = _payload()
    _spike(payload)
    text = _text(payload, kind="evening")
    assert "明日" in text and "次晨" in text
    assert "允许训练" in text
    assert "今天" not in text and "明天可以练" not in text


def test_no_plan_means_no_permission_to_continue_training():
    payload = _payload()
    _spike(payload)
    payload["decision"]["action_plan"]["primary_session"] = None
    text = _text(payload)
    assert "没有可沿用的训练安排" in text
    assert "沿用今天" not in text


@pytest.mark.parametrize("mode", ["argument", "top_level", "metadata"])
def test_facts_only_never_contains_interpretation_or_advice(mode):
    payload = _payload()
    _spike(payload)
    if mode == "top_level":
        payload["facts_only"] = True
    elif mode == "metadata":
        payload["report_context"]["delivery_metadata"] = {"facts_only": True}
    assert reader_briefing_summary(payload, facts_only=mode == "argument") == []


def test_improving_load_event_is_not_presented_as_a_new_increase():
    payload = _payload()
    payload['events'].append({'type':'TRAINING_LOAD_SPIKE','lifecycle':'IMPROVING','deviation_percent':35,'duration_days':8})
    text = _text(payload)
    assert '提示正在改善' in text
    assert '近期训练负荷增加较快' not in text and '35%' not in text
    assert '若无明显疲劳且热身正常' in text


def test_event_lifetime_is_not_the_load_comparison_window():
    payload = _payload()
    _spike(payload)
    payload['events'][-1]['duration_days'] = 8
    payload['events'][-1]['deviation_percent'] = 35.2
    payload['trends'] = [{'metric':'training_load','window_days':7,'status':'AVAILABLE','change_percent':35.2}]
    text = _text(payload)
    assert '近 7 天训练负荷比前 7 天增加 35%' in text
    assert '近 8 天' not in text
    payload['trends'] = []
    assert '近 8 天' not in _text(payload)


def test_resolved_event_and_numeric_percentage_cannot_create_load_warning():
    payload = _payload()
    payload["features"]["training"]["load_7d_change_percent"] = 999
    payload["events"].append({"type": "TRAINING_LOAD_SPIKE", "lifecycle": "RESOLVED"})
    assert "负荷" not in _text(payload)


def test_summary_is_pure_bounded_and_ignores_steps_and_quality_badges():
    payload = _payload()
    _spike(payload)
    payload["features"]["sleep"].update(status="INSUFFICIENT_DATA", duration_minutes=None)
    before = deepcopy(payload)
    first = reader_briefing_summary(payload)
    assert first == reader_briefing_summary(payload)
    assert payload == before
    assert len(first) == 3
    assert all(line.count("。") == 1 for line in first)
    assert "步数" not in "".join(first) and "数据完整" not in "".join(first)


def test_empty_payload_has_no_invented_health_facts():
    assert reader_briefing_summary({}) == []
