"""Synthetic checks for the registered, observational association contract."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from random import Random

import pytest

from vitalis.intelligence.association import PersonalAssociationEngine
from vitalis.intelligence.contracts import Availability, ConfidenceBand
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


TARGET = date(2026, 9, 30)
AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _point(metric, day, value, unit, *, source="zepp", device=None):
    return SeriesPoint(
        metric=metric,
        value=value,
        unit=unit,
        day=day,
        observed_at=day,
        source=source,
        source_scope="device" if device else "normalized_daily_record",
        device_id=device,
    )


def _history(days=90):
    raw = RawDailyProfile(user_id="synthetic-association", day=TARGET, as_of=AS_OF)
    raw.training_history_coverage = {
        "verified_days": [
            (TARGET - timedelta(days=offset)).isoformat()
            for offset in range(days + 1)
        ],
    }
    random = Random(20260930)
    sleep = {}
    for offset in range(days - 1, -1, -1):
        day = TARGET - timedelta(days=offset)
        sleep[day] = 360 + random.randrange(120)
    raw.series = {
        "sleep_duration": [
            _point("sleep_duration", day, value, "min")
            for day, value in sleep.items()
        ],
        "hrv_rmssd": [
            _point("hrv_rmssd", day + timedelta(days=1), value / 8, "ms", device="band")
            for day, value in sleep.items()
            if day < TARGET
        ],
        "sleep_hrv": [
            _point("sleep_hrv", day, value / 8, "ms")
            for day, value in sleep.items()
        ],
    }
    return raw


def _candidate(profile, predictor="sleep_duration", outcome="hrv_rmssd", window=90):
    return next(
        item for item in profile.associations
        if item.predictor_metric == predictor
        and item.outcome_metric == outcome
        and item.window_days == window
    )


def test_registered_pairing_keeps_calendar_hrv_and_same_night_hrv_distinct():
    profile = PersonalAssociationEngine().build("synthetic-run", _history())
    calendar = _candidate(profile)
    nightly = _candidate(profile, outcome="sleep_hrv")

    assert calendar.predictor_day_semantics == "sleep_day"
    assert calendar.outcome_day_semantics == "calendar_day"
    assert calendar.pairing_rule == "sleep_day_d_to_calendar_day_d_plus_1"
    assert calendar.lag_days == 1
    assert calendar.expected_pair_days == 89
    assert calendar.paired_days == calendar.analyzed_pair_days == 89
    assert calendar.coefficient == 1
    assert nightly.predictor_day_semantics == nightly.outcome_day_semantics == "sleep_day"
    assert nightly.pairing_rule == "same_sleep_day"
    assert nightly.lag_days == 0
    assert nightly.expected_pair_days == nightly.paired_days == 90
    assert nightly.coefficient == 1
    assert calendar.hypothesis_id and calendar.hypothesis_rationale
    assert calendar.period_start == TARGET - timedelta(days=89)
    assert calendar.period_end == TARGET
    assert calendar.as_of == AS_OF
    assert calendar.association_only is True


def test_activity_pairs_with_the_following_sleep_day_and_never_a_future_night():
    raw = _history()
    raw.series = {"steps": [], "sleep_duration": []}
    for offset in range(90):
        activity_day = TARGET - timedelta(days=offset)
        steps = 4000 + (offset * 37 % 101) * 40
        raw.series["steps"].append(_point("steps", activity_day, steps, "steps"))
        if activity_day < TARGET:
            raw.series["sleep_duration"].append(
                _point("sleep_duration", activity_day + timedelta(days=1), steps / 20, "min")
            )

    result = _candidate(
        PersonalAssociationEngine().build("synthetic-activity", raw),
        predictor="steps", outcome="sleep_duration",
    )

    assert result.predictor_day_semantics == "activity_day"
    assert result.outcome_day_semantics == "sleep_day"
    assert result.pairing_rule == "activity_day_d_to_sleep_day_d_plus_1"
    assert result.lag_days == 1
    assert result.expected_pair_days == result.paired_days == 89
    assert result.coefficient == 1


def test_known_overlapping_training_is_excluded_with_both_denominators_retained():
    raw = _history()
    confounded_days = {TARGET - timedelta(days=offset) for offset in range(0, 40, 4)}
    raw.workouts = [
        {"workout_id": f"synthetic-overlap-{index}", "source": "zepp", "local_day": day,
         "data": {"type": "strength", "duration": 30}}
        for index, day in enumerate(sorted(confounded_days))
    ]
    raw.series["hrv_rmssd"] = [
        _point("hrv_rmssd", point.day, 200 - point.value if point.day in confounded_days else point.value,
               "ms", device="band")
        for point in raw.series["hrv_rmssd"]
    ]

    result = _candidate(PersonalAssociationEngine().build("synthetic-confounded", raw))

    assert result.paired_days == 89
    assert result.confounded_pair_days == 10
    assert result.confounded_ratio == pytest.approx(10 / 89, abs=0.0001)
    assert result.analyzed_pair_days == 79
    assert result.analyzed_coverage_ratio == pytest.approx(79 / 89, abs=0.0001)
    assert result.coefficient == 1
    assert result.confounding_policy == "exclude_known_training_overlap"
    assert result.p_value is not None and result.p_value <= 0.01
    assert result.q_value is not None and result.q_value <= 0.05
    assert result.fdr_significant is True


def test_p_and_bh_q_are_reproducible_and_share_one_complete_registered_family():
    from vitalis.intelligence.association_statistics import benjamini_hochberg

    raw = _history()
    first = PersonalAssociationEngine().build("synthetic-first", raw)
    raw.series = {key: list(reversed(points)) for key, points in reversed(raw.series.items())}
    second = PersonalAssociationEngine().build("synthetic-second", raw)

    assert first.family_size == len(first.associations)
    assert first.tested_count == sum(item.p_value is not None for item in first.associations)
    expected = benjamini_hochberg([item.p_value for item in first.associations])
    assert [item.q_value for item in first.associations] == pytest.approx(expected, nan_ok=True)
    assert [(item.id, item.coefficient, item.p_value, item.q_value) for item in first.associations] == [
        (item.id, item.coefficient, item.p_value, item.q_value) for item in second.associations
    ]
    supported = _candidate(first)
    assert supported.p_value_method == "seven_day_block_permutation"
    assert supported.permutation_block_days == 7
    assert supported.permutation_count == 4095
    assert supported.permutation_seed is not None
    assert supported.family_size == first.family_size
    assert supported.q_value >= supported.p_value
    assert any("因果" in item for item in first.limitations)


@pytest.mark.parametrize("values", [[420] * 90, [420, 430, 440] * 5])
def test_sparse_or_constant_inputs_have_no_inferential_statistics(values):
    raw = _history()
    raw.series["sleep_duration"] = [
        _point("sleep_duration", TARGET - timedelta(days=index), value, "min")
        for index, value in enumerate(values)
    ]
    result = _candidate(PersonalAssociationEngine().build("synthetic-gated", raw))

    assert result.status == Availability.INSUFFICIENT_DATA
    assert result.expected_pair_days == 89
    assert result.coefficient is result.p_value is result.q_value is None
    assert result.fdr_significant is False
    assert result.confidence == ConfidenceBand.NONE


def test_incompatible_sources_or_devices_do_not_create_cross_stream_associations():
    raw = _history()
    raw.series["sleep_duration"] = [
        _point("sleep_duration", point.day, point.value, "min", source="other", device="watch-a")
        for point in raw.series["sleep_duration"]
    ]
    raw.series["hrv_rmssd"] = [
        _point("hrv_rmssd", point.day, point.value, "ms", device="watch-b")
        for point in raw.series["hrv_rmssd"]
    ]
    result = _candidate(PersonalAssociationEngine().build("synthetic-multiple-sources", raw))

    assert result.status == Availability.INSUFFICIENT_DATA
    assert result.coefficient is result.p_value is result.q_value is None
    assert "incompatible_provenance" in result.gate_reasons


def test_nonfinite_wrong_unit_and_observations_after_as_of_do_not_enter_pairs():
    raw = _history()
    point = raw.series["hrv_rmssd"][0]
    raw.series["hrv_rmssd"] = raw.series["hrv_rmssd"][3:]
    raw.series["hrv_rmssd"].extend([
        _point("hrv_rmssd", point.day, float("nan"), "ms", device="band"),
        _point("hrv_rmssd", point.day + timedelta(days=1), 10, "bpm", device="band"),
        SeriesPoint(
            metric="hrv_rmssd", day=point.day + timedelta(days=2), value=60, unit="ms",
            observed_at=AS_OF + timedelta(seconds=1), source="zepp", source_scope="device", device_id="band",
        ),
    ])
    result = _candidate(PersonalAssociationEngine().build("synthetic-invalid", raw))

    assert result.paired_days == 86
    assert result.expected_pair_days == 89


def test_unknown_training_coverage_is_reported_without_inventing_rest_days():
    raw = _history()
    raw.training_history_coverage = {}
    result = _candidate(PersonalAssociationEngine().build("synthetic-unknown-training", raw))

    assert result.confounded_pair_days == 0
    assert result.confounding_unknown_pair_days == result.paired_days
    assert result.confidence != ConfidenceBand.HIGH
    assert any("未知" in item for item in result.limitations)


def test_stdlib_statistics_handle_ties_invalid_values_and_known_bh_values():
    from vitalis.intelligence.association_statistics import (
        average_ranks, benjamini_hochberg, block_permutation_p_value, spearman_coefficient,
    )

    assert average_ranks([2, 1, 2, 4]) == [2.5, 1.0, 2.5, 4.0]
    assert spearman_coefficient([1, 2, 2, 4], [4, 2, 2, 1]) == pytest.approx(-1)
    assert spearman_coefficient([1, 1, 1], [1, 2, 3]) is None
    assert spearman_coefficient([1, float("inf")], [1, 2]) is None
    assert benjamini_hochberg([0.01, 0.04, 0.03, None, 0.2]) == pytest.approx(
        [0.05, 1 / 15, 1 / 15, None, 0.25], nan_ok=True,
    )
    days = [TARGET - timedelta(days=89 - index) for index in range(90)]
    xs = [float(index % 13) for index in range(90)]
    ys = [float(index % 13) for index in range(90)]
    p = block_permutation_p_value(xs, ys, days, seed=17)
    assert p is not None and p <= 0.01
    assert p == block_permutation_p_value(xs, ys, days, seed=17)
    assert block_permutation_p_value([1] * 90, ys, days, seed=17) is None
    assert block_permutation_p_value(xs[:10], ys[:10], days[:10], seed=17) is None


def test_unknown_scope_or_missing_device_does_not_enter_association_streams():
    raw = _history()
    raw.series["sleep_duration"] = [
        replace(point, source_scope="unknown")
        for point in raw.series["sleep_duration"]
    ]
    result = _candidate(PersonalAssociationEngine().build("unknown-scope", raw))
    assert result.status == Availability.INSUFFICIENT_DATA
    assert "incompatible_provenance" in result.gate_reasons

    raw = _history()
    raw.series["hrv_rmssd"] = [
        replace(point, device_id=None)
        for point in raw.series["hrv_rmssd"]
    ]
    result = _candidate(PersonalAssociationEngine().build("missing-device", raw))
    assert result.status == Availability.INSUFFICIENT_DATA
    assert "incompatible_provenance" in result.gate_reasons
