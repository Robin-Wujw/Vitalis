from datetime import date, timedelta

from vitalis.models import ActivityRecord, NormalizedDaily
from vitalis.services.aggregation_service import AggregatedBlock, AggregationService


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
    AggregationService._aggregate_block(block)
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
    AggregationService._aggregate_block(block)
    assert block.steps_avg == 50
    assert block.calories_total == 50
    assert block.distance_km_total == 1


def test_placeholder_dates_are_not_reported_as_data_days():
    block = _block(None, None)
    AggregationService._aggregate_block(block)
    assert block.days_with_data == 0
