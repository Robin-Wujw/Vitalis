"""多级聚合查询服务：支持半年(180d) → 3月(90d) → 1月(30d) → 7d → 1d 维度下钻。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from vitalis.models import ActivityRecord, DailyHealth, SleepRecord, TrainingRecord
from vitalis.storage import HealthRepository, session_scope

Granularity = Literal["180d", "90d", "30d", "7d", "1d"]

_GRANULARITY_DAYS = {
    "180d": 180,
    "90d": 90,
    "30d": 30,
    "7d": 7,
    "1d": 1,
}


@dataclass
class AggregatedBlock:
    """一个时间块的聚合结果。"""

    start: date
    end: date
    days_with_data: int = 0
    days_total: int = 0

    # sleep aggregates
    sleep_duration_avg: float | None = None
    deep_sleep_avg: float | None = None
    rem_sleep_avg: float | None = None
    light_sleep_avg: float | None = None
    awake_avg: float | None = None
    sleep_score_avg: float | None = None

    # activity aggregates
    steps_avg: float | None = None
    calories_total: int | None = None
    distance_km_total: float | None = None
    resting_hr_avg: float | None = None

    # training aggregates
    workout_count_total: int = 0
    training_duration_total: int = 0
    training_load_total: int = 0

    # health aggregates
    hrv_avg: float | None = None
    recovery_score_avg: float | None = None
    overall_score_avg: float | None = None

    raw_days: list[DailyHealth] = field(default_factory=list, repr=False)


class AggregationService:
    """按粒度聚合用户历史健康数据。"""

    def range_summary(
        self,
        user_id: str,
        start: date,
        end: date,
        granularity: Granularity = "1d",
    ) -> list[AggregatedBlock]:
        """获取指定时间范围、指定粒度的聚合结果。"""
        if start > end:
            start, end = end, start
        days = _GRANULARITY_DAYS[granularity]

        with session_scope() as db:
            repo = HealthRepository(db)
            dailies = self._load_dailies(repo, user_id, start, end)

        # 按块分组
        blocks: list[AggregatedBlock] = []
        cursor = start
        while cursor <= end:
            block_end = min(cursor + timedelta(days=days - 1), end)
            block = AggregatedBlock(start=cursor, end=block_end, days_total=(block_end - cursor).days + 1)
            blocks.append(block)
            cursor = block_end + timedelta(days=1)

        # 将每一天的数据分配到对应块
        daily_map = {d.date: d for d in dailies}
        for block in blocks:
            day = block.start
            while day <= block.end:
                if day in daily_map:
                    block.raw_days.append(daily_map[day])
                day += timedelta(days=1)
            self._aggregate_block(block)

        return blocks

    def _load_dailies(self, repo: HealthRepository, user_id: str, start: date, end: date) -> list[DailyHealth]:
        """从存储加载每日快照（重建 DailyHealth）。"""
        from vitalis.models import ActivityRecord, SleepRecord, TrainingRecord

        sleeps = {date.fromisoformat(r["date"]): r for r in repo.sleep_range(user_id, start, end)}
        acts = {date.fromisoformat(r["date"]): r for r in repo.activity_range(user_id, start, end)}
        trains = {date.fromisoformat(r["date"]): r for r in repo.training_range(user_id, start, end)}
        hds = {hd.date: hd for hd in repo.health_daily_range(user_id, start, end)}

        out: list[DailyHealth] = []
        day = start
        while day <= end:
            s = SleepRecord.model_validate(sleeps[day]) if day in sleeps else None
            a = ActivityRecord.model_validate(acts[day]) if day in acts else None
            t = TrainingRecord.model_validate(trains[day]) if day in trains else None
            hd = hds.get(day)
            daily = DailyHealth(user_id=user_id, date=day, sleep=s, activity=a, training=t)
            if hd:
                daily.hrv = hd.hrv
                daily.recovery_score = hd.recovery_score
                daily.recovery_level = hd.recovery_level
                daily.stress_level = hd.stress_level
                daily.overall_score = hd.overall_score
            out.append(daily)
            day += timedelta(days=1)
        return out

    @staticmethod
    def _aggregate_block(block: AggregatedBlock) -> None:
        days = block.raw_days
        block.days_with_data = len(days)
        if not days:
            return

        # sleep
        sleep_days = [d.sleep for d in days if d.sleep]
        if sleep_days:
            n = len(sleep_days)
            block.sleep_duration_avg = round(sum(s.sleep_duration for s in sleep_days) / n, 1)
            block.deep_sleep_avg = round(sum(s.deep_sleep for s in sleep_days) / n, 1)
            block.rem_sleep_avg = round(sum(s.rem_sleep for s in sleep_days) / n, 1)
            block.light_sleep_avg = round(sum(s.light_sleep for s in sleep_days) / n, 1)
            block.awake_avg = round(sum(s.awake for s in sleep_days) / n, 1)
            scores = [s.sleep_score for s in sleep_days if s.sleep_score is not None]
            if scores:
                block.sleep_score_avg = round(sum(scores) / len(scores), 1)

        # activity
        act_days = [d.activity for d in days if d.activity]
        if act_days:
            n = len(act_days)
            block.steps_avg = round(sum(a.steps for a in act_days) / n, 0)
            block.calories_total = sum(a.calories for a in act_days)
            block.distance_km_total = round(sum(a.distance_km for a in act_days), 2)
            rhrs = [a.resting_hr for a in act_days if a.resting_hr]
            if rhrs:
                block.resting_hr_avg = round(sum(rhrs) / len(rhrs), 1)

        # training
        train_days = [d.training for d in days if d.training]
        if train_days:
            block.workout_count_total = sum(t.workout_count for t in train_days)
            block.training_duration_total = sum(t.total_duration for t in train_days)
            block.training_load_total = sum(t.total_load for t in train_days)

        # health scores
        hrvs = [d.hrv for d in days if d.hrv is not None]
        if hrvs:
            block.hrv_avg = round(sum(hrvs) / len(hrvs), 1)
        recs = [d.recovery_score for d in days if d.recovery_score]
        if recs:
            block.recovery_score_avg = round(sum(recs) / len(recs), 1)
        ovs = [d.overall_score for d in days if d.overall_score]
        if ovs:
            block.overall_score_avg = round(sum(ovs) / len(ovs), 1)
