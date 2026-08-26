"""连接器插件注册表。

数据源插件化的核心：通过 register_connector 把厂商连接器注册到
名字 -> 类 的映射，业务层只依赖 HealthConnector 抽象，不感知具体厂商。
"""

from .base import HealthConnector

_CONNECTORS: dict[str, type[HealthConnector]] = {}


def register_connector(cls: type[HealthConnector]) -> type[HealthConnector]:
    """类装饰器：注册连接器。"""
    if not issubclass(cls, HealthConnector):
        raise TypeError(f"{cls.__name__} 必须是 HealthConnector 子类")
    _CONNECTORS[cls.source] = cls
    return cls


def get_connector(source: str, **kwargs) -> HealthConnector:
    """按 source 名称实例化一个连接器。"""
    if source not in _CONNECTORS:
        raise KeyError(f"未注册的数据源: {source}。可用: {sorted(_CONNECTORS)}")
    return _CONNECTORS[source](**kwargs)


class ConnectorRegistry:
    """注册表 API，便于第三方扩展。"""

    @staticmethod
    def register(cls: type[HealthConnector]) -> type[HealthConnector]:
        return register_connector(cls)

    @staticmethod
    def available() -> list[str]:
        return sorted(_CONNECTORS)

    @staticmethod
    def get(source: str, **kwargs) -> HealthConnector:
        return get_connector(source, **kwargs)
