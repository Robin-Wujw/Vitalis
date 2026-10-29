"""The local Hermes launcher keeps credentials in the child environment."""

import hashlib
from configparser import ConfigParser
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import run_hermes


TOKEN = "synthetic-hermes-private-token"
MODEL_KEY = "synthetic-private-model-api-key"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    home = tmp_path / "private hermes"
    skill = home / "skills" / "vitalis"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("synthetic installed skill\n", encoding="utf-8")
    (home / "config.yaml").write_text("model: synthetic\n", encoding="utf-8")
    token_file = tmp_path / "private.token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o640)
    monkeypatch.setattr(run_hermes, "ROOT", tmp_path / "checkout")
    return home, token_file


def arguments(configured, origin="http://127.0.0.1:8000"):
    home, token_file = configured
    return ["--home", str(home), "--token-file", str(token_file),
            "--api-base-url", origin, "--hermes-command", sys.executable]


def test_launch_injects_private_environment_and_preserves_failure(configured, monkeypatch, capsys):
    home, _ = configured
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", "stale-inherited-token")
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN_FILE", "stale-relative-path")
    monkeypatch.setenv("VITALIS_API", "legacy")
    monkeypatch.setenv("VITALIS_USER", "legacy-user")
    monkeypatch.setenv("HERMES_LAUNCH_TEST", "inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-model-key")
    calls = []

    def child(command, *, cwd, env, check):
        calls.append(command)
        assert command == [sys.executable, "gateway", "run"]
        assert TOKEN not in repr(command)
        assert cwd == home.resolve()
        assert env["HERMES_HOME"] == str(home.resolve())
        assert "VITALIS_ACCESS_TOKEN" not in env
        assert env["VITALIS_ACCESS_TOKEN_FILE"] == str(configured[1].resolve())
        assert env["VITALIS_API_BASE_URL"] == "http://127.0.0.1:8000"
        assert env["HERMES_LAUNCH_TEST"] == "inherited"
        assert env["OPENAI_API_KEY"] == "inherited-model-key"
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


def test_real_child_receives_both_credentials_without_secrets_in_command(configured, capfd):
    model_file = configured[1].with_name("model-api.key")
    model_file.write_text(MODEL_KEY + "\n", encoding="utf-8")
    script = (
        "import hashlib,json,os,sys; "
        "print(json.dumps({'digest':hashlib.sha256(__import__('pathlib').Path(os.environ['VITALIS_ACCESS_TOKEN_FILE']).read_bytes().strip()).hexdigest(),"
        "'model_digest':hashlib.sha256(os.environ['OPENAI_API_KEY'].encode()).hexdigest(),"
        "'home':os.getcwd(),'args':sys.argv[1:]})); sys.exit(17)"
    )
    command = arguments(configured) + ["--model-key-file", str(model_file),
                                       "--", "-c", script, "synthetic-argument"]
    assert TOKEN not in repr(command) and MODEL_KEY not in repr(command)
    assert run_hermes.main(command) == 17
    output = capfd.readouterr()
    assert TOKEN not in output.out + output.err and MODEL_KEY not in output.out + output.err
    payload = json.loads(output.out)
    assert payload == {
        "digest": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "model_digest": hashlib.sha256(MODEL_KEY.encode()).hexdigest(),
        "home": str(configured[0].resolve()),
        "args": ["synthetic-argument"],
    }


def test_model_key_overrides_only_child_environment(configured, monkeypatch, capsys):
    model_file = configured[1].with_name("model-api.key")
    model_file.write_text(MODEL_KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-model-key")
    calls = []

    def child(command, *, cwd, env, check):
        calls.append(command)
        assert env["OPENAI_API_KEY"] == MODEL_KEY
        assert "VITALIS_ACCESS_TOKEN" not in env
        assert env["VITALIS_ACCESS_TOKEN_FILE"] == str(configured[1].resolve())
        assert TOKEN not in repr(command) and MODEL_KEY not in repr(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_hermes.subprocess, "run", child)
    assert run_hermes.main(arguments(configured) + ["--model-key-file", str(model_file),
                                                   "--", "gateway", "run"]) == 0
    assert calls
    assert os.environ["OPENAI_API_KEY"] == "ambient-model-key"
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and MODEL_KEY not in output.out + output.err


@pytest.mark.parametrize("content", [None, "\n", MODEL_KEY + " invalid", "非ASCII", MODEL_KEY * 500],
                         ids=["missing", "blank", "whitespace", "non-ascii", "oversize"])
def test_invalid_model_key_never_launches(configured, content, monkeypatch, capsys):
    model_file = configured[1].with_name("model-api.key")
    if content is not None:
        model_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(run_hermes.subprocess, "run", lambda *a, **k: pytest.fail("child launched"))
    assert run_hermes.main(arguments(configured) + ["--model-key-file", str(model_file)]) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and MODEL_KEY not in output.out + output.err
    assert json.loads(output.err)["failure_code"] == "invalid_model_key"
    assert not (run_hermes.ROOT / ".tmp" / "hermes-launcher").exists()


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


def test_relative_token_path_is_absolute_in_child(configured, monkeypatch):
    home, token_file = configured
    monkeypatch.chdir(token_file.parent)

    def child(command, *, cwd, env, check):
        assert env["VITALIS_ACCESS_TOKEN_FILE"] == str(token_file)
        assert "VITALIS_ACCESS_TOKEN" not in env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_hermes.subprocess, "run", child)
    assert run_hermes.main(arguments((home, Path(token_file.name)))) == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX credential permissions")
