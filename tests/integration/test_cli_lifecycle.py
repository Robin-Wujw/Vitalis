import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine

from vitalis.entrypoints.cli import _reset_dev_database
from vitalis.adapters.persistence.database import check_schema, init_db


def test_user_token_lifecycle_keeps_raw_secret_out_of_stdout(tmp_path):
    target = tmp_path / "owner.db"
    database_url = f"sqlite:///{target.as_posix()}"
    assert _cli("db", "init", database_url=database_url).returncode == 0
    assert _cli("user", "create", "--id", "owner", database_url=database_url).returncode == 0
    token_file = tmp_path / "access.token"
    issued = _cli(
        "token", "issue", "--user", "owner", "--scope", "read",
        "--scope", "analyze", "--output", token_file, database_url=database_url,
    )
    assert issued.returncode == 0, issued.stderr
    token = token_file.read_text(encoding="utf-8").strip()
    assert len(token) >= 40 and token not in issued.stdout and token not in issued.stderr
    digest = json.loads(issued.stdout)["digest"]
    repeated = _cli("token", "issue", "--user", "owner", "--output", token_file, database_url=database_url)
    assert repeated.returncode != 0
    assert token_file.read_text(encoding="utf-8").strip() == token
    revoked = _cli("token", "revoke", "--digest", digest, database_url=database_url)
    assert revoked.returncode == 0, revoked.stderr
    from sqlalchemy.orm import Session
    from vitalis.adapters.persistence.models import AccessToken

    engine = create_engine(database_url)
    try:
        with Session(engine) as db:
            assert db.get(AccessToken, digest).revoked_at is not None
    finally:
        engine.dispose()


ROOT = Path(__file__).parents[2]


def _cli(*args, database_url="sqlite:///:memory:"):
    env = {**os.environ, "DATABASE_URL": database_url, "ZEPP_MOCK": "true"}
    return subprocess.run(
        [sys.executable, "-m", "vitalis", *map(str, args)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


def test_module_cli_version_and_help():
    version = _cli("--version")
    assert version.returncode == 0
    assert version.stdout.strip().startswith("vitalis ")
    assert _cli("--help").returncode == 0
    assert _cli("db", "reset", "--help").returncode == 0


def test_development_reset_requires_confirmation_and_never_targets_configured_db(tmp_path, monkeypatch):
    target = tmp_path / "scratch.db"
    engine = create_engine(f"sqlite:///{target.as_posix()}")
    init_db(engine)
    engine.dispose()
    before = target.read_bytes()
    try:
        _reset_dev_database(target, confirmed=False)
    except ValueError as exc:
        assert "--confirm-discard-local-data" in str(exc)
    else:
        raise AssertionError("reset must require confirmation")
    assert target.read_bytes() == before
    _reset_dev_database(target, confirmed=True)
    engine = create_engine(f"sqlite:///{target.as_posix()}")
    try:
        check_schema(engine)
    finally:
        engine.dispose()


def test_new_database_demo_analysis_and_restart(tmp_path):
    target = tmp_path / "demo.db"
    demo = _cli("demo", "--database", target, "--day", "2026-08-29")
    assert demo.returncode == 0, demo.stderr
    result = json.loads(demo.stdout)
    assert result["dataset"] == "synthetic_demo"
    assert result["days_imported"] > 0
    assert result["analysis_run_id"]
    assert _cli("doctor", database_url=f"sqlite:///{target.as_posix()}").returncode == 0
    assert _cli("db", "init", database_url=f"sqlite:///{target.as_posix()}").returncode == 0
    again = _cli("demo", "--database", target)
    assert again.returncode == 1
    assert "existing data" in again.stderr
