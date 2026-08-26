"""Statistical Engine —— 趋势统计层。

计算（不解释、不总结）：
- HRV 7 天趋势（相对均值百分比）
- 30 天训练负荷（滚动窗口总和/均值）
- 睡眠时长窗口均值
结果以结构化数据（dict）交给 RuleEngine / AIEngine。

纯函数、确定性，便于单元测试。
"""

import statistics
from datetime import date, timedelta
from typing import Any

from vitalis.models import ActivityRecord, DailyHealth, SleepRecord, TrainingRecord

HRV_WINDOW = 7
LOAD_WINDOW = 30


class StatisticalEngine:
    """趋势统计引擎。输入历史 DailyHealth，输出结构化统计特征。"""

    def compute(self, dailies: list[DailyHealth], target: DailyHealth | None = None) -> dict[str, Any]:
        """把历史数据折叠成统计特征。

        dailies: 按日期升序的历史日快照（已含 sleep/activity/training）。
        target: 当日快照；若为 None 则取最后一天。
        """
        ordered = sorted(dailies, key=lambda d: d.date)
        if not ordered:
            return {"hrv_trend_pct": None, "load_30d": 0, "sleep_avg_7d": None}

        target = target or ordered[-1]

        hrv_trend = self._hrv_trend(ordered, target)
        load_30d = self._load_30d(ordered, target)
        sleep_avg = self._sleep_avg(ordered, target)

        return {
            "hrv_trend_pct": hrv_trend,
            "load_30d": load_30d,
            "sleep_avg_7d_min": sleep_avg,
            "window_days": len(ordered),
        }

    # ---- HRV 7 天趋势 ----
    @staticmethod
    def _hrv_trend(dailies: list[DailyHealth], target: DailyHealth) -> float | None:
        """HRV 当前值 vs 过去 7 天均值的偏差百分比。

        优先用显式 hrv 字段；否则用静息心率作为反向代理
        （resting_hr 越低恢复越好，故趋势方向取反）。
        """
        vals: list[float] = []
        uses_resting = False
        for d in dailies:
            if d.date >= target.date:
                continue
            if d.hrv:
                vals.append(float(d.hrv))
            elif d.activity and d.activity.resting_hr:
                vals.append(float(d.activity.resting_hr))
                uses_resting = True
            elif d.activity:
                pass
        if len(vals) < 3:  # 样本太少不计算趋势
            return None

        baseline = statistics.fmean(vals)
        if baseline <= 0:
            return None
        cur = target.hrv or (target.activity.resting_hr if target.activity else 0)
        if not cur:
            return None
        if uses_resting and not target.hrv:
            # resting_hr 越低越好 -> 用反向偏差
            delta = (baseline - float(cur)) / baseline * 100.0
        else:
            delta = (float(cur) - baseline) / baseline * 100.0
        return round(delta, 2)

    # ---- 30 天训练负荷 ----
    @staticmethod
    def _load_30d(dailies: list[DailyHealth], target: DailyHealth) -> int:
        cutoff = target.date - timedelta(days=LOAD_WINDOW)
        total = 0
        for d in dailies:
            if d.date > target.date or d.date < cutoff:
                continue
            if d.training:
                total += d.training.total_load or 0
        return total

    # ---- 7 天平均睡眠 ----
    @staticmethod
    def _sleep_avg(dailies: list[DailyHealth], target: DailyHealth) -> float | None:
        cutoff = target.date - timedelta(days=HRV_WINDOW)
        mins = [
            d.sleep.sleep_duration
            for d in dailies
            if d.sleep and cutoff <= d.date < target.date and d.sleep.sleep_duration
        ]
        return round(statistics.fmean(mins), 1) if len(mins) >= 3 else None