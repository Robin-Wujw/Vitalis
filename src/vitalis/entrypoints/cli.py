"""Single command-line entrypoint for Vitalis operations."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sqlite3
import sys

from sqlalchemy.engine import make_url

from vitalis.config import settings


def _database_file() -> Path | None:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).resolve()


def _configured_sqlite_file() -> Path:
    url = make_url(settings.database_url)
    if url.drivername != "sqlite" or url.query or not url.database or url.database == ":memory:":
        raise ValueError("db backup requires a configured file-based SQLite database")
    return Path(url.database)


def _has_sqlite_sidecars(path: Path) -> bool:
    return any(
        sidecar.exists() or sidecar.is_symlink()
        for suffix in ("-wal", "-shm", "-journal")
        for sidecar in (Path(str(path) + suffix),)
    )


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _check_current_sqlite(path: Path, *, standalone: bool = False) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool
    from vitalis.adapters.persistence.database import check_schema

    if path.is_symlink() or not path.is_file():
        raise ValueError("source must be an existing regular SQLite file, not a symlink")
    if standalone and _has_sqlite_sidecars(path):
        raise ValueError("backup has SQLite sidecar files; use a standalone backup")
    engine = create_engine(
        "sqlite://", creator=lambda: _readonly_sqlite(path), poolclass=NullPool,
    )
    try:
        check_schema(engine)
        with engine.connect() as connection:
            if connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
                raise ValueError("SQLite integrity check failed")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise ValueError("SQLite foreign key check failed")
    except SQLAlchemyError as exc:
        raise ValueError("source is not a readable current SQLite database") from exc
    finally:
        engine.dispose()


def _new_sqlite_target(path: Path, *, configured: Path | None = None) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError("destination must be a new path; existing files and symlinks are refused")
    target = path.resolve()
    if configured is not None and target == configured:
        raise ValueError("db restore refuses the configured Vitalis database")
    if _has_sqlite_sidecars(target):
        raise ValueError("destination has stale SQLite sidecar files")
    return target


def _copy_sqlite(source: Path, target: Path) -> None:
    created = False
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(descriptor)
        with closing(_readonly_sqlite(source)) as source_db:
            with closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True)) as target_db:
                source_db.backup(target_db)
                target_db.execute("PRAGMA journal_mode=DELETE")
        _check_current_sqlite(target, standalone=True)
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def _backup(output: Path) -> None:
    source = _configured_sqlite_file()
    _check_current_sqlite(source)
    target = _new_sqlite_target(output)
    try:
        _copy_sqlite(source, target)
    except sqlite3.Error as exc:
        raise ValueError("SQLite backup failed") from exc
    print(json.dumps({"backup": str(target)}, ensure_ascii=False))


def _restore(backup: Path, database: Path) -> None:
    _check_current_sqlite(backup, standalone=True)
    target = _new_sqlite_target(database, configured=_database_file())
    try:
        _copy_sqlite(backup, target)
    except sqlite3.Error as exc:
        raise ValueError("SQLite restore failed") from exc
    print(json.dumps({"database": str(target)}, ensure_ascii=False))


def _doctor() -> int:
    from datetime import datetime, timezone

    from vitalis.adapters.persistence.database import SchemaMismatch, SessionLocal, check_schema
    from vitalis.adapters.persistence.models import WorkerHeartbeat

    path = _database_file()
    if path is not None and not path.is_file():
        state = "not_initialized"
    else:
        try:
            check_schema()
            state = "current"
        except SchemaMismatch:
            state = "schema_mismatch"
    worker = "not_checked"
    last_seen = None
    if state == "current":
        with SessionLocal() as db:
            row = db.get(WorkerHeartbeat, "primary")
        if row is None:
            worker = "not_seen"
        else:
            age_seconds = (datetime.now(timezone.utc).replace(tzinfo=None) - row.last_seen_at).total_seconds()
            worker = "alive" if 0 <= age_seconds <= 90 else "stale"
            last_seen = row.last_seen_at.isoformat() + "Z"
    print(json.dumps({
        "schema": state,
        "database_backend": make_url(settings.database_url).get_backend_name(),
        "timezone": settings.timezone,
        "worker": worker,
        "worker_last_seen_at": last_seen,
    }, ensure_ascii=False))
    return 0 if state == "current" else 1


def _reset_dev_database(database: Path, confirmed: bool) -> None:
    """Reset only an explicitly named, current development SQLite database."""
    from sqlalchemy import create_engine
    from vitalis.adapters.persistence.database import check_schema, init_db

    if not confirmed:
        raise ValueError("db reset requires --confirm-discard-local-data")
    if settings.env not in {"dev", "test"}:
        raise ValueError("db reset is available only in dev/test environments")
    if database.is_symlink() or not database.is_file() or database.suffix not in {".db", ".sqlite"}:
        raise ValueError("db reset requires an existing regular SQLite .db/.sqlite file")
    target = database.resolve()
    if target == _database_file():
        raise ValueError("db reset refuses the configured Vitalis database")
    if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("db reset refuses a database with pending SQLite sidecar files")
    engine = create_engine(f"sqlite:///{target.as_posix()}")
    try:
        check_schema(engine)
    finally:
        engine.dispose()
    target.unlink()
    fresh = create_engine(f"sqlite:///{target.as_posix()}")
    try:
        init_db(fresh)
    finally:
        fresh.dispose()


def _demo(database: Path, day: str | None) -> None:
    """Generate a separate synthetic data set without vendor credentials."""
    from datetime import date, datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    if database.exists() or database.is_symlink():
        raise ValueError("demo requires a new database path; existing data will not be overwritten")
    if database.suffix not in {".db", ".sqlite"}:
        raise ValueError("demo requires a .db or .sqlite path")
    target_day = date.fromisoformat(day) if day else date.today()
    settings.database_url = f"sqlite:///{database.resolve().as_posix()}"

    from vitalis import bootstrap
    from vitalis.application.sync import SyncCommand
    from vitalis.adapters.demo import seed_demo_workouts
    from vitalis.adapters.persistence import init_db
    from vitalis.intelligence.report_periods import resolve_month_period

    period = resolve_month_period(target_day)
    start = min(target_day - timedelta(days=55), period.reference_start)
    as_of = datetime.combine(target_day, time(21, 20), ZoneInfo(settings.timezone)).astimezone(timezone.utc)
    init_db()
    sync = bootstrap.get_demo_sync().execute(SyncCommand(
        "demo", name="Synthetic demo", start=start, end=target_day
    ))
    seed_demo_workouts(start, target_day, as_of, settings.timezone)
    result = bootstrap.get_intelligence_command(
        today_factory=lambda: target_day, now_factory=lambda: as_of,
    ).analyze("demo", target_day)
    print(json.dumps({
        "dataset": "synthetic_demo",
        "database": str(database.resolve()),
        "user_id": "demo",
        "days_imported": sync.days_synced,
        "analysis_run_id": result.daily.analysis_run_id,
    }, ensure_ascii=False))


def _write_report(report_kind: str, user_id: str, day: str | None, target: str, output: Path) -> None:
    """Render a stored briefing to a new local file without sending it."""
    from datetime import date

    from vitalis import bootstrap
    from vitalis.intelligence.report_rendering import render_report

    if output.exists() or output.is_symlink():
        raise ValueError("report output must be a new path; existing files and symlinks are refused")
    target_day = date.fromisoformat(day) if day else None
    query = bootstrap.get_intelligence_query()
    briefing = {
        "morning": query.morning_briefing,
        "daily": query.daily,
        "evening": query.evening_briefing,
        "weekly": query.weekly_briefing,
        "monthly": query.monthly_briefing,
    }[report_kind](user_id, target_day)
    if briefing is None:
        raise ValueError(f"no {report_kind} briefing is available for this user and date")
    rendered = render_report(briefing, target)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(rendered.content)
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    print(json.dumps({
        "report": report_kind,
        "target": target,
        "media_type": rendered.media_type,
        "renderer_version": rendered.renderer_version,
        "content_sha256": rendered.content_sha256,
        "output": str(output.resolve()),
    }, ensure_ascii=False))


def _create_user(user_id: str) -> None:
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.database import check_schema

    check_schema()
    if not user_id.strip() or user_id != user_id.strip():
        raise ValueError("user id must be nonempty without surrounding whitespace")
    with session_scope() as db:
        HealthRepository(db).upsert_user(user_id)


def _issue_token(user_id: str, scopes: list[str], days: int, output: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.access_tokens import provision_access_token, token_digest
    from vitalis.adapters.persistence.database import check_schema

    check_schema()
    if not 1 <= days <= 365:
        raise ValueError("--expires-days must be between 1 and 365")
    created = False
    try:
        with session_scope() as db:
            token = provision_access_token(
                db, user_id, scopes,
                expires_at=datetime.now(timezone.utc) + timedelta(days=days),
            )
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(token + "\n")
        print(json.dumps({"token_file": str(output.resolve()), "digest": token_digest(token)}))
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise


def _revoke_token(digest: str) -> None:
    from vitalis.adapters.persistence import session_scope
    from vitalis.adapters.persistence.access_tokens import revoke_access_token
    from vitalis.adapters.persistence.database import check_schema

    check_schema()
    with session_scope() as db:
        if not revoke_access_token(db, digest):
            raise ValueError("access token digest not found or already revoked")
    print("access token revoked")


_REPLAY_INPUT_LIMIT_BYTES = 128 * 1024 * 1024


def _require_offline_replay_environment() -> None:
    if settings.env not in {"dev", "test"}:
        raise ValueError("offline replay is available only in dev/test environments")


def _parse_replay_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("replay timestamps must use an explicit timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("replay timestamps must use ISO-8601 format") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("replay timestamps must use an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _read_replay_fixture(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("replay fixture must be an existing regular file")
    try:
        if path.stat().st_size > _REPLAY_INPUT_LIMIT_BYTES:
            raise ValueError("replay fixture exceeds its bounded size")
        raw = path.read_bytes()
        fixture = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ValueError("replay fixture is not valid JSON") from None
    except (OSError, UnicodeError):
        raise ValueError("replay fixture could not be read") from None
    if not isinstance(fixture, dict):
        raise ValueError("replay fixture must contain a JSON object")
    return fixture


def _replay_fixture_command(path: Path, target_user_id: str, parser_version: str | None) -> None:
    _require_offline_replay_environment()
    from vitalis.adapters.replay import CURRENT_PARSER_VERSION, replay_fixture

    fixture = _read_replay_fixture(path)
    result = replay_fixture(
        fixture, target_user_id,
        parser_version=parser_version or CURRENT_PARSER_VERSION,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False))


def _replay_source_journal_command(args: argparse.Namespace) -> None:
    _require_offline_replay_environment()
    from vitalis.adapters.replay import replay_source_records
    from vitalis.adapters.persistence.source_journal import CURRENT_PARSER_VERSION

    try:
        start_date = date.fromisoformat(args.start_date)
        end_date = date.fromisoformat(args.end_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("replay dates must use YYYY-MM-DD format") from exc
    result = replay_source_records(
        args.source_user_id,
        args.target_user_id,
        start_date=start_date,
        end_date=end_date,
        as_of=_parse_replay_datetime(args.as_of),
        journal_ids=args.journal_ids,
        parser_version=args.parser_version or CURRENT_PARSER_VERSION,
        timezone_name=args.timezone,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vitalis", description="Vitalis health data service")
    try:
        package_version = version("vitalis")
    except PackageNotFoundError:
        from vitalis import __version__

        package_version = __version__
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version}")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="Start the loopback API process")
    subcommands.add_parser("worker", help="Start scheduled synchronization and delivery")
    subcommands.add_parser("doctor", help="Check configuration and schema without exposing secrets")
    user = subcommands.add_parser("user", help="Manage local users")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    create_user = user_commands.add_parser("create", help="Create a local user")
    create_user.add_argument("--id", required=True)
    token = subcommands.add_parser("token", help="Provision scoped API access")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    issue = token_commands.add_parser("issue", help="Write a new token to a private file")
    issue.add_argument("--user", required=True)
    issue.add_argument("--scope", dest="scopes", choices=("read", "analyze", "sync", "feedback", "manage"), action="append")
    issue.add_argument("--expires-days", type=int, default=30)
    issue.add_argument("--output", type=Path, required=True)
    revoke = token_commands.add_parser("revoke", help="Revoke a token by its non-secret digest")
    revoke.add_argument("--digest", required=True)
    database = subcommands.add_parser("db", help="Initialize, back up, or restore a database")
    db_commands = database.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("init", help="Initialize the configured empty database")
    reset = db_commands.add_parser("reset", help="Reset a specifically named development database")
    reset.add_argument("--database", type=Path, required=True)
    reset.add_argument("--confirm-discard-local-data", action="store_true")
    backup = db_commands.add_parser("backup", help="Snapshot the configured current SQLite database")
    backup.add_argument("--output", type=Path, required=True)
    restore = db_commands.add_parser("restore", help="Restore a current backup to a new SQLite database")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    demo = subcommands.add_parser("demo", help="Create synthetic data in a new SQLite database")
    demo.add_argument("--database", type=Path, required=True)
    demo.add_argument("--day", help="Local date in YYYY-MM-DD format")
    report = subcommands.add_parser("report", help="Render a stored report without sending it")
    report.add_argument("kind", choices=("morning", "daily", "evening", "weekly", "monthly"))
    report.add_argument("--user", required=True)
    report.add_argument("--day", help="Report date in YYYY-MM-DD format")
    report.add_argument("--format", dest="target", choices=("markdown", "html"), default="markdown")
    report.add_argument("--output", type=Path, required=True)
    replay = subcommands.add_parser("replay", help="Replay local source input without network or delivery")
    replay_commands = replay.add_subparsers(dest="replay_command", required=True)
    fixture_replay = replay_commands.add_parser(
        "fixture", help="Replay a synthetic JSON fixture into an independent user",
    )
    fixture_replay.add_argument("fixture_path", nargs="?", type=Path)
    fixture_replay.add_argument("--input", "--fixture", dest="fixture_option", type=Path)
    fixture_replay.add_argument("--target-user", "--user", dest="target_user_id", required=True)
    fixture_replay.add_argument("--parser-version")
    journal_replay = replay_commands.add_parser(
        "source-journal", aliases=("journal",),
        help="Replay encrypted source journal metadata into an independent user",
    )
    journal_replay.add_argument("--source-user", dest="source_user_id", required=True)
    journal_replay.add_argument("--target-user", "--user", dest="target_user_id", required=True)
    journal_replay.add_argument("--start-date", "--start", dest="start_date", required=True)
    journal_replay.add_argument("--end-date", "--end", dest="end_date", required=True)
    journal_replay.add_argument("--as-of", required=True)
    journal_replay.add_argument("--timezone", default="UTC")
    journal_replay.add_argument("--journal-id", "--journal-ids", dest="journal_ids", action="append")
    journal_replay.add_argument("--parser-version")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn

            uvicorn.run(
                "vitalis.entrypoints.api.app:app",
                host=settings.host, port=settings.port, reload=False,
            )
        elif args.command == "worker":
            from vitalis.entrypoints.worker import main as worker

            worker()
        elif args.command == "doctor":
            return _doctor()
        elif args.command == "user":
            _create_user(args.id)
            print("local user ready")
        elif args.command == "token":
            if args.token_command == "issue":
                _issue_token(args.user, args.scopes or ["read"], args.expires_days, args.output)
            else:
                _revoke_token(args.digest)
        elif args.command == "db":
            if args.db_command == "init":
                from vitalis.adapters.persistence import init_db

                init_db()
                print("database initialized")
            elif args.db_command == "reset":
                _reset_dev_database(args.database, args.confirm_discard_local_data)
                print("development database reset")
            elif args.db_command == "backup":
                _backup(args.output)
            else:
                _restore(args.backup, args.database)
        elif args.command == "demo":
            _demo(args.database, args.day)
        elif args.command == "report":
            _write_report(args.kind, args.user, args.day, args.target, args.output)
        elif args.command == "replay":
            if args.replay_command == "fixture":
                path = args.fixture_option or args.fixture_path
                if args.fixture_option is not None and args.fixture_path is not None:
                    raise ValueError("replay fixture accepts one input path")
                if path is None:
                    raise ValueError("replay fixture requires --input PATH")
                _replay_fixture_command(path, args.target_user_id, args.parser_version)
            else:
                _replay_source_journal_command(args)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"vitalis: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
