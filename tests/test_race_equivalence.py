"""Synthetic contracts for conditional race equivalence, never personal forecasts."""

import json
import math
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from vitalis.intelligence.race_equivalence import race_equivalence

START = "2025-02-03T08:00:00+08:00"
END = "2025-02-03T08:50:01+08:00"


def _workout(**changes):
    return {
        "workout_id": "synthetic-run",
        "source": "synthetic-watch",
        "name": "Synthetic ordinary training run",
        "type": "running",
        "sport_mode_label": "Synthetic outdoor running",
        "started_at": START,
        "ended_at": END,
        "duration": 50,
        "distance_km": 10.0,
        **changes,
    }


def _assert_error(code, workout=None, detail=None, **kwargs):
    with pytest.raises(ValueError) as exc:
        race_equivalence(_workout() if workout is None else workout, detail, **kwargs)
    assert str(exc.value) == code


def test_default_scenario_uses_elapsed_seconds_and_preserves_source_facts():
    result = race_equivalence(_workout(), None)

    assert result["kind"] == "race_equivalence"
    assert result["target_km"] == 21.0975
    assert result["effort_basis"] == "scenario"
    assert result["equivalence"]["seconds"] == pytest.approx(3001 * (21.0975 / 10) ** 1.06)
    facts = result["source_facts"]
    assert facts["source"] == "synthetic-watch"
    assert facts["workout_id"] == "synthetic-run"
    assert facts["name"] == "Synthetic ordinary training run"
    assert facts["sport_mode_label"] == "Synthetic outdoor running"
    assert facts["type"] == "running"
    assert facts["distance_km"] == 10.0
    assert facts["distance_unit"] == "km"
    assert facts["started_at"] == START
    assert facts["ended_at"] == END
    assert facts["duration_minutes"] == 50
    assert facts["time_seconds"] == 3001
    assert facts["time_basis"] == "elapsed_timestamps"
    assert facts["time_resolution_seconds"] == 1
    assert facts["fields_used"] == ["type", "distance_km", "started_at", "ended_at"]
    assert facts["duration_comparison"] == {
        "state": "within_minute_resolution", "elapsed_minus_duration_seconds": 1.0,
    }
    assert facts["fetched_at"] is None
    assert facts["timestamp_timezone_basis"] == {
        "started_at": "explicit_offset", "ended_at": "explicit_offset",
    }


def test_method_and_qualification_do_not_claim_a_personal_forecast_or_probability():
    result = race_equivalence(_workout(), None)
    method = result["method"]
    assert method["name"] == "Riegel"
    assert method["version"] == "1.0"
    assert method["formula"] == "T2 = T1 * (D2 / D1) ** exponent"
    assert method["point_exponent"] == 1.06
    assert method["sensitivity_exponents"] == [1.04, 1.10]
    assert result["qualification"] == {
        "status": "conditional_equivalence",
        "validation_state": "not_validated_personal_forecast",
        "actual_race_fitness_prediction": False,
        "maximal_effort_verified": False,
    }
    assert result["basis"]["kind"] == "scenario_assumption"
    assert result["basis"]["maximal_effort"] == "assumed"
    assert result["basis"]["persisted"] is False
    assert "what-if" in result["basis"]["description"].lower()
    assert result["assumptions"]
    assert result["sensitivity_range"]["interpretation"] == "exponent_sensitivity_not_prediction_interval"
    encoded = json.dumps(result, allow_nan=False)
    assert "confidence_percent" not in encoded
    assert "predicted_race_time" not in encoded
    assert "predicted_fitness" not in encoded


def test_user_reported_maximal_is_explicit_transient_and_unverified():
    workout = _workout()
    result = race_equivalence(workout, None, effort_basis="user_reported_maximal")
    scenario = race_equivalence(workout, None)

    assert result["effort_basis"] == "user_reported_maximal"
    assert result["basis"]["kind"] == "transient_user_report"
    assert result["basis"]["maximal_effort"] == "user_reported"
    assert result["basis"]["persisted"] is False
    assert result["basis"]["independently_verified"] is False
    assert "race, test or all-out" in result["basis"]["description"]
    assert result["equivalence"] == scenario["equivalence"]
    assert result["qualification"]["maximal_effort_verified"] is False
    assert "effort_basis" not in workout


@pytest.mark.parametrize("value", [None, "", "maximal", "race", "SCENARIO", True, 1, [], {}])
def test_invalid_effort_basis_has_stable_error(value):
    _assert_error("invalid_effort_basis", effort_basis=value)


