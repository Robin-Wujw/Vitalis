"""Actual exercise repetitions are numeric; planned repetitions remain text."""

import pytest
from pydantic import ValidationError
from sqlalchemy import Integer

from vitalis.intelligence.contracts import (
    StrengthExerciseInput,
    StrengthExerciseRecord,
    TrainingStep,
)
from vitalis.intelligence.strength import normalize_exercise
from vitalis.adapters.persistence.models import StrengthExercise


def test_actual_repetitions_are_integer_or_missing_and_plans_remain_text():
    exercise = StrengthExerciseInput(exercise_name="卧推", repetitions=8)
    record = normalize_exercise("u", "w", 1, exercise)

    assert record.repetitions == 8
    assert record.model_dump(mode="json")["repetitions"] == 8
    assert StrengthExerciseInput(exercise_name="卧推").repetitions is None
    assert normalize_exercise(
        "u", "w", 1, StrengthExerciseInput(exercise_name="卧推")
    ).repetitions is None
    assert isinstance(StrengthExercise.__table__.c.repetitions.type, Integer)
    assert TrainingStep(order=1, name="卧推", repetitions="6–12 次").repetitions == "6–12 次"


@pytest.mark.parametrize("value", ["8", "8 次", "6–8 次", 8.0, True, 0, -1])
def test_actual_repetitions_reject_text_and_invalid_counts(value):
    with pytest.raises(ValidationError):
        StrengthExerciseInput(exercise_name="卧推", repetitions=value)

    valid = normalize_exercise("u", "w", 1, StrengthExerciseInput(
        exercise_name="卧推", repetitions=8,
    ))
    with pytest.raises(ValidationError):
        StrengthExerciseRecord.model_validate({
            **valid.model_dump(mode="python"), "repetitions": value,
        })
