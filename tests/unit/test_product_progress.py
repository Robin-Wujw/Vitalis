"""Pure validation and progress contracts for Phase 3 product tracking."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from vitalis.application.product_tracking import (
    GoalInput,
    GoalPatch,
    ProductFeedbackInput,
    ProductTrackingValidationError,
    calculate_progress,
)


DAY = date(2026, 9, 20)


def _goal(**overrides):
    payload = {
        "confirmed": True,
        "goal_type": "sleep_duration",
        "target_value": 450,
        "target_unit": "min",
        "target_date": date(2026, 10, 20),
        "window_days": 3,
    }
    payload.update(overrides)
    return GoalInput.model_validate(payload)


def _point(day, value, *, source="zepp", scope="device", unit="min", observed_at=None, device="watch"):
    return {
        "day": day,
        "value": value,
        "unit": unit,
        "source": source,
        "source_scope": scope,
        "device_id": device,
        "observed_at": observed_at or datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
    }


def _context(goal=None):
    return {
        "user_id": "progress-owner",
        "day": DAY,
        "goals": [
            {
                "id": "goal-1",
                "goal_type": "sleep_duration",
                "metric_key": "sleep_duration",
                "target_value": 450,
                "target_unit": "min",
                "target_date": date(2026, 10, 20),
                "window_days": 3,
                "aggregation": "mean",
                "comparison": "at_leAST".lower(),
                "source": "user_confirmed",
                **(goal or {}),
            }
        ],
    }


def test_goal_requires_explicit_confirmation_and_valid_target_fields():
    with pytest.raises(ValidationError):
        _goal(confirmed=False)
    with pytest.raises(ValidationError):
        _goal(goal_type="not valid")
    with pytest.raises(ValidationError):
        _goal(target_value=-1)
    with pytest.raises(ValidationError):
        _goal(target_unit=" ")
    with pytest.raises(ValidationError):
        _goal(target_date=None)


def test_goal_training_days_are_sorted_and_can_be_explicitly_cleared():
    body = _goal(
        available_training_days=[7, 2, 5],
        expected_training_preferences_revision="a" * 64,
    )
    assert body.available_training_days == [2, 5, 7]
    cleared = _goal(
        available_training_days=[],
        expected_training_preferences_revision="a" * 64,
    )
    assert cleared.available_training_days == []
    with pytest.raises(ValidationError):
        _goal(available_training_days=[1, 1], expected_training_preferences_revision="a" * 64)
    with pytest.raises(ValidationError):
        _goal(available_training_days=[0], expected_training_preferences_revision="a" * 64)
    with pytest.raises(ValidationError):
        _goal(available_training_days=[1])


def test_present_pain_or_injury_requires_an_explicit_note():
    with pytest.raises(ValidationError):
        _goal(pain_or_injury_status="PRESENT")
    with pytest.raises(ValidationError):
        _goal(pain_or_injury_status="PRESENT", pain_or_injury_notes=" ")
    body = _goal(
        pain_or_injury_status="PRESENT", pain_or_injury_notes="left knee",
        expected_training_preferences_revision="a" * 64,
    )
    assert body.pain_or_injury_status == "PRESENT"
    assert body.pain_or_injury_notes == "left knee"


def test_goal_patch_requires_a_positive_revision_and_one_change():
    with pytest.raises(ValidationError):
        GoalPatch(confirmed=True, expected_revision=0, target_value=450)
    with pytest.raises(ValidationError):
        GoalPatch(confirmed=True, expected_revision=1)
    with pytest.raises(ValidationError):
        GoalPatch(
            confirmed=True,
            expected_revision=1,
            available_training_days=[1],
        )
    patch = GoalPatch(
        confirmed=True,
        expected_revision=1,
        available_training_days=[],
        expected_training_preferences_revision="b" * 64,
    )
    assert patch.available_training_days == []


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "report_usefulness", "usefulness": "useful"},
        {"kind": "data_correction", "correction_field": "sleep", "corrected_value": 420},
        {"kind": "recommendation_completion", "recommendation_id": "rec-1", "completed": True},
        {"kind": "recommendation_outcome", "recommendation_id": "rec-1", "outcome": "beneficial"},
        {"kind": "event_assessment", "health_event_id": "event-1", "false_positive": True},
    ],
)
def test_feedback_requires_explicit_confirmation_and_kind_specific_links(payload):
    payload = {"confirmed": True, **payload}
    if payload["kind"] == "report_usefulness":
        payload["report_run_id"] = "run-1"
    if payload["kind"] == "data_correction":
        payload.update(correction_unit="min", correction_observed_on=DAY)
    payload["notes"] = "synthetic explicit note"
    valid = ProductFeedbackInput.model_validate(payload)
    assert valid.confirmed is True
    assert valid.notes == "synthetic explicit note"
    with pytest.raises(ValidationError):
        ProductFeedbackInput.model_validate({**payload, "confirmed": False})


def test_feedback_rejects_cross_category_content_and_incomplete_corrections():
    with pytest.raises(ValidationError):
        ProductFeedbackInput(
            confirmed=True,
            kind="report_usefulness",
            report_run_id="run-1",
            usefulness="useful",
            completed=True,
        )
    with pytest.raises(ValidationError):
        ProductFeedbackInput(
            confirmed=True,
            kind="data_correction",
            correction_field="sleep",
            corrected_value=420,
            correction_observed_on=DAY,
        )
    with pytest.raises(ValidationError):
        ProductFeedbackInput(
            confirmed=True,
            kind="recommendation_completion",
            recommendation_id="rec-1",
            completed=False,
            workout_source="zepp",
        )


def test_progress_keeps_missing_days_unknown_and_reports_coverage():
    summary = calculate_progress(
        _context(),
        {
            "sleep_duration": [
                _point(DAY - timedelta(days=2), 430),
                _point(DAY - timedelta(days=1), 470),
            ]
        },
    )
    goal = summary.as_dict()["goals"][0]
    assert goal["status"] == "PARTIAL"
    assert goal["value"] == 450
    assert goal["target_met"] is None
    assert goal["coverage"] == {
        "observed_days": 2,
        "expected_days": 3,
        "ratio": pytest.approx(2 / 3),
        "conflict_days": 0,
        "rejected_points": 0,
        "period_start": date(2026, 9, 18),
        "period_end": DAY,
    }

    unknown = calculate_progress(_context(), {"sleep_duration": []}).as_dict()["goals"][0]
    assert unknown["status"] == "UNKNOWN"
    assert unknown["value"] is None
    assert unknown["target_met"] is None


def test_progress_rejects_conflicting_streams_instead_of_picking_one_value():
    points = [
        _point(DAY - timedelta(days=1), 440, source="zepp", device="watch-a"),
        _point(DAY, 460, source="zepp", device="watch-b"),
    ]
    summary = calculate_progress(_context(), {"sleep_duration": points}).as_dict()["goals"][0]
    assert summary["status"] == "CONFLICT"
    assert summary["value"] is None
    assert summary["target_met"] is None
    assert summary["coverage"]["conflict_days"] == 2


def test_progress_applies_as_of_cutoff_and_rejects_invalid_provenance():
    cutoff = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    points = [
        _point(DAY - timedelta(days=2), 430),
        _point(DAY - timedelta(days=1), 440),
        _point(DAY, 460, observed_at=datetime(2026, 9, 20, 13, tzinfo=timezone.utc)),
        _point(DAY, 470, source="", observed_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc)),
        _point(DAY, 470, unit="hours", observed_at=datetime(2026, 9, 20, 10, tzinfo=timezone.utc)),
    ]
    context = {**_context(), "observed_as_of": cutoff}
    summary = calculate_progress(context, {"sleep_duration": points}).as_dict()["goals"][0]
    assert summary["status"] == "PARTIAL"
    assert summary["value"] == 435
    assert summary["coverage"]["rejected_points"] == 3


def test_progress_equal_and_at_most_comparisons_are_explicit():
    equal = calculate_progress(
        _context({"comparison": "equal", "target_value": 450}),
        {
            "sleep_duration": [
                _point(DAY - timedelta(days=2), 450),
                _point(DAY - timedelta(days=1), 450),
                _point(DAY, 450),
            ]
        },
    ).as_dict()["goals"][0]
    assert equal["status"] == "AVAILABLE"
    assert equal["target_met"] is True
    assert equal["progress_ratio"] is None

    at_most = calculate_progress(
        _context({"comparison": "at_most", "target_value": 445}),
        {
            "sleep_duration": [
                _point(DAY - timedelta(days=2), 440),
                _point(DAY - timedelta(days=1), 450),
                _point(DAY, 460),
            ]
        },
    ).as_dict()["goals"][0]
    assert at_most["target_met"] is False
    assert at_most["progress_ratio"] == pytest.approx(445 / ((440 + 450 + 460) / 3))


def test_progress_requires_an_explicit_calendar_day():
    with pytest.raises(ProductTrackingValidationError):
        calculate_progress({"user_id": "owner", "goals": []}, {})
