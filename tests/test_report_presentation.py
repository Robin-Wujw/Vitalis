from __future__ import annotations

from datetime import date

from vitalis.intelligence.report_presentation import (
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
