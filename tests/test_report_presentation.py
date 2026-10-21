from __future__ import annotations

from vitalis.intelligence.report_presentation import (
    daily_display_sections,
    daily_presentation,
    period_presentation,
)


def _daily(**overrides):
    payload = {
        "date": "2026-10-07",
        "report_context": {
            "as_of": "2026-10-07T21:20:00Z",
            "target_day_complete": True,
        },
        "features": {
            "sleep": {
                "duration_minutes": 420,
                "duration_deviation": {
                    "baseline_reference": 410,
                    "percent": 2.4,
                    "baseline_window_days": 28,
                    "unit": "min",
                },
            },
            "hrv": {
                "status": "AVAILABLE",
                "value_ms": 65,
                "deviation": {
                    "baseline_reference": 63,
                    "percent": 3.2,
                    "baseline_window_days": 28,
                    "unit": "ms",
                },
                "rhr_bpm": 52,
                "rhr_deviation": {
                    "baseline_reference": 50,
                    "percent": 4,
                    "baseline_window_days": 28,
                    "unit": "bpm",
                },
            },
            "activity": {
                "steps": {
                    "value": 8300,
                    "unit": "steps",
                    "deviation": {
                        "baseline_reference": 7600,
                        "percent": 9.2,
                        "baseline_window_days": 28,
                        "unit": "steps",
                    },
                },
            },
            "training": {
                "strength_duration_minutes": 48,
                "strength_sessions": 1,
                "recent_workouts": [],
                "running": {"recent_sessions": []},
                "strength": {"recent_sessions": []},
            },
        },
    }
    payload.update(overrides)
    return payload


def test_morning_metrics_are_stable_and_hrv_is_optional():
    result = daily_presentation(_daily(), morning=True)
    assert [item["key"] for item in result["metrics"]] == ["sleep", "rhr", "hrv"]
    assert result["metrics"][0]["value"] == 420
    assert result["metrics"][0]["unit"] == "min"
    comparison = result["metrics"][0]["comparison"]
    assert comparison["reference_value"] == 410
    assert comparison["change_percent"] == 2.4

    conflict = _daily()
    conflict["features"]["hrv"]["corroboration_status"] = "conflicting"
    result = daily_presentation(conflict, morning=True)
    assert [item["key"] for item in result["metrics"]] == ["sleep", "rhr"]


def test_daily_missing_sleep_uses_local_gap_without_blocking_activity():
    payload = _daily()
    payload["features"]["sleep"]["duration_minutes"] = None
    payload["features"]["activity"]["steps"] = {"value": 8300}
    result = daily_presentation(payload)
    assert result["metrics"][0]["key"] == "sleep"
    assert result["metrics"][0]["value"] is None
    assert result["metrics"][0]["gap"]
    assert any(item["key"] == "steps" for item in result["metrics"])


def test_incomplete_day_does_not_project_activity_comparison():
    payload = _daily()
    payload["report_context"]["target_day_complete"] = False
    result = daily_presentation(payload)
    steps = next(item for item in result["metrics"] if item["key"] == "steps")
    assert steps["value"] == 8300
    assert steps["comparison"] is None


def test_workout_identity_requires_source_and_id_and_keeps_unmatched_records():
    payload = _daily()
    payload["features"]["training"] = {
        "recent_workouts": [
            {
                "date": "2026-10-07",
                "source": "zepp",
                "workout_id": "generic-1",
                "sport_mode_label": "户外跑",
                "duration_minutes": 40,
                "distance_km": 6,
            }
        ],
        "running": {
            "recent_sessions": [
                {
                    "date": "2026-10-07",
                    "source": "other",
                    "workout_id": "specialist-1",
                    "classification_label": "跑步专项",
                    "confidence": "HIGH",
                    "duration_minutes": 40,
                    "distance_km": 6,
                    "average_pace_seconds_per_km": 400,
                }
            ]
        },
        "strength": {"recent_sessions": []},
    }
    result = daily_presentation(payload)
    assert len(result["training"]) == 2
    assert any(item["title"] == "户外跑" for item in result["training"])
    assert any(item["title"] == "跑步专项" for item in result["training"])


