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


def test_report_export_is_read_only_and_never_overwrites_a_file(tmp_path):
    import sqlite3

    target = tmp_path / "reports.db"
    created = _cli("demo", "--database", target, "--day", "2026-10-07")
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["days_imported"] >= 61
    database_url = f"sqlite:///{target.as_posix()}"

    def counts():
        with sqlite3.connect(target) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for (name,) in tables}

    before = counts()
    for kind in ("morning", "daily", "weekly", "monthly"):
        for format in ("markdown", "html"):
            output = tmp_path / f"{kind}.{format}"
            result = _cli(
                "report", kind, "--user", "demo", "--day", "2026-10-07",
                "--format", format, "--output", output, database_url=database_url,
            )
            assert result.returncode == 0, result.stderr
            metadata = json.loads(result.stdout)
            content = output.read_bytes()
            import hashlib
            assert metadata["content_sha256"] == hashlib.sha256(content).hexdigest()
            assert metadata["media_type"] == f"text/{format}"
            assert metadata["renderer_version"]
            assert b"2026-10-07" in content
            again = _cli(
                "report", kind, "--user", "demo", "--day", "2026-10-07",
                "--format", format, "--output", output, database_url=database_url,
            )
            assert again.returncode == 1
            assert output.read_bytes() == content
    assert counts() == before


def test_report_creation_race_preserves_the_other_writers_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from vitalis import bootstrap
    from vitalis.entrypoints import cli

    briefing = {
        "period": "evening", "headline": "合成记录", "date": "2026-10-07",
        "metrics": [{"key": "steps", "label": "步数", "value": 0, "unit": "steps"}],
    }
    reader = lambda *_args: briefing
    monkeypatch.setattr(bootstrap, "get_intelligence_query", lambda: SimpleNamespace(
        morning_briefing=reader, daily=reader, evening_briefing=reader,
        weekly_briefing=reader, monthly_briefing=reader,
    ))
    output = tmp_path / "report.md"

    def racing_writer(*_args):
        output.write_text("other writer's file", encoding="utf-8")
        raise FileExistsError("file was created concurrently")

    monkeypatch.setattr(cli.os, "open", racing_writer)
    import pytest
    with pytest.raises(FileExistsError):
        cli._write_report("daily", "demo", "2026-10-07", "markdown", output)
    assert output.read_text(encoding="utf-8") == "other writer's file"


def test_report_export_uses_daily_profile_for_daily_and_briefing_for_evening(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from vitalis import bootstrap
    from vitalis.entrypoints import cli

    calls = []
    daily_profile = {
        "analysis_run_id": "daily-run", "user_id": "demo", "date": "2026-10-07",
        "report_context": {"as_of": "2026-10-07T13:20:00+00:00", "timezone": "Asia/Shanghai"},
        "data_quality": {"status": "SUFFICIENT", "status_label": "数据完整"},
        "features": {"sleep": {"duration_minutes": 420}, "activity": {}, "training": {}},
        "facts": {}, "events": [], "decision": {"action": "INSUFFICIENT_DATA", "action_plan": {}},
    }
    evening_briefing = {
        "period": "evening", "analysis_run_id": "evening-run", "user_id": "demo",
        "date": "2026-10-07", "period_start": "2026-10-07", "period_end": "2026-10-07",
        "headline": "当日复盘", "metrics": [], "findings": [], "training": [],
        "suggestions": [], "alerts": [], "sections": [],
    }

    def daily(*_args):
        calls.append("daily")
        return daily_profile

    def evening(*_args):
        calls.append("evening")
        return evening_briefing

    monkeypatch.setattr(bootstrap, "get_intelligence_query", lambda: SimpleNamespace(
        morning_briefing=lambda *_args: None,
        daily=daily,
        evening_briefing=evening,
        weekly_briefing=lambda *_args: None,
        monthly_briefing=lambda *_args: None,
    ))
    daily_output = tmp_path / "daily.md"
    evening_output = tmp_path / "evening.md"
    cli._write_report("daily", "demo", "2026-10-07", "markdown", daily_output)
    cli._write_report("evening", "demo", "2026-10-07", "markdown", evening_output)

    assert calls == ["daily", "evening"]
    assert daily_output.read_text(encoding="utf-8").startswith("# 日报 ·")
    assert evening_output.read_text(encoding="utf-8").startswith("# 晚报 ·")
