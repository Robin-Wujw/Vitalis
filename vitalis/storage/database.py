"""存储层（PostgreSQL + TimescaleDB）。

- 初期使用 PostgreSQL（psycopg 驱动）。
- 未来迁移 TimescaleDB 时只需改动 DDL（启用时序超表），ORM 层不变。
- 开发/测试可切换到 SQLite。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from vitalis.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# SQLite 内存库需要 StaticPool：所有连接共享同一份内存 DB（否则多连接各持一份空库）
_memory_sqlite = settings.database_url in ("sqlite://", "sqlite:///:memory:")
_engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    poolclass=StaticPool if _memory_sqlite else None,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def get_engine():
    return _engine


def init_db() -> None:
    """Create the current schema and require resolved source identities."""
    from . import models as _  # noqa: F401
    from .identity_migration import (
        SourceIdentityMigrationRequired,
        audit_source_identities,
    )

    Base.metadata.create_all(bind=_engine)
    # `create_all` does not add indexes declared after an existing table was
    # created, so apply current indexes explicitly for existing databases.
    metric_samples = Base.metadata.tables["metric_samples"]
    next(
        index
        for index in metric_samples.indexes
        if index.name == "ix_metric_samples_user_metric_timestamp"
    ).create(bind=_engine, checkfirst=True)
    try:
        for table_name in ("users", "auth_tokens"):
            for index in Base.metadata.tables[table_name].indexes:
                if index.name and index.name.startswith("uq_"):
                    index.create(bind=_engine, checkfirst=True)
    except SQLAlchemyError as exc:
        raise SourceIdentityMigrationRequired(
            "Zepp 身份映射存在重复或尚未迁移；先运行 "
            "`python -m vitalis.storage.identity_migration audit`，"
            "解决冲突后运行 `python -m vitalis.storage.identity_migration migrate --apply`"
        ) from exc
    with Session(_engine) as db:
        audit = audit_source_identities(db)
    if not audit.clean:
        raise SourceIdentityMigrationRequired(
            "Zepp 身份映射跨表不一致或缺少厂商用户 id；先运行 "
            "`python -m vitalis.storage.identity_migration audit` 并显式解析"
        )


def get_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """上下文管理器用法：with session_scope() as db: ..."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