def test_strength_sets_preserve_distribution_and_adjacent_grouping():
    payload = _daily()
    payload["features"]["training"] = {
        "recent_workouts": [],
        "running": {"recent_sessions": []},
        "strength": {
            "recent_sessions": [
                {
                    "date": "2026-10-07",
                    "source": "zepp",
                    "workout_id": "strength-1",
                    "focus_label": "力量训练",
                    "explicit_exercises": [
                        {
                            "exercise_name": "A",
                            "exercise_id": "a",
                            "sets": 1,
                            "repetitions": 12,
                            "weight_value": 10,
                            "weight_unit": "kg",
                            "weight_basis": "machine",
                            "source": "vendor_explicit",
                        },
                        {
                            "exercise_name": "B",
                            "exercise_id": "b",
                            "sets": 1,
                            "repetitions": 10,
                            "source": "user_confirmed",
                        },
                        {
                            "exercise_name": "A",
                            "exercise_id": "a",
                            "sets": 1,
                            "repetitions": 8,
                            "weight_value": 10,
                            "weight_unit": "kg",
                            "weight_basis": "machine",
                            "source": "vendor_explicit",
                        },
                    ],
                    "comparisons": [
                        {
                            "exercise_id": "a",
                            "reference_workout_date": "2026-10-01",
                            "comparable": True,
                            "delta_total_repetitions": 2,
                        }
                    ],
                }
            ],
        },
    }
    result = daily_presentation(payload)
    workout = result["training"][0]
    assert [item["name"] for item in workout["exercises"]] == ["A", "B", "A"]
    assert [item["sets"][0]["repetitions"] for item in workout["exercises"]] == [12, 10, 8]
    assert workout["exercises"][0]["sets"][0]["weight_basis"] == "machine"
    assert workout["exercises"][0]["comparison"]
    assert workout["exercises"][0]["reference_date"] == "2026-10-01"


def test_period_comparisons_are_date_labeled_and_gated():
    payload = {
        "period_start": "2026-10-01",
        "period_end": "2026-10-07",
        "report_context": {
            "reference_period": {"start": "2026-09-24", "end": "2026-09-30"}
        },
        "facts": {
            "sleep": {
                "available_days": 2,
                "previous_available_days": 7,
                "average_minutes": 420,
                "previous_average_minutes": 410,
                "change_percent": 2.4,
            },
            "activity": {
                "metrics": [
                    {
                        "metric": "steps",
                        "average": 8000,
                        "previous_average": 7600,
                        "change_percent": 5.3,
                        "complete_days": 7,
                        "previous_complete_days": 7,
                    }
                ]
            },
            "training": {"strength_sessions": 2},
        },
        "inferences": {},
        "actions": {},
    }
    result = period_presentation(payload, "weekly")
    assert [item["key"] for item in result["metrics"]] == [
        "sleep_average", "steps_average", "strength_frequency"
    ]
    assert result["metrics"][0]["comparison"] is None
    comparison = result["metrics"][1]["comparison"]
    assert comparison["label"] == "上期"
    assert comparison["reference_period_start"] == "2026-09-24"
    assert comparison["reference_period_end"] == "2026-09-30"


def test_facts_only_has_no_prescription_projection():
    payload = _daily()
    payload["decision"] = {
        "action": "TRAIN_LIGHT",
        "action_label": "轻量训练",
        "action_plan": {
            "primary_session": {"title": "unsafe plan", "stop_conditions": []}
        },
    }
    result = daily_presentation(payload, morning=True, facts_only=True)
    assert result["suggestions"] == []
    assert result["findings"] == []


def test_short_daily_comparison_label_hides_internal_baseline_window_but_keeps_it_in_data():
    result = daily_presentation(_daily(), morning=True)

    comparison = result["metrics"][0]["comparison"]
    assert comparison["label"] == "个人参照"
    assert comparison["baseline_window_days"] == 28
    assert "近 28 日" not in comparison["label"]


def test_visible_daily_activity_suppresses_contradictory_vendor_active_minutes():
    payload = _daily()
    payload["features"]["activity"]["active_minutes"] = {"value": 30}
    payload["features"]["training"]["recent_workouts"] = [{
        "date": "2026-10-07", "duration_minutes": 45,
    }]

    sections = daily_display_sections(payload)
    activity = next(item for item in sections if item["key"] == "display_activity")

    assert "活动时长 30 分钟" not in activity["facts"]
    assert any("设备活动总时长小于当天已记录训练时长" in item for item in activity["limitations"])


