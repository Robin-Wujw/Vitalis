"""Application use case for user-scoped normalized range summaries."""

from __future__ import annotations

from datetime import date, timedelta

from vitalis.domain import NormalizedDaily
from vitalis.domain.aggregation import (
    AggregatedBlock,
    GRANULARITY_DAYS,
    Granularity,
    aggregate_block,
)

from .ports import RangeSummaryReader


class RangeSummaryQuery:
    """Build aggregate blocks from a repository-agnostic daily-record port."""

    def __init__(self, reader: RangeSummaryReader) -> None:
        self._reader = reader

    def range_summary(
        self,
        user_id: str,
        start: date,
        end: date,
        granularity: Granularity = "1d",
    ) -> list[AggregatedBlock]:
        """Return summaries for the requested inclusive date range."""
        if start > end:
            start, end = end, start
        days = GRANULARITY_DAYS[granularity]
        dailies = self._reader.load_daily_records(user_id, start, end)

        blocks: list[AggregatedBlock] = []
        cursor = start
        while cursor <= end:
            block_end = min(cursor + timedelta(days=days - 1), end)
            blocks.append(
                AggregatedBlock(
                    start=cursor,
                    end=block_end,
                    days_total=(block_end - cursor).days + 1,
                )
            )
            cursor = block_end + timedelta(days=1)

        daily_map: dict[date, NormalizedDaily] = {
            daily.date: daily for daily in dailies
        }
        for block in blocks:
            day = block.start
            while day <= block.end:
                if day in daily_map:
                    block.raw_days.append(daily_map[day])
                day += timedelta(days=1)
            aggregate_block(block)

        return blocks
