"""Synthetic completed-work arithmetic, not capacity or strength inference."""

from copy import deepcopy
from datetime import date

import pytest

from vitalis.intelligence.strength_review import review_strength_workouts


START = date(2026, 8, 1)
END = date(2026, 8, 28)


def strength_set(order=1, **changes):
    return {
        "order": order, "source": "lap_62", "exercise_id": "synthetic_press",
        "exercise_name": "Synthetic press", "repetitions": 10,
        "weight_value": 20, "weight_unit": "kg", "weight_basis": None,
        "limitations": ["exercise_name_reference_mapping"], **changes,
    }


def workout(workout_id="current", day=20, sets=None, **changes):
    return {
        "source": "zepp", "workout_id": workout_id, "detail_available": True,
        "data": {
            "training_family": "strength", "started_at": f"2026-08-{day:02d}T08:00:00Z",
        },
        "detail": {"strength_sets": sets if sets is not None else [strength_set()],
                   "fetched_at": "2026-08-28T09:00:00Z"},
        **changes,
    }


def review(rows, **kwargs):
    return review_strength_workouts(rows, start=START, end=END, timezone_name="UTC", **kwargs)


def test_expanded_confirmation_budget_is_checked_before_allocating_sets():
    rows = [workout(confirmed_exercises=[{
        "exercise_id": "press", "exercise_name": "Press", "sets": 2001,
        "repetitions": 8, "weight_kg": 20,
    }])]
    with pytest.raises(ValueError, match="strength_review_set_budget_exceeded"):
        review(rows)


def test_unknown_basis_still_has_nominal_recorded_load_and_ordered_repetitions():
    result = review([workout(sets=[
        strength_set(3, repetitions=8), strength_set(1, repetitions=12),
        strength_set(2, repetitions=10),
    ])])
    session = result["sessions"][0]
    exercise = session["exercises"][0]
    assert session["date"] == "2026-08-20"
    assert session["fetched_at"] == "2026-08-28T09:00:00Z"
    assert [s["order"] for s in exercise["sets"]] == [1, 2, 3]
    assert [s["repetitions"] for s in exercise["sets"]] == [12, 10, 8]
    assert exercise["known_repetitions"] == 30
    assert exercise["weight_basis"] is None
    volume = exercise["nominal_volume"]
    assert volume["metric"] == "nominal_recorded_load"
    assert volume["value"] == 600
    assert volume["unit"] == "kg*reps"
    assert volume["known_sets"] == 3
    assert volume["state"] == "complete"
    assert exercise["qualification"] == "recorded_work_not_muscle_load"
    assert "weight_basis_unknown" in exercise["limitations"]
    assert "exercise_name_reference_mapping" in exercise["limitations"]
    assert result["exercise_totals"][0]["nominal_volume"]["value"] == 600


def test_bodyweight_repetitions_remain_available_without_zero_or_added_body_mass():
    result = review([workout(sets=[
        strength_set(1, weight_basis="bodyweight", weight_value=0, repetitions=12),
        strength_set(2, weight_basis="bodyweight", weight_value=5, repetitions=8),
    ])])
    exercise = result["sessions"][0]["exercises"][0]
    assert exercise["known_repetitions"] == 20
    assert [s["weight_value"] for s in exercise["sets"]] == [0, 5]
    assert exercise["nominal_volume"]["value"] is None
    assert exercise["nominal_volume"]["state"] == "not_applicable"
    assert result["exercise_totals"][0]["nominal_volume"]["value"] is None


def test_partial_sets_and_missing_session_keep_known_subtotals_with_missing_counts():
    result = review([
        workout("missing", 21, sets=[]),
        workout(sets=[strength_set(1), strength_set(2, weight_value=None),
                      strength_set(3, repetitions=None)]),
    ])
    exercise = result["sessions"][1]["exercises"][0]
    assert exercise["known_repetitions"] == 20
    assert exercise["coverage"]["repetitions_missing_sets"] == 1
    assert exercise["coverage"]["weight_missing_sets"] == 1
    assert exercise["nominal_volume"]["value"] == 200
    assert exercise["nominal_volume"]["known_sets"] == 1
    assert exercise["nominal_volume"]["missing_sets"] == 2
    assert exercise["nominal_volume"]["state"] == "partial"
    assert result["coverage"]["sessions_with_sets"] == 1
    assert result["coverage"]["sessions_missing_sets"] == 1
    assert result["coverage"]["upstream_coverage_verified"] is False
    assert result["sessions"][0]["exercises"] == []


