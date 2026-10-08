"""Upgrade the known calendar-report deployment database without losing records.

This is an explicit deployment operation, not a runtime schema fallback. Stop the
API and worker before --apply. The source is checkpointed and backed up, an
isolated candidate is upgraded and validated, and only then is it atomically
installed at the existing database path. Reports and notifications are not sent.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from vitalis.adapters.persistence.database import Base, SCHEMA_REVISION, check_schema
from vitalis.adapters.persistence import models  # noqa: F401 - register current tables


SOURCE_REVISION = "2026-10-calendar-report-delivery"
NOTIFICATION_COLUMNS = {
    "id", "user_id", "analysis_run_id", "period", "target_date", "status",
    "attempt_count", "lease_token", "lease_expires_at", "next_attempt_at",
    "last_error", "created_at", "updated_at",
}
STRENGTH_COLUMNS = {
    "id", "user_id", "workout_source", "workout_id", "order", "exercise_name",
    "exercise_id", "session_focus", "movement_pattern", "movement_pattern_label",
    "muscle_groups", "muscle_group_labels", "sets", "repetitions", "weight_kg",
    "rpe", "rir", "rest_seconds", "source", "confidence", "confidence_label",
    "created_at",
}
AFFECTED = {"notification_deliveries": NOTIFICATION_COLUMNS, "strength_exercises": STRENGTH_COLUMNS}
DIALECT = sqlite.dialect()


def _quote(name: str) -> str:
    return DIALECT.identifier_preparer.quote(name)


@contextmanager
def _connect(path: Path, *, readonly: bool = False):
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=30)
    try:
        yield connection
    finally:
        connection.close()


def _revision(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute("SELECT revision FROM vitalis_schema WHERE id=1").fetchone()
    except sqlite3.Error as exc:
        raise ValueError("source is not a versioned Vitalis database") from exc
    if row is None:
        raise ValueError("source has no schema revision")
    return row[0]


def _validate_source(connection: sqlite3.Connection) -> None:
    found = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if found != set(Base.metadata.tables):
        raise ValueError("source table set does not match the known deployment schema")
    for name, table in Base.metadata.tables.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")}
        expected = AFFECTED.get(name, {column.name for column in table.columns})
        if columns != expected:
            raise ValueError(f"source table {name} does not match the known deployment schema")
    statuses = {row[0] for row in connection.execute("SELECT DISTINCT status FROM notification_deliveries")}
    if not statuses <= {"pending", "running", "succeeded", "failed", "uncertain", "deferred"}:
        raise ValueError("source contains unsupported notification statuses")
    _integrity(connection)


def _integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("database foreign key check failed")


def _fingerprint(connection: sqlite3.Connection, name: str, columns: list[str] | None = None) -> tuple[int, str]:
    columns = columns or [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")]
    primary_key = [column.name for column in Base.metadata.tables[name].primary_key.columns]
    order = ",".join(_quote(column) for column in primary_key or columns)
    select = ",".join(_quote(column) for column in columns)
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(f"SELECT {select} FROM {_quote(name)} ORDER BY {order}"):
        # Private values only pass through this in-memory digest; no values are
        # included in output, exceptions, fixtures, or deployment logs.
        data = [value.hex() if isinstance(value, bytes) else value for value in row]
        digest.update(json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _new_file(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("backup and candidate destinations must be new paths")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _copy_database(source: sqlite3.Connection, target: Path) -> None:
    _new_file(target)
    with _connect(target) as destination:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE")


def _rebuild(connection: sqlite3.Connection, name: str) -> None:
    table = Base.metadata.tables[name]
    temporary = name + "__deployment_upgrade"
    sql = str(CreateTable(table).compile(dialect=DIALECT))
    sql = sql.replace(f"CREATE TABLE {_quote(name)}", f"CREATE TABLE {_quote(temporary)}", 1)
    connection.execute(sql)
    old = AFFECTED[name]
    expressions = []
    for column in table.columns:
        key = column.name
        if name == "notification_deliveries" and key == "status":
            expression = "CASE status WHEN 'succeeded' THEN 'accepted' WHEN 'running' THEN 'uncertain' ELSE status END"
        elif name == "notification_deliveries" and key == "send_attempt_count":
            expression = "attempt_count"
        elif name == "notification_deliveries" and key == "poll_attempt_count":
            expression = "0"
        elif name == "notification_deliveries" and key in {"lease_token", "lease_expires_at"}:
            expression = "NULL"
        elif name == "notification_deliveries" and key == "last_error":
            expression = "CASE WHEN status='running' THEN 'lease_expired_uncertain' ELSE last_error END"
        elif name == "notification_deliveries" and key == "provider_status":
            # The old sender discarded provider IDs and never confirmed final
            # delivery. Preserve acknowledgment without claiming final delivery.
            expression = "CASE WHEN status='succeeded' THEN 'accepted_untracked' ELSE NULL END"
        elif name == "strength_exercises" and key == "weight_unit":
            expression = "CASE WHEN weight_kg IS NOT NULL THEN 'kg' ELSE NULL END"
        elif key in old:
            expression = _quote(key)
        else:
            expression = "NULL"
        expressions.append(expression)
    columns = ",".join(_quote(column.name) for column in table.columns)
    connection.execute(f"INSERT INTO {_quote(temporary)} ({columns}) SELECT {','.join(expressions)} FROM {_quote(name)}")
    connection.execute(f"DROP TABLE {_quote(name)}")
    connection.execute(f"ALTER TABLE {_quote(temporary)} RENAME TO {_quote(name)}")
    for index in sorted(table.indexes, key=lambda item: item.name):
        connection.execute(str(CreateIndex(index).compile(dialect=DIALECT)))


def upgrade(database: Path, backup: Path, *, apply: bool = False) -> dict:
    if database.is_symlink() or not database.is_file():
        raise ValueError("source must be an existing regular SQLite database")
    database = database.resolve()
    backup = backup.absolute()
    with _connect(database, readonly=True) as source:
        revision = _revision(source)
        if revision == SCHEMA_REVISION:
            engine = create_engine(f"sqlite:///{database.as_posix()}")
            try:
                check_schema(engine)
            finally:
                engine.dispose()
            return {"status": "already_current", "revision": revision}
        if revision != SOURCE_REVISION:
            raise ValueError("only the verified calendar-report deployment revision can be upgraded")
        _validate_source(source)
    if not apply:
        return {"status": "ready", "source_revision": revision, "target_revision": SCHEMA_REVISION}
    if backup.exists() or backup.is_symlink() or backup.resolve() == database:
        raise ValueError("backup must be a new path distinct from the source")
    original_stat = database.stat()
    candidate = database.with_name(database.name + ".upgrade-" + uuid4().hex + ".sqlite")
    try:
        with _connect(database) as source:
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and checkpoint[0]:
                raise ValueError("database still has active WAL writers; stop services first")
            _copy_database(source, backup)
            _copy_database(source, candidate)
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise ValueError("source has active SQLite sidecar data; services must be stopped")
        with _connect(candidate) as working:
            untouched = {name: _fingerprint(working, name) for name in Base.metadata.tables if name not in AFFECTED and name != "vitalis_schema"}
            strength = _fingerprint(working, "strength_exercises", sorted(STRENGTH_COLUMNS))
            deliveries = _fingerprint(working, "notification_deliveries", sorted(NOTIFICATION_COLUMNS - {"status", "lease_token", "lease_expires_at", "last_error"}))
            original_statuses = Counter(dict(working.execute("SELECT status,COUNT(*) FROM notification_deliveries GROUP BY status")))
            working.execute("PRAGMA foreign_keys=OFF")
            working.execute("BEGIN IMMEDIATE")
            _rebuild(working, "strength_exercises")
            _rebuild(working, "notification_deliveries")
            working.execute("UPDATE vitalis_schema SET revision=? WHERE id=1", (SCHEMA_REVISION,))
            working.commit()
            working.execute("PRAGMA foreign_keys=ON")
            _integrity(working)
            if any(_fingerprint(working, name) != fingerprint for name, fingerprint in untouched.items()):
                raise ValueError("upgrade changed unrelated records")
            if _fingerprint(working, "strength_exercises", sorted(STRENGTH_COLUMNS)) != strength:
                raise ValueError("upgrade changed existing strength records")
            if _fingerprint(working, "notification_deliveries", sorted(NOTIFICATION_COLUMNS - {"status", "lease_token", "lease_expires_at", "last_error"})) != deliveries:
                raise ValueError("upgrade changed delivery identities or history")
            expected = Counter()
            for status, count in original_statuses.items():
                expected[{"succeeded": "accepted", "running": "uncertain"}.get(status, status)] += count
            actual = Counter(dict(working.execute("SELECT status,COUNT(*) FROM notification_deliveries GROUP BY status")))
            if actual != expected:
                raise ValueError("upgrade did not preserve delivery-state semantics")
        engine = create_engine(f"sqlite:///{candidate.as_posix()}")
        try:
            check_schema(engine)
        finally:
            engine.dispose()
        os.chmod(candidate, stat.S_IMODE(original_stat.st_mode))
        if hasattr(os, "chown"):
            os.chown(candidate, original_stat.st_uid, original_stat.st_gid)
        os.replace(candidate, database)
        return {"status": "upgraded", "revision": SCHEMA_REVISION, "backup": str(backup), "records_preserved": True}
    finally:
        # Only this operation's candidate is removed. The source and backup are
        # retained on every validation error.
        candidate.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.database, args.backup, apply=args.apply)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"deployment database upgrade failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
