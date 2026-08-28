"""健康连接器统一抽象基类。

所有数据源（Zepp/Garmin/Apple/Huawei...）实现此接口。
返回的类型全部是 Vitalis Schema（vitalis.models），而非厂商格式。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from vitalis.models import (
    ActivityRecord,
    NormalizedDaily,
    Device,
    SleepRecord,
    TrainingRecord,
    User,
    Workout,
)


@dataclass
class ConnectorAuth:
    """连接器鉴权信息。具体字段由各厂商连接器自定义。"""

    token: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ConnectorSyncResult:
    """一次数据同步的汇总结果。"""

    user_id: str
    source: str
    days_synced: int
    sleep_count: int
    workout_count: int
    activity_count: int


class HealthConnector(ABC):
    """厂商数据连接器抽象基类。"""

    #: 数据源标识，如 "zepp"。registry 据此注册。
    source: str = "base"

    def __init__(self, auth: ConnectorAuth | None = None) -> None:
        self.auth = auth or ConnectorAuth()

    @abstractmethod
    def authenticate(self) -> ConnectorAuth:
        """完成鉴权（扫码/应用 token），返回有效鉴权信息。"""

    @abstractmethod
    def sync(self, user: User, start: date | None = None, end: date | None = None) -> ConnectorSyncResult:
        """同步历史数据：拉取原始数据 -> 转成 Vitalis Schema -> 写入存储。

        start/end 缺省时同步最近 N 天。
        """

    @abstractmethod
    def fetch(
        self, user: User, start: date | None = None, end: date | None = None, repo=None
    ) -> list[NormalizedDaily]:
        """只拉取并转换数据（不写存储），返回统一 NormalizedDaily 列表。

        供测试与 API「预览」使用。repo 为可选的 data repository，
        连接器可据此读取/保存厂商 token（见 ZeppConnector）。
        """

    # ---- 原始数据获取（各厂商实现） ----
    def _fetch_raw_sleep(self, user: User, day: date) -> list[dict]:
        raise NotImplementedError

    def _fetch_raw_activity(self, user: User, day: date) -> list[dict]:
        raise NotImplementedError

    def _fetch_raw_training(self, user: User, day: date) -> list[dict]:
        raise NotImplementedError

    # ---- 转换辅助（由 parser 实现，见 connectors/<vendor>/parser.py） ----
    def parse_sleep(self, raw: dict) -> SleepRecord:  # pragma: no cover
        raise NotImplementedError

    def parse_activity(self, raw: dict) -> ActivityRecord:  # pragma: no cover
        raise NotImplementedError

    def parse_training(self, raw: dict) -> TrainingRecord:  # pragma: no cover
        raise NotImplementedError

    def parse_workout(self, raw: dict) -> Workout:  # pragma: no cover
        raise NotImplementedError

    def parse_device(self, raw: dict) -> Device:  # pragma: no cover
        raise NotImplementedError


class NoopConnector(HealthConnector):
    """占位连接器，用于演示不依赖具体厂商的流程。"""

    source = "noop"

    def authenticate(self) -> ConnectorAuth:
        return self.auth

    def sync(self, user, start=None, end=None, repo=None):
        # 什么都不做
        return ConnectorSyncResult(user.id, self.source, 0, 0, 0, 0)

    def fetch(self, user, start=None, end=None, repo=None):
        return []
