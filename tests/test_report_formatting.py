from vitalis.intelligence.report_formatting import value_with_unit


def test_minute_units_are_not_repeated_after_hour_carry():
    assert value_with_unit(426, "min") == "7 小时 6 分钟"
    assert value_with_unit(45, "minutes") == "45 分钟"