def test_mixed_identity_unit_source_basis_device_side_and_machine_never_merge():
    variants = [
        {}, {"weight_unit": "lb", "weight_kg": 9.0718474},
        {"source": "strength_sets"}, {"weight_basis": "per_hand"},
        {"device_id": "device-a"}, {"device_id": "device-b"},
        {"machine_id": "machine-a"}, {"machine_id": "machine-b"},
        {"side": "left"}, {"side": "right"},
        {"exercise_id": "other_press"},
    ]
    result = review([
        workout(sets=[strength_set(i, **changes) for i, changes in enumerate(variants, 1)]),
        workout("other-source", 19, source="other_vendor"),
    ])
    groups = result["sessions"][0]["exercises"]
    assert len(groups) == len(variants)
    assert len(result["exercise_totals"]) == len(variants) + 1
    pounds = next(g for g in groups if g["weight_unit"] == "lb")
    assert pounds["nominal_volume"]["value"] == 200
    assert pounds["nominal_volume"]["unit"] == "lb*reps"
    single = next(g for g in groups if g["weight_basis"] == "per_hand")
    assert single["nominal_volume"]["value"] == 200  # Never infer two hands.
    assert "total_volume" not in result
    assert "capacity_volume_kg" not in result


def test_assistance_is_separate_and_negative_unlabelled_weight_is_not_positive_load():
    result = review([workout(sets=[
        strength_set(1, weight_basis="assistance", weight_value=15),
        strength_set(2, weight_value=-15),
        strength_set(3, exercise_id="unknown_unit", weight_unit=None),
    ])])
    assistance, negative, unknown_unit = result["sessions"][0]["exercises"]
    assert assistance["nominal_volume"]["value"] is None
    assert assistance["assistance_volume"]["value"] == 150
    assert assistance["assistance_volume"]["metric"] == "nominal_recorded_assistance"
    assert negative["nominal_volume"]["value"] is None
    assert negative["assistance_volume"]["value"] is None
    assert unknown_unit["nominal_volume"]["value"] is None
    assert unknown_unit["known_repetitions"] == 10


def test_recent_previous_is_same_group_not_just_latest_workout_and_is_stable():
    rows = [workout("older", 10, [strength_set(repetitions=8)]),
            workout("previous", 18, [strength_set(repetitions=9)]),
            workout("current", 20, [strength_set(repetitions=12)]),
            workout("wrong-unit", 19, [strength_set(weight_unit="lb")])]
    result = review(rows)
    assert result == review(list(reversed(rows)))
    current = result["sessions"][0]["exercises"][0]
    previous = current["previous"]
    assert previous["workout_id"] == "previous"
    assert previous["date"] == "2026-08-18"
    assert previous["current"]["repetitions"] == [12]
    assert previous["reference"]["repetitions"] == [9]
    assert previous["reference"]["weights"] == [20]
    assert previous["delta_total_repetitions"] == 3
    assert previous["delta_nominal_volume"] == 60
    assert previous["comparability"] == "conditional"
    assert previous["proves_strength_gain"] is False
    assert "weight_basis_unknown" in previous["limitations"]
    assert "device_unknown" in previous["limitations"]
    assert previous["coverage"]["compatible_previous_sessions"] == 2
    assert previous["coverage"]["sample_count"] == 2


def test_previous_allows_changed_weights_without_claiming_strength_and_retains_partial():
    rows = [workout("previous", 18, [strength_set(1), strength_set(2, repetitions=None)]),
            workout("current", 20, [strength_set(1, weight_value=25), strength_set(2)])]
    previous = review(rows)["sessions"][0]["exercises"][0]["previous"]
    assert previous["current"]["weights"] == [25, 20]
    assert previous["reference"]["weights"] == [20, 20]
    assert previous["matched_sets"][0]["delta_weight"] == 5
    assert previous["delta_total_repetitions"] is None
    assert previous["delta_nominal_volume"] is None
    assert previous["delta_known_nominal_volume"] == 250
    assert previous["coverage"]["reference"]["repetitions_missing_sets"] == 1


def test_same_timestamp_sessions_cannot_be_each_others_previous():
    result = review([workout("b"), workout("a")])
    assert all(g["previous"] is None for s in result["sessions"] for g in s["exercises"])
    assert result == review([workout("a"), workout("b")])