@pytest.mark.parametrize("rpe", [None, 1, 10, {"value": 1}, {"value": 10}, float("nan")])
def test_ordinary_run_needs_no_rpe_hr_or_pace_gate(rpe):
    workout = _workout(
        session_rpe=rpe, heart_rate_avg=90, heart_rate_max=100,
        moving_pace_seconds_per_km=120, classification="RECOVERY_RUN",
        race=True, maximal_effort=True,
    )
    detail = {"samples": [{"metric": "speed", "value": 15, "unit": "m/s"}]}
    result = race_equivalence(workout, detail)

    assert result["effort_basis"] == "scenario"
    assert result["basis"]["maximal_effort"] == "assumed"
    assert result["equivalence"] == race_equivalence(_workout(), None)["equivalence"]
    assert "heart_rate_avg" not in result["source_facts"]["fields_used"]


def test_same_distance_is_identity_in_point_and_sensitivity():
    result = race_equivalence(_workout(), None, target_km=10)

    assert result["equivalence"] == {
        "seconds": 3001.0, "display": "0:50:01", "display_resolution_seconds": 1,
    }
    assert result["sensitivity_range"]["seconds"] == [3001.0, 3001.0]
    assert result["sensitivity_range"]["display"] == ["0:50:01", "0:50:01"]
    assert result["sensitivity_range"]["exponents_at_bounds"] == [1.04, 1.10]


@pytest.mark.parametrize("target", [3, 5, 10, 21.0975, 42.195])
def test_sensitivity_bounds_are_sorted_in_both_distance_directions(target):
    result = race_equivalence(_workout(), None, target_km=target)
    pairs = sorted((3001 * (target / 10) ** exponent, exponent) for exponent in (1.04, 1.10))

    assert result["sensitivity_range"]["seconds"] == pytest.approx([pair[0] for pair in pairs])
    assert result["sensitivity_range"]["exponents_at_bounds"] == [pair[1] for pair in pairs]
    low, high = result["sensitivity_range"]["seconds"]
    assert low <= result["equivalence"]["seconds"] <= high


@pytest.mark.parametrize("source,target", [(3, 3), (3, 13.5), (10, 42.195), (50, 3)])
def test_supported_domain_endpoints_and_longer_source_runs(source, target):
    result = race_equivalence(
        _workout(distance_km=source, started_at=None, ended_at=None, duration=300),
        None, target_km=target,
    )
    assert result["target_km"] == target
    assert result["source_facts"]["distance_km"] == source
    assert result["method"]["domain"]["max_target_source_ratio"] == 4.5


@pytest.mark.parametrize("value", [0, -1, True, False, "10", "", [], {}, float("nan"), float("inf"), -float("inf"), 10**400])
def test_source_distance_must_be_positive_finite_number(value):
    _assert_error("invalid_distance", _workout(distance_km=value))


def test_missing_source_distance_does_not_guess_raw_units():
    workout = _workout(distance_km=None, distance=10000, distance_meters=10000)
    _assert_error("missing_distance", workout, {"distance_km": 10})
    del workout["distance_km"]
    _assert_error("missing_distance", workout)


@pytest.mark.parametrize("value", [0.001, 1, 2.999999])
def test_source_distance_below_three_km_is_outside_domain(value):
    _assert_error("source_distance_out_of_range", _workout(distance_km=value))


@pytest.mark.parametrize("value", [None, 0, -1, True, "21.0975", [], {}, float("nan"), float("inf"), 10**400])
def test_target_distance_must_be_positive_finite_number(value):
    _assert_error("invalid_target_distance", target_km=value)


@pytest.mark.parametrize("value", [2.999999, 42.195001, 100])
def test_target_distance_bounds(value):
    _assert_error("target_distance_out_of_range", target_km=value)


def test_distance_ratio_limit_is_not_a_blanket_scenario_refusal():
    workout = _workout(distance_km=3)
    _assert_error("distance_ratio_out_of_range", workout, target_km=13.500001)
    assert race_equivalence(workout, None, target_km=13.5)["equivalence"]["seconds"] > 0


@pytest.mark.parametrize("value", [None, "", "run", "RUNNING", "walking", "cycling", "strength", 1, [], {}])
def test_only_canonical_running_type_is_eligible(value):
    _assert_error("unsupported_workout_type", _workout(type=value))


