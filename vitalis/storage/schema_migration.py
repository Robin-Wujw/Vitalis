"""Audited SQLite migration from known legacy Vitalis table layouts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import CheckConstraint, MetaData, UniqueConstraint, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateTable

from . import models as orm
from .database import Base, get_engine


class SchemaMigrationRequired(RuntimeError):
    """The database layout does not match the current ORM contract."""


class UnsafeSchemaMigration(RuntimeError):
    """The known additive migration cannot preserve this database safely."""


TABLE_ORDER = (
    "auth_tokens",
    "training_preferences",
    "sleep_records",
    "activity_records",
    "training_records",
    "metric_samples",
    "daily_metrics",
    "dense_data_files",
    "analysis_runs",
    "recommendation_instances",
    "strength_exercises",
    "subjective_feedback",
    "sync_attempts",
    "sync_chunks",
    "sync_stream_states",
    "workout_metric_samples",
    "zepp_browser_links",
    "zepp_pairing_sessions",
)

COLUMN_TRANSFORMS = {
    "auth_tokens": {"region_host": "COALESCE(region_host, '')"},
    "metric_samples": {"device_id": "COALESCE(device_id, '')"},
    "daily_metrics": {"device_id": "COALESCE(device_id, '')"},
    "dense_data_files": {"device_id": "COALESCE(device_id, '')"},
}

COLUMN_EXPRESSIONS = {
    "auth_tokens": {"region_host": "''"},
    "training_preferences": {},
    "sleep_records": {},
    "activity_records": {},
    "training_records": {},
    "metric_samples": {"device_id": "''"},
    "daily_metrics": {"device_id": "''"},
    "dense_data_files": {"device_id": "''"},
    "analysis_runs": {"profile_revision_used": "0"},
    "recommendation_instances": {
        "linked_workout_source": (
            "CASE WHEN linked_workout_id IS NULL THEN NULL ELSE :legacy_source END"
        ),
    },
    "strength_exercises": {"workout_source": ":legacy_source"},
    "subjective_feedback": {
        "workout_source": (
            "CASE WHEN workout_id IS NULL THEN NULL ELSE :legacy_source END"
        ),
    },
    "sync_attempts": {
        "trigger_ref": "NULL",
        "plan_version": "'legacy-schema'",
        "request_key": "'legacy-' || id",
        "attempt_count": "0",
    },
    "sync_chunks": {
        "fetch_status": "'never'",
        "parse_status": "'never'",
        "write_status": "'never'",
    },
    "sync_stream_states": {"attempt_id": "NULL"},
    "workout_metric_samples": {"source": ":legacy_source"},
    "zepp_browser_links": {"sync_attempt_id": "NULL"},
    "zepp_pairing_sessions": {
        "processing_started_at": "NULL",
        "sync_attempt_id": "NULL",
    },
}


@dataclass(frozen=True)
class TableSchemaDrift:
    table: str
    missing_columns: tuple[str, ...]
    extra_columns: tuple[str, ...]
    altered_columns: tuple[str, ...]
    primary_key_mismatch: tuple[str, ...]
    missing_foreign_keys: tuple[str, ...]
    extra_foreign_keys: tuple[str, ...]
    unexpected_triggers: tuple[str, ...]
    missing_unique_constraints: tuple[str, ...]
    extra_unique_constraints: tuple[str, ...]
    missing_check_constraints: tuple[str, ...]
    extra_check_constraints: tuple[str, ...]
    missing_indexes: tuple[str, ...]
    extra_indexes: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not any(
            (
                self.missing_columns,
                self.extra_columns,
                self.altered_columns,
                self.primary_key_mismatch,
                self.missing_foreign_keys,
                self.extra_foreign_keys,
                self.unexpected_triggers,
                self.missing_unique_constraints,
                self.extra_unique_constraints,
                self.missing_check_constraints,
                self.extra_check_constraints,
                self.missing_indexes,
                self.extra_indexes,
            )
        )


@dataclass(frozen=True)
class SchemaAudit:
    missing_tables: tuple[str, ...]
    extra_tables: tuple[str, ...]
    tables: tuple[TableSchemaDrift, ...]

    @property
    def clean(self) -> bool:
        return (
            not self.missing_tables
            and not self.extra_tables
            and all(item.clean for item in self.tables)
        )

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "missing_tables": self.missing_tables,
            "extra_tables": self.extra_tables,
            "tables": [asdict(item) for item in self.tables if not item.clean],
        }


@dataclass(frozen=True)
class SchemaMigrationReport:
    migrated_tables: tuple[str, ...]
    row_counts: dict[str, int]
    audit: SchemaAudit

    def as_dict(self) -> dict:
        return {
            "migrated_tables": self.migrated_tables,
            "row_counts": self.row_counts,
            "audit": self.audit.as_dict(),
        }


def _constraint_signature(columns: list[str] | tuple[str, ...]) -> str:
    return ",".join(columns)


def _normalized_sql(value: object) -> str:
    text_value = "" if value is None else str(value)
    return " ".join(text_value.lower().split())


def _index_signature(
    name: str,
    columns: list[str] | tuple[str, ...],
    unique: bool,
    where: object = None,
) -> str:
    return "|".join((
        name,
        _constraint_signature(columns),
        "unique" if unique else "index",
        _normalized_sql(where),
    ))


def _column_signature(
    type_name: object,
    nullable: bool,
    default: object,
    primary_key: bool,
) -> str:
    return "|".join((
        _normalized_sql(type_name),
        "null" if nullable else "not-null",
        _normalized_sql(default),
        "pk" if primary_key else "column",
    ))


def _foreign_key_signature(
    columns: list[str] | tuple[str, ...],
    referred_table: str,
    referred_columns: list[str] | tuple[str, ...],
    ondelete: object = None,
) -> str:
    return "|".join((
        _constraint_signature(columns),
        referred_table,
        _constraint_signature(referred_columns),
        _normalized_sql(ondelete),
    ))


def _table_triggers(bind: Engine | Connection, table: str) -> tuple[str, ...]:
    statement = text(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'trigger' AND tbl_name = :table ORDER BY name"
    )
    if isinstance(bind, Engine):
        with bind.connect() as connection:
            return tuple(connection.execute(statement, {"table": table}).scalars())
    return tuple(bind.execute(statement, {"table": table}).scalars())


def audit_sqlite_schema(bind: Engine | Connection) -> SchemaAudit:
    if bind.dialect.name != "sqlite":
        raise UnsafeSchemaMigration("此迁移器只支持 SQLite")
    inspector = inspect(bind)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    drifts = []
    for name in sorted(expected_tables & actual_tables):
        table = Base.metadata.tables[name]
        actual_column_rows = {
            item["name"]: item for item in inspector.get_columns(name)
        }
        actual_columns = set(actual_column_rows)
        expected_columns = set(table.columns.keys())
        altered_columns = []
        for column_name in sorted(expected_columns & actual_columns):
            expected_column = table.columns[column_name]
            expected_signature = _column_signature(
                expected_column.type.compile(dialect=bind.dialect),
                expected_column.nullable,
                expected_column.server_default.arg
                if expected_column.server_default is not None else None,
                expected_column.primary_key,
            )
            actual_column = actual_column_rows[column_name]
            actual_signature = _column_signature(
                actual_column["type"],
                actual_column["nullable"],
                actual_column.get("default"),
                actual_column.get("primary_key", False),
            )
            if expected_signature != actual_signature:
                altered_columns.append(
                    f"{column_name}: expected={expected_signature}; actual={actual_signature}"
                )

        expected_primary_key = tuple(column.name for column in table.primary_key.columns)
        actual_primary_key = tuple(
            inspector.get_pk_constraint(name).get("constrained_columns") or ()
        )
        primary_key_mismatch = (
            (
                f"expected={_constraint_signature(expected_primary_key)}; "
                f"actual={_constraint_signature(actual_primary_key)}"
            ),
        ) if expected_primary_key != actual_primary_key else ()

        expected_foreign_keys = {
            _foreign_key_signature(
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].target_fullname.split(".", 1)[0],
                tuple(element.column.name for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in table.foreign_key_constraints
        }
        actual_foreign_keys = {
            _foreign_key_signature(
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table") or "",
                tuple(item.get("referred_columns") or ()),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(name)
        }

        expected_unique = {
            _constraint_signature(tuple(column.name for column in constraint.columns))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique = {
            _constraint_signature(tuple(item.get("column_names") or ()))
            for item in inspector.get_unique_constraints(name)
        }
        expected_checks = {
            constraint.name or str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        actual_checks = {
            item.get("name") or item.get("sqltext") or ""
            for item in inspector.get_check_constraints(name)
        }
        expected_indexes = {
            _index_signature(
                index.name,
                tuple(column.name for column in index.columns),
                bool(index.unique),
                index.dialect_options["sqlite"].get("where"),
            )
            for index in table.indexes
            if index.name
        }
        actual_indexes = {
            _index_signature(
                item["name"],
                tuple(item.get("column_names") or ()),
                bool(item.get("unique")),
                (item.get("dialect_options") or {}).get("sqlite_where"),
            )
            for item in inspector.get_indexes(name)
        }

        drifts.append(TableSchemaDrift(
            table=name,
            missing_columns=tuple(sorted(expected_columns - actual_columns)),
            extra_columns=tuple(sorted(actual_columns - expected_columns)),
            altered_columns=tuple(altered_columns),
            primary_key_mismatch=primary_key_mismatch,
            missing_foreign_keys=tuple(sorted(expected_foreign_keys - actual_foreign_keys)),
            extra_foreign_keys=tuple(sorted(actual_foreign_keys - expected_foreign_keys)),
            unexpected_triggers=_table_triggers(bind, name),
            missing_unique_constraints=tuple(sorted(expected_unique - actual_unique)),
            extra_unique_constraints=tuple(sorted(actual_unique - expected_unique)),
            missing_check_constraints=tuple(sorted(expected_checks - actual_checks)),
            extra_check_constraints=tuple(sorted(actual_checks - expected_checks)),
            missing_indexes=tuple(sorted(expected_indexes - actual_indexes)),
            extra_indexes=tuple(sorted(actual_indexes - expected_indexes)),
        ))
    return SchemaAudit(
        missing_tables=tuple(sorted(expected_tables - actual_tables)),
        extra_tables=tuple(sorted(actual_tables - expected_tables)),
        tables=tuple(drifts),
    )


def _row_count(connection: Connection, table: str) -> int:
    quoted = connection.dialect.identifier_preparer.quote(table)
    return int(connection.execute(text(f"SELECT count(*) FROM {quoted}")).scalar_one())


def _validate_preconditions(connection: Connection, drift: SchemaAudit) -> None:
    if drift.missing_tables:
        raise UnsafeSchemaMigration(
            f"数据库缺少表: {', '.join(drift.missing_tables)}"
        )
    if drift.extra_tables:
        raise UnsafeSchemaMigration(
            f"数据库存在未知表: {', '.join(drift.extra_tables)}"
        )
    trigger_tables = [
        item.table for item in drift.tables if item.unexpected_triggers
    ]
    if trigger_tables:
        raise UnsafeSchemaMigration(
            f"待迁移表存在未管理 trigger: {', '.join(sorted(trigger_tables))}"
        )
    unknown = [item.table for item in drift.tables if not item.clean and item.table not in COLUMN_EXPRESSIONS]
    if unknown:
        raise UnsafeSchemaMigration(
            f"存在未支持的表结构差异: {', '.join(sorted(unknown))}"
        )
    for table in ("sync_attempts", "sync_chunks"):
        item = next((entry for entry in drift.tables if entry.table == table), None)
        if item is not None and not item.clean and _row_count(connection, table):
            raise UnsafeSchemaMigration(
                f"{table} 含旧版运行状态，不能自动解释；请先完成或清理旧同步任务"
            )


def _rebuild_table(
    connection: Connection,
    table_name: str,
    *,
    legacy_source: str,
) -> int:
    preparer = connection.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    temporary_name = f"__vitalis_migrate_{table_name}"
    quoted_temporary = preparer.quote(temporary_name)
    connection.exec_driver_sql(f"DROP TABLE IF EXISTS {quoted_temporary}")

    temporary_metadata = MetaData()
    target = Base.metadata.tables[table_name]
    for source_table in Base.metadata.tables.values():
        source_table.to_metadata(
            temporary_metadata,
            name=temporary_name if source_table.name == table_name else source_table.name,
        )
    temporary = temporary_metadata.tables[temporary_name]
    connection.execute(CreateTable(temporary))

    actual_columns = {
        item["name"] for item in inspect(connection).get_columns(table_name)
    }
    destination = []
    expressions = []
    mappings = COLUMN_EXPRESSIONS[table_name]
    transforms = COLUMN_TRANSFORMS.get(table_name, {})
    for column in target.columns:
        destination.append(preparer.quote(column.name))
        if column.name in actual_columns and column.name in transforms:
            expressions.append(transforms[column.name])
        elif column.name in actual_columns:
            expressions.append(preparer.quote(column.name))
        elif column.name in mappings:
            expressions.append(mappings[column.name])
        elif column.nullable:
            expressions.append("NULL")
        else:
            raise UnsafeSchemaMigration(
                f"{table_name}.{column.name} 缺少安全回填规则"
            )

    before = _row_count(connection, table_name)
    statement = text(
        f"INSERT INTO {quoted_temporary} ({', '.join(destination)}) "
        f"SELECT {', '.join(expressions)} FROM {quoted_table}"
    )
    connection.execute(statement, {"legacy_source": legacy_source})
    after = _row_count(connection, temporary_name)
    if after != before:
        raise UnsafeSchemaMigration(
            f"{table_name} 行数校验失败: {before} -> {after}"
        )

    connection.exec_driver_sql(f"DROP TABLE {quoted_table}")
    connection.exec_driver_sql(
        f"ALTER TABLE {quoted_temporary} RENAME TO {quoted_table}"
    )
    for index in target.indexes:
        index.create(bind=connection, checkfirst=True)
    return after


def migrate_sqlite_schema(
    engine: Engine,
    *,
    legacy_source: str = "zepp",
) -> SchemaMigrationReport:
    if engine.dialect.name != "sqlite":
        raise UnsafeSchemaMigration("此迁移器只支持 SQLite")
    legacy_source = legacy_source.strip()
    if not legacy_source:
        raise UnsafeSchemaMigration("legacy_source 不能为空")

    before = audit_sqlite_schema(engine)
    if before.clean:
        return SchemaMigrationReport((), {}, before)

    migrated = []
    row_counts = {}
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            with connection.begin():
                _validate_preconditions(connection, before)
                drift_by_table = {
                    item.table: item for item in before.tables if not item.clean
                }
                for table_name in TABLE_ORDER:
                    if table_name not in drift_by_table:
                        continue
                    row_counts[table_name] = _rebuild_table(
                        connection,
                        table_name,
                        legacy_source=legacy_source,
                    )
                    migrated.append(table_name)
                transaction_audit = audit_sqlite_schema(connection)
                if not transaction_audit.clean:
                    raise SchemaMigrationRequired(
                        "迁移事务内 schema 仍不一致: "
                        f"{json.dumps(transaction_audit.as_dict(), ensure_ascii=False)}"
                    )
                foreign_key_errors = list(
                    connection.exec_driver_sql("PRAGMA foreign_key_check")
                )
                if foreign_key_errors:
                    raise UnsafeSchemaMigration(
                        f"外键校验失败: {len(foreign_key_errors)}"
                    )
        except Exception:
            connection.rollback()
            for table_name in TABLE_ORDER:
                temporary_name = f"__vitalis_migrate_{table_name}"
                quoted = connection.dialect.identifier_preparer.quote(temporary_name)
                connection.exec_driver_sql(f"DROP TABLE IF EXISTS {quoted}")
            connection.commit()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()

    after = audit_sqlite_schema(engine)
    if not after.clean:
        raise SchemaMigrationRequired(
            f"迁移后 schema 仍不一致: {json.dumps(after.as_dict(), ensure_ascii=False)}"
        )
    return SchemaMigrationReport(tuple(migrated), row_counts, after)


def backup_sqlite_database(engine: Engine, target: Path) -> Path:
    if engine.dialect.name != "sqlite" or not engine.url.database:
        raise UnsafeSchemaMigration("备份只支持文件型 SQLite 数据库")
    source = Path(engine.url.database).resolve()
    if str(engine.url.database) == ":memory:":
        raise UnsafeSchemaMigration("内存 SQLite 无法创建文件备份")
    target = target.resolve()
    if target == source:
        raise UnsafeSchemaMigration("备份路径不能与源数据库相同")
    if target.exists():
        raise UnsafeSchemaMigration("备份路径已存在，拒绝覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
    with sqlite3.connect(target) as backup:
        result = backup.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        target.unlink(missing_ok=True)
        raise UnsafeSchemaMigration(f"备份 quick_check 失败: {result}")
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and migrate Vitalis SQLite schema")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit", help="compare the database with current ORM metadata")
    migrate = subparsers.add_parser("migrate", help="rebuild known legacy tables")
    migrate.add_argument("--legacy-source", default="zepp")
    migrate.add_argument("--backup", type=Path, required=True)
    migrate.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = get_engine()
    if args.command == "audit":
        print(json.dumps(audit_sqlite_schema(engine).as_dict(), ensure_ascii=False, indent=2))
        return 0
    if not args.apply:
        raise SystemExit("这是数据库变更操作；确认审计结果后显式传入 --apply")
    backup = backup_sqlite_database(engine, args.backup)
    report = migrate_sqlite_schema(engine, legacy_source=args.legacy_source)
    payload = report.as_dict()
    payload["backup"] = str(backup)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