def test_unprotected_token_never_launches(configured, monkeypatch, capsys):
    configured[1].chmod(0o644)
    monkeypatch.setattr(run_hermes.subprocess, "run", lambda *a, **k: pytest.fail("child launched"))
    assert run_hermes.main(arguments(configured)) == 2
    assert json.loads(capsys.readouterr().err)["failure_code"] == "invalid_access_token"


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


def test_exec_replaces_process_with_private_environment(configured, monkeypatch, capsys):
    home, token_file = configured
    model_file = token_file.with_name("model-api.key")
    model_file.write_text(MODEL_KEY + "\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-model-key")
    changed_directory = []

    class Replaced(Exception):
        pass

    def replace(executable, command, environment):
        assert changed_directory == [home.resolve()]
        assert executable == sys.executable
        assert command == [sys.executable, "gateway", "run", "--external-supervisor"]
        assert TOKEN not in repr(command) and MODEL_KEY not in repr(command)
        assert "VITALIS_ACCESS_TOKEN" not in environment
        assert environment["VITALIS_ACCESS_TOKEN_FILE"] == str(token_file.resolve())
        assert environment["OPENAI_API_KEY"] == MODEL_KEY
        assert environment["HERMES_HOME"] == str(home.resolve())
        raise Replaced

    monkeypatch.setattr(run_hermes, "os", SimpleNamespace(
        name="posix", environ=os.environ, chdir=changed_directory.append, execvpe=replace,
    ))
    monkeypatch.setattr(run_hermes.subprocess, "run", lambda *a, **kw: pytest.fail("child spawned"))
    with pytest.raises(Replaced):
        run_hermes.main(arguments(configured) + ["--model-key-file", str(model_file), "--exec",
                                                "--", "gateway", "run", "--external-supervisor"])
    assert os.environ["OPENAI_API_KEY"] == "ambient-model-key"
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err and MODEL_KEY not in output.out + output.err


def test_exec_requires_posix_without_starting_child(configured, monkeypatch, capsys):
    monkeypatch.setattr(run_hermes, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(run_hermes.subprocess, "run", lambda *a, **kw: pytest.fail("child spawned"))
    assert run_hermes.main(arguments(configured) + ["--exec"]) == 2
    assert json.loads(capsys.readouterr().err)["failure_code"] == "exec_requires_posix"
    assert not (run_hermes.ROOT / ".tmp").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX exec preserves the supervised PID")
def test_real_exec_preserves_pid_credentials_and_exit_status(configured):
    home, token_file = configured
    model_file = token_file.with_name("model-api.key")
    model_file.write_text(MODEL_KEY + "\n", encoding="utf-8")
    bootstrap = (
        "import sys; from pathlib import Path; from tools import run_hermes; "
        "run_hermes.ROOT=Path(sys.argv[1]); raise SystemExit(run_hermes.main(sys.argv[2:]))"
    )
    child = (
        "import hashlib,json,os,sys; print(json.dumps({'pid':os.getpid(),'home':os.getcwd(),"
        "'token':hashlib.sha256(__import__('pathlib').Path(os.environ['VITALIS_ACCESS_TOKEN_FILE']).read_bytes().strip()).hexdigest(),"
        "'model':hashlib.sha256(os.environ['OPENAI_API_KEY'].encode()).hexdigest()})); sys.exit(23)"
    )
    command = [sys.executable, "-B", "-c", bootstrap, str(run_hermes.ROOT),
               *arguments(configured), "--model-key-file", str(model_file), "--exec", "--", "-c", child]
    assert TOKEN not in repr(command) and MODEL_KEY not in repr(command)
    with subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2],
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 23
        assert errors == "" and TOKEN not in output and MODEL_KEY not in output
        assert json.loads(output) == {
            "pid": process.pid, "home": str(home.resolve()),
            "token": hashlib.sha256(TOKEN.encode()).hexdigest(),
            "model": hashlib.sha256(MODEL_KEY.encode()).hexdigest(),
        }


def test_linux_unit_supervises_gateway_pid_with_private_state():
    unit = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "vitalis-hermes.service"
    config = ConfigParser(interpolation=None, strict=False)
    assert config.read(unit, encoding="utf-8")
    service = config["Service"]
    assert service["User"] == service["Group"] == "vitalis-hermes"
    assert service["StateDirectory"] == "vitalis-hermes" and service["StateDirectoryMode"] == "0700"
    assert service["Type"] == "simple"
    command = shlex.split(service["ExecStart"])
    separator = command.index("--")
    assert "--exec" in command[:separator]
    assert command[separator + 1:] == ["gateway", "run", "--external-supervisor"]
    assert command[command.index("--hermes-command") + 1] == "/opt/hermes/current/venv/bin/hermes"
    assert "--token-file" in command and "--model-key-file" in command
    assert service["KillMode"] == "mixed" and service["KillSignal"] == "SIGTERM"
    assert service["ExecStop"] == "/opt/hermes/current/venv/bin/python -m gateway.systemd_stop_mark"
    assert int(service["TimeoutStopSec"]) >= 70
    assert service["RestartForceExitStatus"] == service["SuccessExitStatus"] == "75"
    assert service["RestartPreventExitStatus"] == "78"
    assert service["ProtectSystem"] == "strict" and service["NoNewPrivileges"] == "true"
    assert "/var/lib/vitalis-hermes" in service["ReadWritePaths"]
    assert "/opt/vitalis/current/.tmp" in service["ReadWritePaths"]
    assert "HERMES_HOME=/var/lib/vitalis-hermes" in unit.read_text(encoding="utf-8")
    assert not config.has_option("Service", "EnvironmentFile")