def test_confirmed_records_replace_vendor_sets_without_mutating_either_source():
    row = workout(confirmed_exercises=[{
        "id": "confirmation", "source": "user_confirmed", "order": 1,
        "exercise_id": "synthetic_press", "exercise_name": "Confirmed press",
        "sets": 2, "repetitions": 6, "weight_kg": 30, "weight_unit": "kg",
        "weight_basis": "per_hand", "created_at": "2026-08-22T11:00:00Z",
    }])
    original = deepcopy(row)
    session = review([row])["sessions"][0]
    assert row == original
    assert session["record_source"] == "user_confirmed"
    exercise = session["exercises"][0]
    assert exercise["source_scope"] == "user_confirmed"
    assert exercise["set_count"] == 2
    assert exercise["known_repetitions"] == 12
    assert exercise["nominal_volume"]["value"] == 360
    assert [s["within_record_order"] for s in exercise["sets"]] == [1, 2]
    assert all(s["fetched_at"] == "2026-08-22T11:00:00Z" for s in exercise["sets"])


def test_selected_confirmation_provenance_does_not_use_vendor_fetch_timestamp():
    row = workout(confirmed_exercises=[{
        "source": "user_confirmed", "order": 1, "exercise_name": "Confirmed press",
        "sets": 1, "repetitions": 6, "weight_kg": 30,
        "created_at": "2026-08-22T11:00:00Z",
    }])
    session = review([row])["sessions"][0]
    assert session["fetched_at"] == "2026-08-22T11:00:00Z"
    assert session["detail_fetched_at"] == "2026-08-28T09:00:00Z"
    assert session["exercises"][0]["sets"][0]["recorded_at"] == "2026-08-22T11:00:00Z"


def test_partial_comparison_exposes_both_group_and_session_missingness():
    previous_row = workout("previous", 18, [strength_set(repetitions=None)])
    previous_row["data"]["vendor_reported_sets"] = 3
    current_row = workout("current", 20, [strength_set()])
    previous = review([previous_row, current_row])["sessions"][0]["exercises"][0]["previous"]
    assert "partial_recorded_dose" in previous["limitations"]
    assert previous["coverage"]["reference_session"]["missing_summary_sets"] == 2
    assert previous["coverage"]["current_session"]["detail_complete"] is None


def test_finite_input_overflow_does_not_generate_nonfinite_volume():
    import json

    result = review([workout(sets=[strength_set(weight_value=1e308)])])
    json.dumps(result, allow_nan=False)
    exercise = result["sessions"][0]["exercises"][0]
    assert exercise["nominal_volume"]["value"] is None
    assert "arithmetic_out_of_range" in exercise["limitations"]


def test_confirmation_unknown_set_count_does_not_invent_one_set():
    row = workout(confirmed_exercises=[{
        "source": "user_confirmed", "order": 1, "exercise_name": "Confirmed press",
        "sets": None, "repetitions": 6, "weight_kg": 30,
    }])
    exercise = review([row])["sessions"][0]["exercises"][0]
    assert exercise["set_count"] is None
    assert exercise["known_repetitions"] is None
    assert exercise["sets"][0]["repetitions"] == 6
    assert exercise["nominal_volume"]["value"] is None
    assert exercise["coverage"]["records_with_unknown_set_count"] == 1


def test_unidentified_sets_are_visible_but_never_assumed_to_be_the_same_action():
    unknown = strength_set(exercise_id=None, exercise_name=None)
    result = review([workout("previous", 18, [unknown]), workout("current", 20, [unknown])])
    assert len(result["exercise_totals"]) == 2
    assert result["sessions"][0]["exercises"][0]["previous"] is None
    assert result["sessions"][0]["exercises"][0]["nominal_volume"]["value"] == 200


def test_local_dates_and_fetched_time_are_explicit_without_assuming_synced_coverage():
    row = workout(data={"training_family": "strength", "started_at": "2026-08-19T23:30:00Z"})
    result = review_strength_workouts([row], start=START, end=END, timezone_name="Asia/Shanghai", truncated=True)
    assert result["sessions"][0]["date"] == "2026-08-20"
    assert result["window"] == {"start": "2026-08-01", "end": "2026-08-28", "days": 28, "timezone": "Asia/Shanghai"}
    assert result["time_basis"] == "activity_day"
    assert result["truncated"] is True
    assert result["coverage"]["upstream_coverage_verified"] is False


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), True, "20"])
def test_invalid_numeric_weights_remain_missing(weight):
    exercise = review([workout(sets=[strength_set(weight_value=weight)])])["sessions"][0]["exercises"][0]
    assert exercise["nominal_volume"]["value"] is None
    assert exercise["known_repetitions"] == 10
