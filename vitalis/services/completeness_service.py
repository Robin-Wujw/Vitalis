"""数据完整性检查服务：判断今日健康数据是否足够做分析推送。

核心逻辑：
  1. 检查昨晚睡眠是否已结束（有 wake_time）
  2. 检查今日活动是否有基础数据
  3. 如果不完整，建议下一次重试时间
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from vitalis.models import DailyHealth
from vitalis.storage import HealthRepository


@dataclass
class CompletenessReport:
    """数据完整性报告。"""

    complete: bool
    missing: list[str]  # sleep_wake / activity / training
    retry_at: datetime | None  # 建议下一次重试时间
    reason: str

    def to_dict(self) -> dict:
        return {
            "complete": self.complete,
            "missing": self.missing,
            "retry_at": self.retry_at.isoformat() if self.retry_at else None,
            "reason": self.reason,
        }


class CompletenessService:
    """检查用户当日健康数据的完整性。"""

    # 假设用户最晚 11:00 起床（超过这个时间还没醒，视为异常但不再等）
    LATEST_WAKE_TIME = time(11, 0)
    # 下午重试时间
    AFTERNOON_RETRY = time(14, 0)

    def check_today(self, repo: HealthRepository, user_id: str, target_day: date | None = None) -> CompletenessReport:
        """检查指定日期的数据是否完整（默认今天）。"""
        target = target_day or date.today()
        missing: list[str] = []

        # 1. 检查睡眠数据
        sleep_raw = repo.get_sleep(user_id, target)
        if sleep_raw:
            # 判断睡眠是否已结束（有 wake_time 或睡眠时长达标）
            wake = sleep_raw.get("wake_time")
            duration = sleep_raw.get("sleep_duration", 0)
            if wake is None and duration < 360:  # 不足 6 小时可能还没醒
                missing.append("sleep_wake")
        else:
            # 检查昨天（昨晚入睡的数据可能落在昨天）
            yesterday = target - timedelta(days=1)
            sleep_yest = repo.get_sleep(user_id, yesterday)
            if sleep_yest is None:
                missing.append("sleep")

        # 2. 检查活动数据
        activity_raw = repo.activity_range(user_id, target, target)
        if not activity_raw:
            missing.append("activity")

        # 3. 判断是否应该继续等
        now = datetime.now()
        latest_wake = datetime.combine(target, self.LATEST_WAKE_TIME)
        afternoon_retry = datetime.combine(target, self.AFTERNOON_RETRY)

        if missing:
            if now < latest_wake and "sleep_wake" in missing:
                # 还没过最晚起床时间，再等
                return CompletenessReport(
                    complete=False,
                    missing=missing,
                    retry_at=latest_wake,
                    reason=f"用户可能尚未起床（睡眠未结束），建议 {self.LATEST_WAKE_TIME.strftime('%H:%M')} 后重试",
                )
            elif now < afternoon_retry:
                # 过了起床时间但数据仍缺，下午再试
                return CompletenessReport(
                    complete=False,
                    missing=missing,
                    retry_at=afternoon_retry,
                    reason="数据仍不完整，下午重试",
                )
            else:
                # 下午了，不再等了，用现有数据做分析
                return CompletenessReport(
                    complete=True,
                    missing=missing,
                    retry_at=None,
                    reason="数据不完整但已超过等待阈值，使用现有数据",
                )

        return CompletenessReport(
            complete=True,
            missing=[],
            retry_at=None,
            reason="数据完整",
        )
