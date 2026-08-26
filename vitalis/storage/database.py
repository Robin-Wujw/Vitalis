"""存储层（PostgreSQL + TimescaleDB）。

- 初期使用 PostgreSQL（psycopg 驱动）。
- 未来迁移 TimescaleDB 时只需改动 DDL（启用时序超表），ORM 层不变。
- 开发/测试可切换到 SQLite。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
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
    """建表。导入 models 以确保注册。"""
    from . import models as _  # noqa: F401

    Base.metadata.create_all(bind=_engine)
    _lightweight_migrations()


def _lightweight_migrations() -> None:
    """轻量列迁移：SQLite/PostgreSQL 兼容（开发期新增列用）。"""
    from sqlalchemy import text as sa_text

    is_sqlite = settings.database_url.startswith("sqlite")
    with _engine.begin() as conn:
        # 判断 auth_tokens 是否含 region_host 列
        if is_sqlite:
            cols = [r[1] for r in conn.execute(sa_text("PRAGMA table_info(auth_tokens)"))]
        else:
            cols = [
                r[0]
                for r in conn.execute(sa_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='auth_tokens'"
                ))
            ]
        if cols and "region_host" not in cols:
            conn.execute(sa_text("ALTER TABLE auth_tokens ADD COLUMN region_host VARCHAR(256) DEFAULT ''"))


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
