"""Current-schema storage initialization and transaction helpers."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Integer, String, UniqueConstraint, create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from vitalis.config import settings

SCHEMA_REVISION = "2026-10-durable-pushplus-delivery"


class SchemaMismatch(RuntimeError):
    """The database is not a fresh or current Vitalis database."""


class Base(DeclarativeBase):
    """Base class shared by the current ORM models."""


class SchemaVersion(Base):
    __tablename__ = "vitalis_schema"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)


def _create_engine(database_url: str) -> Engine:
    memory_sqlite = database_url in ("sqlite://", "sqlite:///:memory:")
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 30.0} if database_url.startswith("sqlite") else {},
        poolclass=StaticPool if memory_sqlite else None,
        pool_pre_ping=True,
    )
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    return engine


_engine = _create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


class UnitOfWork:
    """Explicit application transaction boundary.

    Entering a unit of work never commits implicitly.  Callers must invoke
    ``commit()``; otherwise the context rolls back on normal exit as well as on
    exceptions.  ``session_scope`` remains available for existing infrastructure
    until each consumer is migrated to this contract.
    """

    def __init__(self, session_factory=None):
        if session_factory is None:
            self._session_factory = SessionLocal
        elif isinstance(session_factory, Engine):
            self._session_factory = sessionmaker(
                bind=session_factory, autoflush=False, expire_on_commit=False
            )
        elif callable(session_factory):
            self._session_factory = session_factory
        else:
            raise TypeError("UnitOfWork requires a SQLAlchemy engine or session factory")
        self.db: Session | None = None
        self.repository = None
        self._committed = False

    @property
    def session(self) -> Session:
        if self.db is None:
            raise RuntimeError("UnitOfWork is not active")
        return self.db

    @property
    def transaction(self) -> Session:
        """Expose the active transaction object through a neutral UoW port."""
        return self.session

    @property
    def repo(self):
        if self.repository is None:
            raise RuntimeError("UnitOfWork is not active")
        return self.repository

    def __enter__(self) -> "UnitOfWork":
        if self.db is not None:
            raise RuntimeError("UnitOfWork cannot be re-entered")
        self.db = self._session_factory()
        # Avoid a module import cycle: repositories import Base/models here.
        from .repositories import HealthRepository

        self.repository = HealthRepository(self.db)
        self._committed = False
        return self

    def commit(self) -> None:
        self.session.commit()
        self._committed = True

    def rollback(self) -> None:
        if self.db is not None:
            self.db.rollback()
        self._committed = False

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is not None or not self._committed:
                self.rollback()
        finally:
            if self.db is not None:
                self.db.close()
            self.db = None
            self.repository = None
        return False


def get_engine() -> Engine:
    return _engine


def _strip_redundant_predicate_parentheses(expression: str) -> str:
    def strip_outer(value: str) -> str:
        while value.startswith("(") and value.endswith(")"):
            depth = 0
            closes_at_end = True
            for position, character in enumerate(value):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0 and position != len(value) - 1:
                        closes_at_end = False
                        break
            if closes_at_end and depth == 0:
                value = value[1:-1].strip()
            else:
                break
        return value

    previous = None
    while expression != previous:
        previous = expression
        expression = strip_outer(expression)
        expression = re.sub(r"\(([A-Za-z_][A-Za-z_0-9]*)\)", r"\1", expression)
        expression = re.sub(
            r"\(((?:[^()]|\([^()]*\))*?"
            r"(?:=|<>|!=|<=|>=|<|>|LIKE|IS)\s+(?:[^()]|\([^()]*\))*?)\)",
            lambda match: (
                match.group(0) if re.search(r"\b(?:AND|OR)\b", match.group(1), re.IGNORECASE)
                else match.group(1)
            ),
            expression,
            flags=re.IGNORECASE,
        )
    return expression


def _index_predicate(where: object, dialect: str) -> tuple[str, ...] | None:
    if where is None:
        return None
    expression = str(where)
    if dialect == "postgresql":
        # PostgreSQL deparses VARCHAR comparisons with casts and IN as = ANY(ARRAY[...]).
        expression = re.sub(
            r"::(?:text|character varying)(?:\(\d+\))?(?:\[\])?", "", expression,
            flags=re.IGNORECASE,
        )
        expression = _strip_redundant_predicate_parentheses(expression)
        expression = re.sub(
            r"=\s*ANY\s*\(+\s*ARRAY\[([^\]]+)\]\s*\)+", r" IN (\1)", expression,
            flags=re.IGNORECASE,
        )
        expression = expression.replace("~~", "LIKE")
        expression = _strip_redundant_predicate_parentheses(expression)
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z_0-9]*|!=|<>|<=|>=|[^\s]",
        expression,
    )
    return tuple(token if token.startswith(("'", '"')) else token.lower() for token in tokens)


def check_schema(engine: Engine | None = None) -> None:
    """Validate a current database without DDL or an implicit upgrade."""
    from . import models as _  # noqa: F401

    target = engine or _engine
    database = target.url.database
    if (target.dialect.name == "sqlite" and database and database != ":memory:"
        and not Path(database).is_file()):
        raise SchemaMismatch("数据库尚未初始化；请先运行 vitalis db init")
    inspector = inspect(target)
    found = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    if found != expected:
        raise SchemaMismatch(
            "数据库不是当前 Vitalis schema；请使用新的空数据库，现有数据库未被修改"
        )
    with Session(target) as db:
        revision = db.get(SchemaVersion, 1)
    if revision is None or revision.revision != SCHEMA_REVISION:
        raise SchemaMismatch(
            "数据库 schema 版本不匹配；请使用新的空数据库，现有数据库未被修改"
        )
    for table in Base.metadata.tables.values():
        actual = {column["name"] for column in inspector.get_columns(table.name)}
        required = {column.name for column in table.columns}
        dialect = target.dialect.name
        indexes = {
            item["name"]: (
                tuple(item["column_names"]),
                item.get("unique"),
                _index_predicate(
                    item.get("dialect_options", {}).get(f"{dialect}_where"), dialect,
                ),
            )
            for item in inspector.get_indexes(table.name)
        }
        required_indexes = {
            index.name: (
                tuple(column.name for column in index.columns),
                index.unique,
                _index_predicate(index.dialect_options.get(dialect, {}).get("where"), dialect),
            )
            for index in table.indexes
        }
        required_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
        }
        unique = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints(table.name)
        }
        required_fks = {
            (tuple(column.name for column in constraint.columns),
             tuple(element.column.table.name for element in constraint.elements),
             tuple(element.column.name for element in constraint.elements))
            for constraint in table.foreign_key_constraints
        }
        fks = {
            (tuple(item["constrained_columns"]),
             tuple([item["referred_table"]] * len(item["referred_columns"])),
             tuple(item["referred_columns"]))
            for item in inspector.get_foreign_keys(table.name)
        }
        if (actual != required
            or any(indexes.get(name) != columns for name, columns in required_indexes.items())
            or required_unique != unique
            or required_fks != fks):
            raise SchemaMismatch(
                f"数据库表 {table.name} 结构不匹配；请使用新的空数据库，现有数据库未被修改"
            )


def init_db(engine: Engine | None = None) -> None:
    """Initialize an empty database or reject an existing non-current database."""
    from . import models as _  # noqa: F401

    target = engine or _engine
    if inspect(target).get_table_names():
        check_schema(target)
        return
    Base.metadata.create_all(bind=target)
    with Session(target) as db, db.begin():
        db.add(SchemaVersion(id=1, revision=SCHEMA_REVISION))
    check_schema(target)


def get_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success and roll back on errors."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
