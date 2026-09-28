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
_ISOLATED_CLI = (
    "import dotenv; dotenv.dotenv_values=lambda *args, **kwargs: {}; "
    "from vitalis.entrypoints.cli import main; raise SystemExit(main())"
)


def _command(env, *args, cwd, isolated=False):
    command = (
        [sys.executable, "-B", "-c", _ISOLATED_CLI, *map(str, args)]
        if isolated else [sys.executable, "-m", "vitalis", *map(str, args)]
    )
    completed = subprocess.run(
        command,
        cwd=cwd, env=env, capture_output=True, text=True, timeout=75,
    )
    assert completed.returncode == 0, (args, completed.stdout, completed.stderr)
    return completed.stdout


def _port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


@contextmanager
def _service(env, *args, cwd, isolated=False):
    logfile = cwd / (args[0] + ".log")
    command = (
        [sys.executable, "-B", "-c", _ISOLATED_CLI, *args]
        if isolated else [sys.executable, "-m", "vitalis", *args]
    )
    with logfile.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command, cwd=cwd, env=env,
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


def _http(origin, token, path, *, body=None, key=None):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        origin + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _http_job(origin, token, job_id, kind):
    for _ in range(120):
        status_code, payload = _http(origin, token, f"/api/jobs/{job_id}")
        assert status_code == 200
        state = (
            payload["attempt"]["status"] if kind == "sync" else payload["status"]
        )
        if state in {"succeeded", "partial", "failed", "needs_reauth", "cancelled"}:
            return state, payload
        time.sleep(0.25)
    raise AssertionError(f"{kind} job never finished")


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


def test_fresh_mock_sync_then_morning_and_evening_without_demo(tmp_path):
    assert not tmp_path.is_relative_to(ROOT)
    day = "2026-08-29"  # Saturday exercises the optional workout-detail chunk.
    database = tmp_path / "briefing.db"
    port = _port()
    origin = f"http://127.0.0.1:{port}"
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("ZEPP_", "VITALIS_", "PUSHPLUS_"))
        and key not in {"DATABASE_URL", "PYTHONPATH"}
    }
    env.update({
        "DATABASE_URL": f"sqlite:///{database.as_posix()}",
        "ZEPP_MOCK": "true",
        "VITALIS_ENV": "test",
        "VITALIS_TIMEZONE": "UTC",
        "VITALIS_PUSH_USER": "",
        "PUSHPLUS_TOKEN": "",
        "VITALIS_TOKEN_ENCRYPTION_KEY": "",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SYNC_DISPATCHER_INTERVAL_SECONDS": "1",
    })
    _command(env, "db", "init", cwd=tmp_path, isolated=True)
    _command(env, "user", "create", "--id", "mock-briefing", cwd=tmp_path, isolated=True)
    token_file = tmp_path / "briefing.token"
    _command(
        env, "token", "issue", "--user", "mock-briefing",
        "--scope", "read", "--scope", "sync", "--scope", "analyze",
        "--expires-days", "1", "--output", token_file,
        cwd=tmp_path, isolated=True,
    )
    token = token_file.read_text(encoding="utf-8").strip()

    with _service(env, "serve", cwd=tmp_path, isolated=True) as api:
        _ready(origin, api)
        assert _http(origin, token, f"/api/reports/morning?day={day}")[0] == 404
        assert _http(origin, token, f"/api/reports/evening?day={day}")[0] == 404
        assert _http(origin, token, "/api/deliveries") == (200, [])
        with _service(env, "worker", cwd=tmp_path, isolated=True) as worker:
            sync_http, sync = _http(
                origin, token, "/api/sync-jobs",
                body={"from": day, "to": day}, key="mock-briefing-sync-20260829",
            )
            assert sync_http == 202
            sync_status, sync_job = _http_job(origin, token, sync["job_id"], "sync")
            assert worker.poll() is None and sync_status == "succeeded", sync_status
            chunks = sync_job["chunks"]
            assert len(chunks) >= 19
            assert all(row["status"] == "succeeded" for row in chunks)
            for stream in ("hrv", "daily_summary", "devices", "workouts"):
                assert any(
                    row["stream"] == stream and row["records_written"] > 0
                    for row in chunks
                ), stream
            assert any(row["stream"] == "workout_detail" for row in chunks)

            analysis_http, analysis = _http(
                origin, token, "/api/analysis-runs",
                body={"day": day}, key="mock-briefing-analysis-20260829",
            )
            assert analysis_http == 202
            analysis_status, analyzed = _http_job(origin, token, analysis["job_id"], "analysis")
            assert analysis_status == "succeeded", analysis_status
            morning_http, morning = _http(origin, token, f"/api/reports/morning?day={day}")
            evening_http, evening = _http(origin, token, f"/api/reports/evening?day={day}")
            assert morning_http == evening_http == 200
            assert morning["analysis_run_id"] == evening["analysis_run_id"] == analyzed["analysis_run_id"]
            assert morning["date"] == evening["date"] == day
            assert morning["sections"] and evening["sections"]
            assert _http(origin, token, "/api/deliveries") == (200, [])
