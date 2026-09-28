from datetime import date, timedelta

from vitalis.application.aggregation import RangeSummaryQuery
from vitalis.domain import ActivityRecord, NormalizedDaily
from vitalis.domain.aggregation import AggregatedBlock, aggregate_block


DAY = date(2026, 8, 28)


def _block(*activities):
    return AggregatedBlock(
        start=DAY, end=DAY + timedelta(days=len(activities) - 1),
        days_total=len(activities),
        raw_days=[NormalizedDaily(user_id="aggregate", date=DAY + timedelta(days=index), activity=value)
                  for index, value in enumerate(activities)],
    )


def test_missing_activity_metrics_do_not_crash_or_turn_into_zero():
    block = _block(ActivityRecord(user_id="aggregate"), None)
    aggregate_block(block)
    assert block.steps_avg is None
    assert block.calories_total is None
    assert block.distance_km_total is None
    assert block.days_with_data == 1


def test_explicit_zero_is_counted_but_legacy_default_zero_is_not():
    block = _block(
        ActivityRecord(user_id="aggregate", steps=100, calories=50, distance_km=1),
        ActivityRecord(user_id="aggregate", steps=0, calories=0, distance_km=0),
        ActivityRecord(user_id="aggregate", steps=0, calories=0, distance_km=0,
                       observed_fields=["steps", "calories", "distance_km"]),
    )
    aggregate_block(block)
    assert block.steps_avg == 50
    assert block.calories_total == 50
    assert block.distance_km_total == 1


def test_missing_sleep_stages_are_not_averaged_as_zero():
    from vitalis.domain import SleepRecord

    block = _block(None, None)
    block.raw_days[0].sleep = SleepRecord(
        user_id="aggregate", sleep_duration=420,
        deep_sleep=60, observed_fields=["deep_sleep"],
    )
    block.raw_days[1].sleep = SleepRecord(
        user_id="aggregate", sleep_duration=480,
    )
    aggregate_block(block)
    assert block.sleep_duration_avg == 450
    assert block.deep_sleep_avg == 60
    assert block.light_sleep_avg is None
    assert block.awake_avg is None


def test_placeholder_dates_are_not_reported_as_data_days():
    block = _block(None, None)
    aggregate_block(block)
    assert block.days_with_data == 0


def test_range_summary_uses_a_reader_port_and_preserves_empty_days():
    class Reader:
        def load_daily_records(self, user_id, start, end):
            return [
                NormalizedDaily(
                    user_id=user_id,
                    date=start,
                    activity=ActivityRecord(user_id=user_id, steps=100),
                ),
                NormalizedDaily(user_id=user_id, date=end),
            ]

    blocks = RangeSummaryQuery(Reader()).range_summary(
        "range-user", DAY, DAY + timedelta(days=7), "7d"
    )

    assert len(blocks) == 2
    assert blocks[0].days_total == 7
    assert blocks[0].days_with_data == 1
    assert blocks[0].steps_avg == 100
    assert blocks[1].days_total == 1
    assert blocks[1].days_with_data == 0
    assert blocks[1].steps_avg is None