def test_strength_card_projects_saved_capacity_without_recalculating_raw_sets():
    payload = _daily()
    payload["features"]["training"]["strength"]["recent_sessions"] = [
        {
            "date": "2026-10-01",
            "explicit_exercises": [{
                "source": "user_confirmed", "exercise_id": "bench_press", "exercise_name": "卧推",
                "sets": 3, "repetitions": 8, "weight_kg": 50,
                "weight_unit": "kg", "weight_basis": "total",
            }],
        },
        {
            "date": "2026-10-07",
            "capacity_details": [{"exercise_name": "卧推", "volume": 1200,
                                  "weight_basis": "total", "estimated_1rm": None}],
            "dose_coverage": {"recorded_sets": 4, "volume_sets": 3},
            "muscle_doses": [
                {"muscle_group_label": "胸部", "direct_sets": 3, "indirect_sets": 0},
                {"muscle_group_label": "肱三头肌", "direct_sets": 0, "indirect_sets": 3},
            ],
            "explicit_exercises": [{
                "source": "user_confirmed", "exercise_id": "bench_press", "exercise_name": "卧推",
                "sets": 3, "repetitions": 8, "weight_kg": 60,
                "weight_unit": "kg", "weight_basis": "total",
            }, {
                "source": "lap_62", "vendor_exercise_code": 999999,
                "repetitions": 12, "weight_value": 20,
            }],
        },
    ]

    card = next(item for item in daily_display_sections(payload) if item["key"] == "display_strength_card")
    text = " ".join(card["facts"])

    assert "卧推容量 1,200 kg·次" in text
    assert "本次记录 4 组" in text
    assert "其中 3 组有明确重量容量" in text
    assert "主要练到胸部" in text
    assert "肱三头肌 3 组辅助参与" in text
    assert "直接 3 组" not in text
    assert "间接 3 组" not in text
    assert "1,440" not in text
    assert "估算 1RM" not in text
    assert "容量占比" not in text
    assert "999999" not in text


def test_running_card_reuses_analytics_and_does_not_claim_same_heart_rate():
    payload = _daily()
    payload["features"]["training"]["running"]["recent_sessions"] = [{
        "date": "2026-10-07",
        "classification_label": "轻松跑",
        "heart_rate_zone_source": "lactate_threshold",
        "heart_rate_zones": [
            {"zone": 1, "label": "一区：轻松", "share_percent": 72},
            {"zone": 2, "label": "二区：耐力", "share_percent": 28},
        ],
        "cardiac_drift_percent": 4.5,
        "average_pace_seconds_per_km": 360,
        "average_heart_rate_bpm": 145,
        "comparable_baseline": {
            "median_pace_seconds_per_km": 375,
            "pace_difference_percent": -4.0,
            "median_heart_rate_bpm": 142,
        },
    }]

    card = next(item for item in daily_display_sections(payload) if item["key"] == "display_running_card")
    text = " ".join(card["facts"])

    assert "心率区间分布" in text
    assert "心率漂移 +4.5%" in text
    assert "相近距离的近期配速对照" in text
    assert "同心率" not in text
    assert "同心率" not in " ".join(card["limitations"])


def _running_card(session):
    payload = _daily()
    payload["features"]["training"]["running"]["recent_sessions"] = [{"date": "2026-10-07", **session}]
    return next(item for item in daily_display_sections(payload) if item["key"] == "display_running_card")


def test_running_card_without_heart_rate_detail_states_each_gap():
    card = _running_card({"average_pace_seconds_per_km": 380})
    assert card["facts"] == []
    limitations = " ".join(card["limitations"])
    assert "没有足够心率证据" in limitations
    assert "未计算心率漂移" in limitations
    assert "没有足够的相近距离历史样本" in limitations


def test_running_card_too_short_for_drift_keeps_zones_and_says_why():
    card = _running_card({
        "confidence": "MODERATE",
        "heart_rate_zone_source": "device_workout",
        "heart_rate_zones": [{"zone": 2, "label": "二区：耐力", "share_percent": 100}],
        "limitations": ["时长不足 20 分钟，未计算心率漂移。"],
    })
    assert "心率区间分布（设备区间）：二区：耐力 100%。" in card["facts"][0]
    assert "时长不足 20 分钟，未计算心率漂移。" in card["limitations"]
    assert not any("漂移 " in fact for fact in card["facts"])


def test_partial_day_steps_say_why_they_are_not_compared():
    payload = _daily()
    payload["report_context"]["target_day_complete"] = False
    payload["features"]["activity"]["steps"] = {
        "value": 8000, "deviation": {"percent": -20.0, "direction": "below", "baseline_reference": 10000},
    }
    steps = next(item for item in daily_presentation(payload)["metrics"] if item["key"] == "steps")
    assert steps["comparison"] is None
    assert steps["detail"] == "今天还没过完，暂不和平时比较。"