@pytest.mark.parametrize("value", [None, [], "workout", 1, True])
def test_invalid_workout_container_is_rejected(value):
    with pytest.raises(ValueError, match="^invalid_workout$"):
        race_equivalence(value, None)


@pytest.mark.parametrize("value", [[], "detail", 1, True])
def test_invalid_detail_container_is_rejected(value):
    _assert_error("invalid_detail", detail=value)


@pytest.mark.parametrize("field,value", [
    ("distance_unit", "m"), ("distance_unit", "mi"), ("distance_unit", ""),
    ("distance_unit", []), ("duration_unit", "s"), ("duration_unit", "hours"),
    ("duration_unit", ""), ("duration_unit", {}),
])
def test_conflicting_explicit_units_are_not_converted_or_guessed(field, value):
    _assert_error("invalid_units", _workout(**{field: value}))


@pytest.mark.parametrize("duration_unit", [None, "min", "minutes"])
def test_optional_canonical_units_are_accepted(duration_unit):
    result = race_equivalence(_workout(distance_unit="km", duration_unit=duration_unit), None)
    assert result["source_facts"]["distance_unit"] == "km"


def test_missing_endpoints_fall_back_to_rounded_integer_minutes():
    result = race_equivalence(_workout(started_at=None, ended_at=None, duration=51), None, target_km=10)

    assert result["equivalence"] == {
        "seconds": 3060.0, "display": "51 min", "display_resolution_seconds": 60,
    }
    facts = result["source_facts"]
    assert facts["time_basis"] == "rounded_minutes"
    assert facts["time_resolution_seconds"] == 60
    assert facts["time_seconds"] == 3060
    assert facts["fields_used"] == ["type", "distance_km", "duration"]
    assert facts["duration_comparison"]["state"] == "elapsed_unavailable"
    assert facts["pauses"]["time_handling"] == "duration_pause_inclusion_unknown"
    assert "rounded_minute_source_time" in result["limitations"]


@pytest.mark.parametrize("missing", ["started_at", "ended_at"])
def test_one_valid_timestamp_can_be_preserved_with_minute_fallback(missing):
    workout = _workout(**{missing: None})
    result = race_equivalence(workout, None)
    assert result["source_facts"]["time_basis"] == "rounded_minutes"
    assert result["source_facts"]["started_at"] == workout["started_at"]
    assert result["source_facts"]["ended_at"] == workout["ended_at"]


def test_exact_timestamps_do_not_require_summary_duration():
    workout = _workout(duration=None)
    for value in (workout, {key: item for key, item in workout.items() if key != "duration"}):
        result = race_equivalence(value, None)
        assert result["source_facts"]["time_seconds"] == 3001
        assert result["source_facts"]["duration_minutes"] is None
        assert result["source_facts"]["duration_comparison"]["state"] == "unavailable"


@pytest.mark.parametrize("value", [0, -1, True, False, 50.0, 50.5, "50", "00:50:00", [], {}, float("nan"), float("inf"), 10**400])
def test_explicit_invalid_duration_is_not_hidden_by_exact_timestamps(value):
    _assert_error("invalid_duration", _workout(duration=value))


@pytest.mark.parametrize("patch", [
    {"started_at": None, "ended_at": None, "duration": None},
    {"started_at": START, "ended_at": None, "duration": None},
    {"started_at": None, "ended_at": END, "duration": None},
])
def test_unknown_duration_has_stable_error_and_does_not_guess_detail_or_pace(patch):
    workout = _workout(**patch, duration_seconds=3001, moving_pace_seconds_per_km=300)
    detail = {"duration_seconds": 3001, "elapsed_seconds": 3001, "laps": [{"duration_seconds": 3001}]}
    _assert_error("missing_time", workout, detail)


@pytest.mark.parametrize("minutes", [1, 49, 52, 5000])
def test_elapsed_wins_duration_conflicts_without_selecting_moving_pace(minutes):
    result = race_equivalence(_workout(duration=minutes, moving_pace_seconds_per_km=200), None)
    facts = result["source_facts"]
    assert facts["time_seconds"] == 3001
    assert facts["duration_minutes"] == minutes
    assert facts["duration_comparison"] == {
        "state": "differs_from_elapsed", "elapsed_minus_duration_seconds": 3001 - minutes * 60,
    }
    assert "reported_duration_differs_from_elapsed" in result["limitations"]


