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

        if is_sqlite:
            indexes = conn.execute(sa_text("PRAGMA index_list(metric_samples)")).fetchall()
            unique_columns: list[list[str]] = []
            for index in indexes:
                if not index[2]:
                    continue
                name = str(index[1]).replace("'", "''")
                unique_columns.append([
                    row[2] for row in conn.execute(sa_text(f"PRAGMA index_info('{name}')"))
                ])
            old_key = ["user_id", "source", "metric", "timestamp"]
            if old_key in unique_columns:
                old_count = conn.execute(sa_text("SELECT COUNT(*) FROM metric_samples")).scalar_one()
                conn.execute(sa_text("ALTER TABLE metric_samples RENAME TO metric_samples_legacy"))
                conn.execute(sa_text("""
                    CREATE TABLE metric_samples (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL,
                        source VARCHAR(32) NOT NULL,
                        metric VARCHAR(64) NOT NULL,
                        timestamp DATETIME NOT NULL,
                        value FLOAT NOT NULL,
                        unit VARCHAR(24) NOT NULL,
                        source_scope VARCHAR(24) NOT NULL,
                        device_id VARCHAR(128) NOT NULL DEFAULT '',
                        CONSTRAINT uq_metric_sample UNIQUE
                            (user_id, source, metric, timestamp, device_id)
                    )
                """))
                conn.execute(sa_text("""
                    INSERT INTO metric_samples
                        (id, user_id, source, metric, timestamp, value, unit, source_scope, device_id)
                    SELECT id, user_id, source, metric, timestamp, value, unit, source_scope,
                           COALESCE(device_id, '')
                    FROM metric_samples_legacy
                """))
                new_count = conn.execute(sa_text("SELECT COUNT(*) FROM metric_samples")).scalar_one()
                if new_count != old_count:
                    raise RuntimeError("metric_samples migration row-count mismatch")
                conn.execute(sa_text("DROP TABLE metric_samples_legacy"))
                conn.execute(sa_text("CREATE INDEX ix_metric_samples_user_id ON metric_samples (user_id)"))
                conn.execute(sa_text("CREATE INDEX ix_metric_samples_metric ON metric_samples (metric)"))
                conn.execute(sa_text("CREATE INDEX ix_metric_samples_timestamp ON metric_samples (timestamp)"))

            dense_indexes = conn.execute(
                sa_text("PRAGMA index_list(dense_data_files)")
            ).fetchall()
            dense_unique_columns: list[list[str]] = []
            for index in dense_indexes:
                if not index[2]:
                    continue
                name = str(index[1]).replace("'", "''")
                dense_unique_columns.append([
                    row[2] for row in conn.execute(sa_text(f"PRAGMA index_info('{name}')"))
                ])
            dense_old_key = ["user_id", "source", "stream", "file_id"]
            if dense_old_key in dense_unique_columns:
                old_count = conn.execute(sa_text("SELECT COUNT(*) FROM dense_data_files")).scalar_one()
                conn.execute(sa_text("ALTER TABLE dense_data_files RENAME TO dense_data_files_legacy"))
                conn.execute(sa_text("""
                    CREATE TABLE dense_data_files (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL,
                        source VARCHAR(32) NOT NULL,
                        stream VARCHAR(64) NOT NULL,
                        file_id VARCHAR(256) NOT NULL,
                        file_type VARCHAR(64) NOT NULL,
                        date DATE,
                        start_utc DATETIME,
                        end_utc DATETIME,
                        source_scope VARCHAR(24) NOT NULL,
                        device_id VARCHAR(128) NOT NULL DEFAULT '',
                        parse_status VARCHAR(24) NOT NULL,
                        sample_count INTEGER NOT NULL,
                        CONSTRAINT uq_dense_data_file UNIQUE
                            (user_id, source, stream, file_id, start_utc, device_id)
                    )
                """))
                conn.execute(sa_text("""
                    INSERT INTO dense_data_files
                        (id, user_id, source, stream, file_id, file_type, date, start_utc,
                         end_utc, source_scope, device_id, parse_status, sample_count)
                    SELECT id, user_id, source, stream, file_id, file_type, date, start_utc,
                           end_utc, source_scope, COALESCE(device_id, ''), parse_status, sample_count
                    FROM dense_data_files_legacy
                """))
                new_count = conn.execute(sa_text("SELECT COUNT(*) FROM dense_data_files")).scalar_one()
                if new_count != old_count:
                    raise RuntimeError("dense_data_files migration row-count mismatch")
                conn.execute(sa_text("DROP TABLE dense_data_files_legacy"))
                for column in ("user_id", "stream", "date", "start_utc", "parse_status"):
                    conn.execute(sa_text(
                        f"CREATE INDEX ix_dense_data_files_{column} ON dense_data_files ({column})"
                    ))
        else:
            constraint_columns = [
                row[0] for row in conn.execute(sa_text("""
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_name = 'metric_samples'
                      AND tc.constraint_name = 'uq_metric_sample'
                    ORDER BY kcu.ordinal_position
                """))
            ]
            if constraint_columns == ["user_id", "source", "metric", "timestamp"]:
                conn.execute(sa_text("UPDATE metric_samples SET device_id = '' WHERE device_id IS NULL"))
                conn.execute(sa_text("ALTER TABLE metric_samples ALTER COLUMN device_id SET DEFAULT ''"))
                conn.execute(sa_text("ALTER TABLE metric_samples ALTER COLUMN device_id SET NOT NULL"))
                conn.execute(sa_text("ALTER TABLE metric_samples DROP CONSTRAINT uq_metric_sample"))
                conn.execute(sa_text("""
                    ALTER TABLE metric_samples ADD CONSTRAINT uq_metric_sample
                    UNIQUE (user_id, source, metric, timestamp, device_id)
                """))

            dense_constraint_columns = [
                row[0] for row in conn.execute(sa_text("""
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_name = 'dense_data_files'
                      AND tc.constraint_name = 'uq_dense_data_file'
                    ORDER BY kcu.ordinal_position
                """))
            ]
            if dense_constraint_columns == ["user_id", "source", "stream", "file_id"]:
                conn.execute(sa_text(
                    "UPDATE dense_data_files SET device_id = '' WHERE device_id IS NULL"
                ))
                conn.execute(sa_text(
                    "ALTER TABLE dense_data_files DROP CONSTRAINT uq_dense_data_file"
                ))
                conn.execute(sa_text("""
                    ALTER TABLE dense_data_files ADD CONSTRAINT uq_dense_data_file
                    UNIQUE (user_id, source, stream, file_id, start_utc, device_id)
                """))


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
