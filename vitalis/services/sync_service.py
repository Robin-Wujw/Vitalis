"""同步服务：把连接器拉取的数据写入存储。

负责「数据采集 -> 持久化」的解耦：
- 只依赖 HealthConnector 抽象与 HealthRepository，不感知具体厂商。
- 多用户支持：每个用户独立同步。
"""

from datetime import date, timedelta

from vitalis.connectors import HealthConnector
from vitalis.models import NormalizedDaily
from vitalis.storage import HealthRepository, session_scope
from vitalis.time import local_today


class SyncService:
    """数据同步服务。"""

    def __init__(self, connector: HealthConnector):
        self.connector = connector

    def sync_user(
        self,
        user_id: str,
        name: str = "",
        start: date | None = None,
        end: date | None = None,
    ) -> dict:
        """同步一个用户的历史数据到存储层。

        流程（对应架构文档「用户数据流程」）：
        1. 确保用户记录存在
        2. connector.fetch 拉取并转换为 Vitalis Schema
        3. repository.save_daily 写入各表
        """
        end = end or local_today()
        start = start or (end - timedelta(days=14))

        from vitalis.models import User

        with session_scope() as db:
            HealthRepository(db).upsert_user(user_id, name=name, source=self.connector.source)
        user = User(id=user_id, name=name, source=self.connector.source)

        if (
            self.connector.source == "zepp"
            and not getattr(self.connector, "mock", False)
            and hasattr(self.connector, "sync_with_report")
        ):
            from vitalis.connectors.zepp.fetcher import FetchWindow
            report = self.connector.sync_with_report(
                user, window=FetchWindow.local_dates(start, end),
                days=(end - start).days + 1, trigger="service"
            )
            return {
                "user_id": user_id, "source": self.connector.source,
                "start": start.isoformat(), "end": end.isoformat(),
                "days_synced": (end - start).days + 1 if report.success else 0,
                "attempt_id": report.progress.get("attempt_id") if report.progress else None,
                "attempt_status": report.progress.get("status") if report.progress else None,
                "success": report.success,
            }

        with session_scope() as db:
            repo = HealthRepository(db)
            dailies: list[NormalizedDaily] = self.connector.fetch(user, start, end, repo=repo)
            days = 0
            for d in dailies:
                repo.save_daily(d)
                days += 1

        return {
            "user_id": user_id, "source": self.connector.source,
            "start": start.isoformat(), "end": end.isoformat(), "days_synced": days,
        }

    def preview(self, user_id: str, start: date | None = None, end: date | None = None) -> list[dict]:
        """只拉取转换，不写库（API 预览用）。"""
        from vitalis.models import User

        user = User(id=user_id, name=user_id)
        with session_scope() as db:
            repo = HealthRepository(db)
            end = end or local_today()
            start = start or (end - timedelta(days=7))
            dailies = self.connector.fetch(user, start, end, repo=repo)
        return [d.model_dump(mode="json") for d in dailies]
