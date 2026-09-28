"""CLI backup and restore operate only on disposable current-schema SQLite files."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]


def _cli(*args, database_url: str = "sqlite:///:memory:") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vitalis", *map(str, args)],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "ZEPP_MOCK": "true"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _current_file(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    initialized = _cli("db", "init", database_url=_url(path))
    assert initialized.returncode == 0, initialized.stderr
    return path


def _facts(path: Path) -> tuple[str, int, int, int]:
    with closing(sqlite3.connect(path)) as db:
        return (
            db.execute("SELECT name FROM users WHERE id='demo'").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM sleep_records").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM access_tokens").fetchone()[0],
        )


def test_demo_online_wal_backup_restore_and_restart(tmp_path):
    source = tmp_path / "demo.db"
    demo = _cli("demo", "--database", source, "--day", "2026-08-29")
    assert demo.returncode == 0, demo.stderr
    assert json.loads(demo.stdout)["days_imported"] > 0
    token_file = tmp_path / "private.token"
    issued = _cli("token", "issue", "--user", "demo", "--output", token_file, database_url=_url(source))
    assert issued.returncode == 0, issued.stderr
    secret = token_file.read_text(encoding="utf-8").strip()
    digest = json.loads(issued.stdout)["digest"]
    backup = tmp_path / "snapshot.sqlite"

    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE users SET name='Snapshot in WAL' WHERE id='demo'")
        writer.commit()
        assert Path(str(source) + "-wal").exists()
        result = _cli("db", "backup", "--output", backup, database_url=_url(source))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"backup": str(backup.resolve())}
        assert backup.is_file()
        assert not Path(str(backup) + "-wal").exists()
        assert not Path(str(backup) + "-shm").exists()
        assert _facts(backup) == _facts(source)
        writer.execute("UPDATE users SET name='After snapshot' WHERE id='demo'")
        writer.commit()

    restored = tmp_path / "restored.db"
    result = _cli("db", "restore", "--backup", backup, "--database", restored, database_url=_url(source))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"database": str(restored.resolve())}
    assert _facts(restored) == _facts(backup)
    assert _facts(restored)[0] == "Snapshot in WAL"
    assert _facts(source)[0] == "After snapshot"
    assert _facts(restored)[1] > 0 and _facts(restored)[2] > 0 and _facts(restored)[3] == 1
    for path in (backup, restored):
        assert secret not in path.read_bytes().decode("latin1")
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()
    assert secret not in result.stdout + result.stderr
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT token_digest FROM access_tokens").fetchone()[0] == digest
    assert _cli("doctor", database_url=_url(restored)).returncode == 0
    assert _cli("db", "init", database_url=_url(restored)).returncode == 0
    assert _cli("doctor", database_url=_url(source)).returncode == 0


def test_backup_requires_current_configured_file_and_new_destination(tmp_path):
    source = _current_file(tmp_path)
    target = tmp_path / "backup.db"
    target.write_bytes(b"leave this file alone")
    refused = _cli("db", "backup", "--output", target, database_url=_url(source))
    assert refused.returncode == 1 and "new path" in refused.stderr
    assert target.read_bytes() == b"leave this file alone"
    target.unlink()
    sidecar = Path(str(target) + "-wal")
    sidecar.write_bytes(b"stale WAL")
    refused = _cli("db", "backup", "--output", target, database_url=_url(source))
    assert refused.returncode == 1 and "sidecar" in refused.stderr
    assert sidecar.read_bytes() == b"stale WAL"
    assert not target.exists()

    missing = tmp_path / "no-database.db"
    refused = _cli("db", "backup", "--output", tmp_path / "missing-copy.db", database_url=_url(missing))
    assert refused.returncode == 1 and "existing regular SQLite" in refused.stderr
    assert not missing.exists()
    refused = _cli("db", "backup", "--output", tmp_path / "memory.db")
    assert refused.returncode == 1 and "file-based SQLite" in refused.stderr
    refused = _cli(
        "db", "backup", "--output", tmp_path / "postgres.db",
        database_url="postgresql+psycopg://localhost:5432/not_connected",
    )
    assert refused.returncode == 1 and "file-based SQLite" in refused.stderr
    assert not (tmp_path / "postgres.db").exists()

    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE vitalis_schema SET revision='unknown'")
        db.commit()
    refused = _cli("db", "backup", "--output", tmp_path / "unknown.db", database_url=_url(source))
    assert refused.returncode == 1 and "schema" in refused.stderr
    assert not (tmp_path / "unknown.db").exists()


def test_restore_refuses_unknown_backup_and_unsafe_targets(tmp_path):
    source = _current_file(tmp_path)
    backup = tmp_path / "good.db"
    assert _cli("db", "backup", "--output", backup, database_url=_url(source)).returncode == 0
    restored = tmp_path / "restored.db"
    restored.write_bytes(b"unchanged")
    refused = _cli("db", "restore", "--backup", backup, "--database", restored, database_url=_url(source))
    assert refused.returncode == 1 and "new path" in refused.stderr
    assert restored.read_bytes() == b"unchanged"
    restored.unlink()

    sidecar = Path(str(restored) + "-shm")
    sidecar.write_bytes(b"stale")
    refused = _cli("db", "restore", "--backup", backup, "--database", restored, database_url=_url(source))
    assert refused.returncode == 1 and "sidecar" in refused.stderr
    assert sidecar.read_bytes() == b"stale"
    assert not restored.exists()
    sidecar.unlink()

    configured = tmp_path / "configured.db"
    refused = _cli("db", "restore", "--backup", backup, "--database", configured, database_url=_url(configured))
    assert refused.returncode == 1 and "configured Vitalis database" in refused.stderr
    assert not configured.exists()
    refused = _cli("db", "restore", "--backup", backup, database_url=_url(source))
    assert refused.returncode == 2 and "--database" in refused.stderr

    invalid = tmp_path / "invalid.db"
    with closing(sqlite3.connect(invalid)) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
        db.commit()
    original = invalid.read_bytes()
    refused = _cli("db", "restore", "--backup", invalid, "--database", restored, database_url=_url(source))
    assert refused.returncode == 1 and "schema" in refused.stderr
    assert invalid.read_bytes() == original and not restored.exists()
    invalid.write_bytes(b"not sqlite")
    refused = _cli("db", "restore", "--backup", invalid, "--database", restored, database_url=_url(source))
    assert refused.returncode == 1 and "readable current SQLite" in refused.stderr
    assert not restored.exists()

    backup_sidecar = Path(str(backup) + "-wal")
    backup_sidecar.write_bytes(b"stale")
    refused = _cli("db", "restore", "--backup", backup, "--database", restored, database_url=_url(source))
    assert refused.returncode == 1 and "standalone backup" in refused.stderr
    assert not restored.exists()


def test_symlink_sources_and_destinations_are_refused(tmp_path):
    source = _current_file(tmp_path)
    backup = tmp_path / "backup.db"
    assert _cli("db", "backup", "--output", backup, database_url=_url(source)).returncode == 0
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    refused = _cli("db", "backup", "--output", tmp_path / "no.db", database_url=_url(alias))
    assert refused.returncode == 1 and "symlink" in refused.stderr
    refused = _cli("db", "backup", "--output", alias, database_url=_url(source))
    assert refused.returncode == 1 and "symlink" in refused.stderr
    refused = _cli("db", "restore", "--backup", alias, "--database", tmp_path / "no-restored.db")
    assert refused.returncode == 1 and "symlink" in refused.stderr
    refused = _cli("db", "restore", "--backup", backup, "--database", alias, database_url=_url(source))
    assert refused.returncode == 1 and "symlink" in refused.stderr
    assert source.is_file() and not (tmp_path / "no-restored.db").exists()
