"""The local Hermes launcher keeps credentials in the child environment."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from tools import run_hermes


TOKEN = "synthetic-hermes-private-token"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    home = tmp_path / "private hermes"
    skill = home / "skills" / "vitalis"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("synthetic installed skill\n", encoding="utf-8")
    (home / "config.yaml").write_text("model: synthetic\n", encoding="utf-8")
    token_file = tmp_path / "private.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setattr(run_hermes, "ROOT", tmp_path / "checkout")
    return home, token_file


def arguments(configured, origin="http://127.0.0.1:8000"):
    home, token_file = configured
    return ["--home", str(home), "--token-file", str(token_file),
            "--api-base-url", origin, "--hermes-command", sys.executable]


def test_launch_injects_private_environment_and_preserves_failure(configured, monkeypatch, capsys):
    home, _ = configured
    monkeypatch.setenv("VITALIS_API", "legacy")
    monkeypatch.setenv("VITALIS_USER", "legacy-user")
    monkeypatch.setenv("HERMES_LAUNCH_TEST", "inherited")
    calls = []

    def child(command, *, cwd, env, check):
        calls.append(command)
        assert command == [sys.executable, "gateway", "run"]
        assert TOKEN not in repr(command)
        assert cwd == home.resolve()
        assert env["HERMES_HOME"] == str(home.resolve())
        assert env["VITALIS_ACCESS_TOKEN"] == TOKEN
        assert env["VITALIS_API_BASE_URL"] == "http://127.0.0.1:8000"
        assert env["HERMES_LAUNCH_TEST"] == "inherited"
        assert "VITALIS_API" not in env and "VITALIS_USER" not in env
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
        scratch = run_hermes.ROOT / ".tmp" / "hermes-launcher"
        assert scratch.is_dir()
        assert {env[name] for name in ("TMP", "TEMP", "TMPDIR")} == {str(scratch)}
        assert check is False
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(run_hermes.subprocess, "run", child)
    assert run_hermes.main(arguments(configured) + ["--", "gateway", "run"]) == 23
    assert calls
    assert TOKEN not in repr(capsys.readouterr())
    assert os.environ.get("VITALIS_API") == "legacy"


def test_real_child_receives_token_without_token_in_command(configured, capfd):
    script = (
        "import hashlib,json,os,sys; "
        "print(json.dumps({'digest':hashlib.sha256(os.environ['VITALIS_ACCESS_TOKEN'].encode()).hexdigest(),"
        "'home':os.getcwd(),'args':sys.argv[1:]})); sys.exit(17)"
    )
    command = arguments(configured) + ["--", "-c", script, "synthetic-argument"]
    assert TOKEN not in repr(command)
    assert run_hermes.main(command) == 17
    output = capfd.readouterr()
    assert TOKEN not in output.out + output.err
    payload = json.loads(output.out)
    assert payload == {
        "digest": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "home": str(configured[0].resolve()),
        "args": ["synthetic-argument"],
    }


@pytest.mark.parametrize("invalid", [
    "missing_home", "missing_config", "missing_skill", "missing_token",
    "empty_token", "invalid_token", "oversize_token", "invalid_origin",
])
def test_invalid_configuration_never_launches(configured, invalid, monkeypatch, capsys):
    home, token_file = configured
    origin = "http://127.0.0.1:8000"
    if invalid == "missing_home":
        home = home / "absent"
    elif invalid == "missing_config":
        (home / "config.yaml").unlink()
    elif invalid == "missing_skill":
        (home / "skills" / "vitalis" / "SKILL.md").unlink()
    elif invalid == "missing_token":
        token_file.unlink()
    elif invalid == "empty_token":
        token_file.write_text("\n", encoding="utf-8")
    elif invalid == "invalid_token":
        token_file.write_text(TOKEN + " invalid", encoding="utf-8")
    elif invalid == "oversize_token":
        token_file.write_text(TOKEN * 500, encoding="utf-8")
    else:
        origin = f"http://{TOKEN}@example.com/api?token={TOKEN}"
    monkeypatch.setattr(run_hermes.subprocess, "run", lambda *a, **k: pytest.fail("child launched"))
    assert run_hermes.main(arguments((home, token_file), origin)) != 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    payload = json.loads(output.err)
    assert payload["state"] == "failed"
    assert payload["failure_code"] in {"invalid_hermes_home", "invalid_access_token", "invalid_api_origin"}
    assert not (run_hermes.ROOT / ".tmp" / "hermes-launcher").exists()


def test_missing_executable_is_scrubbed_failure(configured, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError(TOKEN)

    monkeypatch.setattr(run_hermes.subprocess, "run", unavailable)
    assert run_hermes.main(arguments(configured)) == 127
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert json.loads(output.err)["failure_code"] == "hermes_command_unavailable"


def test_unexpected_launch_value_error_never_exposes_its_message(configured, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise ValueError(TOKEN)

    monkeypatch.setattr(run_hermes.subprocess, "run", unavailable)
    assert run_hermes.main(arguments(configured)) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert json.loads(output.err)["failure_code"] == "invalid_arguments"