def test_detail_distance_laps_and_raw_record_values_do_not_override_summary():
    detail = {
        "distance_km": 20, "duration_seconds": 100,
        "laps": [{"distance_meters": 20000, "duration_seconds": 100}],
        "raw": {"distance": 10000, "duration": 50},
    }
    result = race_equivalence(_workout(), detail)
    assert result["equivalence"] == race_equivalence(_workout(), None)["equivalence"]
    assert result["source_facts"]["distance_km"] == 10


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize("value", [
    "", "not-a-timestamp", "2025-02-03", "20250203", 1738540800, True, [], {},
    "2025-02-30T08:00:00Z", "2025-13-03T08:00:00Z", "2025-02-03T25:00:00Z",
    "2025-02-03T08:60:00Z", "2025-02-03T08:00:60Z", "2025-02-03T08:00:00+24:00",
    "2025-02-03T08:00:00+08:60", "2025-02-03X08:00:00Z", "2025-02-03T08:00",
    "2025-02-03T08:00:00.1234567Z", " 2025-02-03T08:00:00Z",
])
def test_present_invalid_timestamp_cannot_silently_use_minute_fallback(field, value):
    _assert_error(f"invalid_{field}", _workout(**{field: value}))


@pytest.mark.parametrize("end", [START, "2025-02-03T07:59:59+08:00", "2025-02-03T00:00:00Z"])
def test_nonpositive_elapsed_is_invalid_even_with_positive_duration(end):
    _assert_error("invalid_elapsed_time", _workout(ended_at=end))


@pytest.mark.parametrize("start,end", [
    ("2025-02-03T00:00:00Z", "2025-02-03T00:50:01+00:00"),
    ("2025-02-03T00:00:00", "2025-02-03T00:50:01"),
    ("2025-02-03 00:00:00", "2025-02-03 00:50:01"),
    ("2025-02-03T00:00:00", "2025-02-03T08:50:01+08:00"),
    ("2025-02-03T01:00:00+01:00", "2025-02-02T19:50:01-05:00"),
    (datetime(2025, 2, 3), datetime(2025, 2, 3, 0, 50, 1, tzinfo=UTC)),  # noqa: DTZ001 -- test the saved naive-UTC contract.
])
def test_valid_offsets_and_documented_naive_utc_have_same_elapsed(start, end):
    result = race_equivalence(_workout(started_at=start, ended_at=end), None)
    assert result["source_facts"]["time_seconds"] == 3001
    assert result["source_facts"]["started_at"] == (start.isoformat() if isinstance(start, datetime) else start)
    assert result["source_facts"]["ended_at"] == (end.isoformat() if isinstance(end, datetime) else end)
    json.dumps(result, allow_nan=False)


def test_naive_utc_interpretation_is_explicit():
    result = race_equivalence(_workout(started_at="2025-02-03T00:00:00", ended_at="2025-02-03T00:50:01"), None)
    assert set(result["source_facts"]["timestamp_timezone_basis"].values()) == {"utc_naive_contract"}
    assert any("naive" in item and "UTC" in item for item in result["assumptions"])


def test_offset_change_uses_utc_elapsed_not_wall_clock_duration():
    result = race_equivalence(_workout(
        started_at="2025-11-02T01:30:00-04:00", ended_at="2025-11-02T01:45:00-05:00", duration=75,
    ), None)
    assert result["source_facts"]["time_seconds"] == 4500


def test_fractional_seconds_are_kept_in_calculation_and_display_rounds_with_carry():
    result = race_equivalence(_workout(
        started_at="2025-02-03T00:00:00.000000Z", ended_at="2025-02-03T00:59:59.500000Z", duration=59,
    ), None, target_km=10)
    assert result["source_facts"]["time_seconds"] == 3599.5
    assert result["source_facts"]["time_resolution_seconds"] == 0.000001
    assert result["equivalence"]["seconds"] == 3599.5
    assert result["equivalence"]["display"] == "1:00:00"
    assert result["equivalence"]["display_resolution_seconds"] == 1


def test_timestamp_resolution_uses_the_less_precise_endpoint():
    result = race_equivalence(_workout(
        started_at="2025-02-03T00:00:00Z", ended_at="2025-02-03T00:50:01.123456Z",
    ), None)
    assert result["source_facts"]["time_resolution_seconds"] == 1
    assert result["source_facts"]["time_seconds"] == 3001.123456


