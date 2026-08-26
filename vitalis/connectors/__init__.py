"""数据连接器层（Data Connector Layer）。

设计：
- `HealthConnector` 是统一抽象基类，所有厂商连接器实现同一接口：
  authenticate() / sync() / fetch()
- `registry` 提供插件化注册：新增厂商只需注册一个类，无需改动其它模块。
- 分析逻辑与该层完全解耦：本层只负责「取原始数据 -> 转成 Vitalis Schema」。
"""
from .base import ConnectorAuth, ConnectorSyncResult, HealthConnector
from .registry import ConnectorRegistry, get_connector, register_connector

# 注册内置连接器插件（新增数据源时在此 import 即自动注册）
from . import zepp  # noqa: F401

__all__ = [
    "ConnectorAuth",
    "ConnectorSyncResult",
    "ConnectorRegistry",
    "HealthConnector",
    "get_connector",
    "register_connector",
]
