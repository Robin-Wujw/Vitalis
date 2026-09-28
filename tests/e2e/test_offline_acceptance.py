"""Fresh local database, actual API/worker processes, detached Skill, and restart."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).parents[2]
SKILL = ROOT / "skills" / "vitalis"


def _command(env, *args, cwd):
    completed = subprocess.run(
        [sys.executable, "-m", "vitalis", *map(str, args)],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=75,
    )
    assert completed.returncode == 0, (args, completed.stdout, completed.stderr)
    return completed.stdout


def _port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


@contextmanager
def _service(env, *args, cwd):
    logfile = cwd / (args[0] + ".log")
    with logfile.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "vitalis", *args], cwd=cwd, env=env,
            stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)


def _ready(origin, process):
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError(f"API exited before ready: {process.returncode}")
        try:
            with urllib.request.urlopen(origin + "/ready", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    raise AssertionError("API did not become ready")


def _skill(script, env, *args, body=None, cwd):
    completed = subprocess.run(
        [sys.executable, str(script), *map(str, args)], cwd=cwd, env=env,
        input=body, capture_output=True, text=True, timeout=25,
    )
    assert completed.returncode == 0, (args, completed.stdout, completed.stderr)
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def _job(script, env, job_id, cwd):
    for _ in range(120):
        status = _skill(script, env, "job", job_id, cwd=cwd)
        if status["status"] in {"succeeded", "partial", "failed", "needs_reauth"}:
            return status
        time.sleep(0.25)
    raise AssertionError(f"job {job_id} never finished")


def test_fresh_demo_http_skill_analysis_feedback_and_restart(tmp_path):
    assert not tmp_path.is_relative_to(ROOT), "end-to-end database must be outside repository"
    database = tmp_path / "fresh.db"
    day = datetime.now(timezone.utc).date().isoformat()
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "ZEPP_MOCK": "true",
        "VITALIS_ENV": "test",
        "VITALIS_TIMEZONE": "UTC",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SYNC_DISPATCHER_INTERVAL_SECONDS": "1",
        "VITALIS_PUSH_USER": "",
        "PUSHPLUS_TOKEN": "",
    }
    env.pop("PYTHONPATH", None)
    demo = json.loads(_command(env, "demo", "--database", database, "--day", day, cwd=tmp_path))
    assert demo["dataset"] == "synthetic_demo" and demo["analysis_run_id"]
    _command(env, "db", "init", cwd=tmp_path)
    assert json.loads(_command(env, "doctor", cwd=tmp_path))["schema"] == "current"
    token_file = tmp_path / "api.token"
    issued = _command(
        env, "token", "issue", "--user", "demo", "--scope", "read",
        "--scope", "analyze", "--scope", "feedback", "--scope", "sync",
        "--output", token_file, cwd=tmp_path,
    )
    token = token_file.read_text(encoding="utf-8").strip()
    assert token and token not in issued
    bundle = tmp_path / "detached" / "vitalis"
    shutil.copytree(SKILL, bundle, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    script = bundle / "scripts" / "vitalis_api.py"
    client_env = {**env, "VITALIS_API_BASE_URL": origin, "VITALIS_ACCESS_TOKEN": token}

    with _service(env, "serve", cwd=tmp_path) as api:
        _ready(origin, api)
        assert _skill(script, client_env, "status", cwd=tmp_path)["user_id"] == "demo"
        prior = _skill(script, client_env, "report", "daily", "--day", day, cwd=tmp_path)
        assert prior["analysis_run_id"] == demo["analysis_run_id"]
        with _service(env, "worker", cwd=tmp_path) as worker:
            queued = _skill(
                script, client_env, "analyze", "--day", day,
                "--key-file", tmp_path / "analysis-key.json", cwd=tmp_path,
            )
            assert queued["job_id"]
            done = _job(script, client_env, queued["job_id"], tmp_path)
            assert worker.poll() is None and done["status"] == "succeeded"
            current = _skill(script, client_env, "report", "daily", "--day", day, cwd=tmp_path)
            assert current["analysis_run_id"] == done["analysis_run_id"]
            assert current["analysis_run_id"] != prior["analysis_run_id"]
            feedback = _skill(
                script, client_env, "feedback", body=json.dumps({
                    "date": day, "notes": "synthetic offline feedback",
                }), cwd=tmp_path,
            )
            assert feedback["notes"] == "synthetic offline feedback"
            after_feedback = _skill(
                script, client_env, "analyze", "--day", day,
                "--key-file", tmp_path / "feedback-analysis-key.json", cwd=tmp_path,
            )
            changed = _job(script, client_env, after_feedback["job_id"], tmp_path)
            assert changed["status"] == "succeeded"
            assert changed["analysis_run_id"] != done["analysis_run_id"]
            persisted_id = changed["analysis_run_id"]

    with _service(env, "serve", cwd=tmp_path) as restarted:
        _ready(origin, restarted)
        restored = _skill(script, client_env, "report", "daily", "--day", day, cwd=tmp_path)
        assert restored["analysis_run_id"] == persisted_id