def test_impossible_source_speed_is_input_sanity_not_effort_classification():
    _assert_error("implausible_distance_time", _workout(
        distance_km=10000, started_at=None, ended_at=None, duration=50,
    ))
    _assert_error("implausible_distance_time", _workout(ended_at="2025-02-03T08:00:01+08:00"))
    result = race_equivalence(_workout(
        distance_km=3, started_at=None, ended_at=None, duration=3,
    ), None, target_km=3)
    assert result["method"]["domain"]["max_source_speed_kmh"] == 60
    assert result["qualification"]["maximal_effort_verified"] is False


def test_slow_elapsed_duration_is_not_mistaken_for_a_unit_conversion():
    result = race_equivalence(_workout(started_at=None, ended_at=None, duration=6000), None)
    assert result["source_facts"]["time_seconds"] == 360000


def test_arithmetic_overflow_is_a_stable_validation_error():
    _assert_error("invalid_equivalence", _workout(
        started_at=None, ended_at=None, duration=2 * 10**306,
    ))


@pytest.mark.parametrize("detail", [None, {}, {"pauses": None}])
def test_absent_pause_information_stays_unknown(detail):
    result = race_equivalence(_workout(), detail)
    assert result["source_facts"]["pauses"] == {
        "state": "unknown", "count": None, "records": None,
        "subtracted": False, "time_handling": "elapsed_includes_any_pauses_between_endpoints",
    }


def test_empty_pause_list_means_none_recorded_not_proof_of_uninterrupted_running():
    result = race_equivalence(_workout(), {"pauses": []})
    pauses = result["source_facts"]["pauses"]
    assert pauses["state"] == "none_recorded"
    assert pauses["count"] == 0
    assert pauses["records"] == []
    assert pauses["subtracted"] is False


def test_pause_records_are_preserved_without_subtraction_or_overlap_deduction():
    records = [
        {"started_at": "2025-02-03T08:10:00+08:00", "duration_seconds": 120},
        {"started_at": "2025-02-03T08:11:00+08:00", "duration_seconds": 90},
        {"started_at": "2025-02-03T08:25:00+08:00", "duration_seconds": 0},
    ]
    result = race_equivalence(_workout(), {"pauses": records}, target_km=10)
    assert result["source_facts"]["pauses"]["records"] == records
    assert result["source_facts"]["pauses"]["state"] == "recorded"
    assert result["source_facts"]["pauses"]["count"] == 3
    assert result["source_facts"]["pauses"]["subtracted"] is False
    assert result["equivalence"]["seconds"] == 3001
    assert "recorded_pauses_not_subtracted" in result["limitations"]


@pytest.mark.parametrize("value", [
    "pause", {}, [None], [{}], [{"started_at": START, "duration_seconds": -1}],
    [{"started_at": START, "duration_seconds": True}],
    [{"started_at": START, "duration_seconds": 10.5}],
    [{"started_at": "bad", "duration_seconds": 10}],
])
def test_invalid_normalized_pause_records_have_stable_error(value):
    _assert_error("invalid_pauses", detail={"pauses": value})


def test_unknown_optional_source_metadata_remains_unknown():
    result = race_equivalence({
        "type": "running", "distance_km": 10, "duration": 50,
    }, None)
    for key in ("source", "workout_id", "name", "sport_mode_label", "started_at", "ended_at", "fetched_at"):
        assert result["source_facts"][key] is None
    assert result["source_facts"]["timestamp_timezone_basis"] == {"started_at": None, "ended_at": None}


def test_fetch_metadata_is_preserved_without_inventing_observation_times():
    result = race_equivalence(_workout(fetched_at="2025-02-04T00:00:00Z"), None)
    assert result["source_facts"]["fetched_at"] == "2025-02-04T00:00:00Z"
    assert "generated_at" not in result


def test_function_is_deterministic_does_not_mutate_inputs_or_share_pause_records():
    workout = _workout(session_rpe={"value": 3})
    detail = {"pauses": [{"started_at": START, "duration_seconds": 10}], "samples": []}
    original = deepcopy((workout, detail))
    first = race_equivalence(workout, detail, effort_basis="user_reported_maximal")
    second = race_equivalence(workout, detail, effort_basis="user_reported_maximal")
    assert first == second
    assert (workout, detail) == original
    first["source_facts"]["pauses"]["records"][0]["duration_seconds"] = 999
    assert (workout, detail) == original
    assert math.isfinite(second["equivalence"]["seconds"])
