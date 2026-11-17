import pytest

from vitalis.intelligence.report_formatting import minutes_precise_text, value_with_unit


def test_minute_units_are_not_repeated_after_hour_carry():
    assert value_with_unit(426, "min") == "7 小时 6 分钟"
    assert value_with_unit(45, "minutes") == "45 分钟"


@pytest.mark.parametrize("value, expected", [
    (0, "0 分钟"),
    (45, "45 分钟"),
    (47.9, "47.9 分钟"),
    (59.9, "59.9 分钟"),
    (59.99, "1 小时"),
    (60, "1 小时"),
    (60.1, "1 小时 0.1 分钟"),
    (61.5, "1 小时 1.5 分钟"),
    (119.9, "1 小时 59.9 分钟"),
    (120, "2 小时"),
    (120.1, "2 小时 0.1 分钟"),
    (None, None),
    (True, None),
    ("47.9", None),
    (float("nan"), None),
    (float("inf"), None),
])
def test_recorded_fractional_minutes_are_not_truncated(value, expected):
    assert minutes_precise_text(value) == expected
