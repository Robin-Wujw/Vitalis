"""Exercise the copied Skill client against an in-process current-API mock."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "vitalis"
TOKEN = "synthetic-private-bearer"


@pytest.fixture
def installed(tmp_path):
    bundle = tmp_path / "detached" / "vitalis"
    assert not bundle.is_relative_to(ROOT)
    shutil.copytree(SKILL, bundle, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return bundle / "scripts" / "vitalis_api.py"


@contextmanager
def mock_api():
    calls = []
    responses = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_PATCH(self):
            self.handle_request()

        def do_PUT(self):
            self.handle_request()

        def handle_request(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size) if size else b""
            calls.append((self.command, self.path, dict(self.headers), body))
            path = urlsplit(self.path).path
            queued = responses.get((self.command, path), [])
            if queued:
                status, data, headers = queued.pop(0)
            elif self.headers.get("Authorization") != "Bearer " + TOKEN:
                status, data, headers = 401, {"detail": "bad auth"}, {}
            elif path == "/api/data-status":
                status, data, headers = 200, {"status": "available"}, {}
            elif path == "/api/reports/daily":
                status, data, headers = 200, {"date": "2026-09-20", "facts": {}}, {}
            elif path == "/api/workouts":
                status, data, headers = 200, {"workouts": []}, {}
            elif path == "/api/workouts/run-1":
                status, data, headers = 200, {"workout_id": "run-1"}, {}
            elif path == "/api/jobs/job-1":
                status, data, headers = 200, {"job_id": "job-1", "status": "queued"}, {}
            elif path in ("/api/analysis-runs", "/api/sync-jobs"):
                status, data, headers = 202, {"job_id": "job-1", "status": "queued"}, {}
            elif path == "/api/feedback":
                status, data, headers = 201, {"id": "feedback-1"}, {}
            else:
                status, data, headers = 404, {"detail": "Not Found"}, {}
            encoded = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def invoke(script, base, *args, token=TOKEN, body=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("VITALIS_")}
    env.pop("PYTHONPATH", None)
    env.update(VITALIS_API_BASE_URL=base, VITALIS_ACCESS_TOKEN=token)
    process = subprocess.run(
        [sys.executable, str(script), *args], cwd=script.parents[2], env=env,
        input=body, capture_output=True, text=True, timeout=15,
    )
    assert process.stderr == ""
    if token:
        assert token not in process.stdout
    return process.returncode, json.loads(process.stdout)


def test_read_operations_and_bearer_only(installed):
    with mock_api() as (base, calls, responses):
        assert invoke(installed, base, "status") == (0, {"status": "available"})
        assert invoke(installed, base, "report", "daily", "--day", "2026-09-20")[0] == 0
        assert invoke(installed, base, "workouts", "--from", "2026-09-01", "--limit", "2") == (
            0, {"workouts": []},
        )
        assert invoke(installed, base, "workouts", "--id", "run-1", "--source", "zepp") == (
            0, {"workout_id": "run-1"},
        )
        assert invoke(installed, base, "job", "job-1") == (
            0, {"job_id": "job-1", "status": "queued"},
        )
        assert [urlsplit(call[1]).path for call in calls] == [
            "/api/data-status", "/api/reports/daily", "/api/workouts",
            "/api/workouts/run-1", "/api/jobs/job-1",
        ]
        assert parse_qs(urlsplit(calls[1][1]).query) == {"day": ["2026-09-20"]}
        assert parse_qs(urlsplit(calls[2][1]).query) == {"from": ["2026-09-01"], "limit": ["2"]}
        assert parse_qs(urlsplit(calls[3][1]).query) == {"source": ["zepp"]}
        assert all(call[0] == "GET" for call in calls)
        assert all(call[2]["Authorization"] == "Bearer " + TOKEN for call in calls)
        assert all("X-User-Id" not in call[2] for call in calls)

        responses[("GET", "/api/reports/daily")] = [(404, {"detail": "no snapshot"}, {})]
        assert invoke(installed, base, "report", "daily", "--day", "2026-09-21") == (
            0, {"status": "snapshot_missing", "http_status": 404},
        )
        responses[("GET", "/api/jobs/not-found")] = [(404, {"detail": "no job"}, {})]
        assert invoke(installed, base, "job", "not-found") == (
            1, {"status": "error", "error": "http_error", "http_status": 404},
        )


def test_allowlisted_extended_read_and_user_actions(installed):
    with mock_api() as (base, calls, responses):
        responses[("GET", "/api/intelligence/trends")] = [(200, {"trends": []}, {})]
        assert invoke(installed, base, "query", "trends", "--day", "2026-09-20") == (0, {"trends": []})
        assert parse_qs(urlsplit(calls[-1][1]).query) == {"day": ["2026-09-20"]}
        count = len(calls)
        assert invoke(installed, base, "query", "unknown")[0] == 1
        assert invoke(installed, base, "query", "profile", "--day", "2026-09-20")[0] == 1
        assert len(calls) == count

        responses[("PATCH", "/api/intelligence/profile")] = [(200, {"revision": 2}, {})]
        payload = {"expected_revision": 1, "sex": "female"}
        assert invoke(installed, base, "action", "profile-patch", body=json.dumps(payload)) == (
            0, {"revision": 2},
        )
        assert calls[-1][0] == "PATCH"
        assert json.loads(calls[-1][3]) == payload
        responses[("POST", "/api/intelligence/workouts/run-1/strength-exercises")] = [
            (201, {"confirmed": True}, {})
        ]
        assert invoke(installed, base, "action", "strength-confirm", "--id", "run-1",
                      "--source", "zepp", body='{"exercises":[]}')[0] == 0
        assert parse_qs(urlsplit(calls[-1][1]).query) == {"source": ["zepp"]}
        responses[("POST", "/api/intelligence/events/event-1/acknowledge")] = [
            (200, {"acknowledged": True}, {})
        ]
        assert invoke(installed, base, "action", "event-acknowledge", "--id", "event-1") == (
            0, {"acknowledged": True},
        )
        assert all(call[2]["Authorization"] == "Bearer " + TOKEN for call in calls)


def test_queued_writes_persist_keys_and_bind_request(installed, tmp_path):
    with mock_api() as (base, calls, _):
        analysis_file = tmp_path / "analysis-key.json"
        args = ("analyze", "--day", "2026-09-20", "--key-file", str(analysis_file))
        assert invoke(installed, base, *args)[0] == 0
        assert analysis_file.is_file()
        assert invoke(installed, base, *args)[0] == 0
        assert calls[0][2]["Idempotency-Key"] == calls[1][2]["Idempotency-Key"]
        assert len(calls[0][2]["Idempotency-Key"]) >= 16
        assert json.loads(calls[0][3]) == {"day": "2026-09-20"}
        assert invoke(installed, base, "analyze", "--day", "2026-09-21",
                      "--key-file", str(analysis_file)) == (
            1, {"status": "error", "error": "idempotency_key_mismatch"},
        )
        assert len(calls) == 2
        assert invoke(installed, base, "sync", "--key-file", str(analysis_file))[0] == 1
        assert len(calls) == 2

        sync_file = tmp_path / "sync-key.json"
        sync_args = ("sync", "--days", "3", "--detail-only", "--detail-limit", "2",
                     "--key-file", str(sync_file))
        assert invoke(installed, base, *sync_args)[0] == 0
        assert invoke(installed, base, *sync_args)[0] == 0
        assert calls[2][2]["Idempotency-Key"] == calls[3][2]["Idempotency-Key"]
        assert calls[2][2]["Idempotency-Key"] != calls[0][2]["Idempotency-Key"]
        assert json.loads(calls[2][3])["detail_limit"] == 2
        assert len(calls) == 4
        refresh_file = tmp_path / "refresh-key.json"
        assert invoke(installed, base, "sync", "--detail-only",
                      "--detail-refresh-before", "2026-09-01T00:00:00Z",
                      "--key-file", str(refresh_file))[0] == 0
        assert json.loads(calls[4][3])["detail_refresh_before"] == "2026-09-01T00:00:00Z"
        assert invoke(installed, base, "sync", "--detail-only",
                      "--detail-refresh-before", "2026-09-01",
                      "--key-file", str(tmp_path / "invalid-key.json"))[0] == 1
        assert len(calls) == 5
        if os.name != "nt":
            assert analysis_file.stat().st_mode & 0o077 == 0


def test_feedback_stdin_and_writes_do_not_retry(installed, tmp_path):
    with mock_api() as (base, calls, responses):
        body = {"notes": "synthetic example", "workout_id": "w1", "workout_source": "zepp"}
        assert invoke(installed, base, "feedback", body=json.dumps(body)) == (
            0, {"id": "feedback-1"},
        )
        assert calls[0][0:2] == ("POST", "/api/feedback")
        assert json.loads(calls[0][3]) == body
        assert "Idempotency-Key" not in calls[0][2]
        count = len(calls)
        assert invoke(installed, base, "feedback", body="not json")[0] == 1
        assert len(calls) == count

        responses[("POST", "/api/feedback")] = [(503, {"detail": TOKEN}, {})]
        assert invoke(installed, base, "feedback", body=json.dumps(body)) == (
            1, {"status": "error", "error": "http_error", "http_status": 503},
        )
        assert len(calls) == count + 1
        responses[("POST", "/api/analysis-runs")] = [(503, {"detail": TOKEN}, {}),
                                                          (202, {"job_id": "duplicate"}, {})]
        analysis_file = tmp_path / "retry-key.json"
        assert invoke(installed, base, "analyze", "--day", "2026-09-20",
                      "--key-file", str(analysis_file))[0] == 1
        assert analysis_file.is_file()
        assert len(calls) == count + 2
        assert len(responses[("POST", "/api/analysis-runs")]) == 1


def test_feedback_key_file_is_durable_and_never_triggers_an_automatic_retry(installed, tmp_path):
    with mock_api() as (base, calls, responses):
        key_file = tmp_path / "feedback-key.json"
        args = ("feedback", "--key-file", str(key_file))
        payload = {"date": "2026-09-20", "notes": "synthetic private note"}
        responses[("POST", "/api/feedback")] = [
            (503, {"detail": "unavailable"}, {}),
            (201, {"id": "first-feedback"}, {}),
        ]
        assert invoke(installed, base, *args, body=json.dumps(payload)) == (
            1, {"status": "error", "error": "http_error", "http_status": 503},
        )
        assert key_file.is_file()
        assert "private note" not in key_file.read_text(encoding="utf-8")
        assert len(calls) == 1
        assert len(responses[("POST", "/api/feedback")]) == 1

        assert invoke(installed, base, *args, body=json.dumps(payload)) == (
            0, {"id": "first-feedback"},
        )
        assert len(calls) == 2
        assert calls[0][2]["Idempotency-Key"] == calls[1][2]["Idempotency-Key"]
        assert json.loads(calls[1][3]) == payload
        assert invoke(installed, base, *args, body=json.dumps({**payload, "notes": "changed"})) == (
            1, {"status": "error", "error": "idempotency_key_mismatch"},
        )
        assert invoke(installed, base, "sync", "--key-file", str(key_file))[0] == 1
        assert len(calls) == 2

        invalid_file = tmp_path / "invalid-feedback-key.json"
        assert invoke(installed, base, "feedback", "--key-file", str(invalid_file),
                      body="not JSON")[0] == 1
        assert not invalid_file.exists()
        assert len(calls) == 2


def test_errors_get_retry_and_cross_origin_redirect(installed):
    with mock_api() as (base, calls, responses), mock_api() as (other_base, other_calls, _):
        responses[("GET", "/api/data-status")] = [
            (503, {"detail": TOKEN}, {}), (200, {"status": "recovered"}, {}),
        ]
        assert invoke(installed, base, "status") == (0, {"status": "recovered"})
        assert len(calls) == 2
        responses[("GET", "/api/data-status")] = [
            (503, {"detail": "busy"}, {}), (503, {"detail": "busy"}, {}),
            (200, {"status": "should not reach"}, {}),
        ]
        assert invoke(installed, base, "status") == (
            1, {"status": "error", "error": "http_error", "http_status": 503},
        )
        assert len(calls) == 4
        responses[("GET", "/api/data-status")] = [
            (302, {"detail": TOKEN}, {"Location": other_base + "/api/data-status"}),
        ]
        assert invoke(installed, base, "status") == (
            1, {"status": "error", "error": "redirect_blocked", "http_status": 302},
        )
        assert len(calls) == 5 and other_calls == []
        responses[("GET", "/api/data-status")] = [(401, {"detail": TOKEN}, {})]
        assert invoke(installed, base, "status") == (
            1, {"status": "error", "error": "unauthorized", "http_status": 401},
        )
        responses[("GET", "/api/data-status")] = [(403, {"detail": TOKEN}, {})]
        assert invoke(installed, base, "status") == (
            1, {"status": "error", "error": "forbidden", "http_status": 403},
        )
        responses[("GET", "/api/data-status")] = [
            (200, {"status": "ok", "nested": {"text": TOKEN}}, {}),
        ]
        assert invoke(installed, base, "status") == (
            0, {"status": "ok", "nested": {"text": "[redacted]"}},
        )
        responses[("GET", "/api/data-status")] = [(200, {"number": float("inf")}, {})]
        assert invoke(installed, base, "status") == (
            1, {"status": "error", "error": "invalid_response_json"},
        )


@pytest.mark.parametrize("origin", [
    "http://user:pass@127.0.0.1:8765", "http://127.0.0.1:8765/api",
    "http://127.0.0.1:8765?token=bad", "http://127.0.0.1:8765#frag",
    "http://127.0.0.1:8765?", "http://127.0.0.1:8765#",
    "http://example.com", "https://example.com:bad", "https://example.com:65536",
    "not-a-url",
])
def test_invalid_origin_never_sends_request(installed, origin):
    code, output = invoke(installed, origin, "status")
    assert code == 1
    assert output == {"status": "error", "error": "invalid_api_origin"}


def test_invalid_arguments_and_missing_token_are_safe(installed):
    with mock_api() as (base, calls, _):
        assert invoke(installed, base, "status", token="")[1] == {
            "status": "error", "error": "invalid_access_token",
        }
        assert invoke(installed, base, "report", "unexpected", token=TOKEN)[1] == {
            "status": "error", "error": "invalid_arguments",
        }
        assert invoke(installed, base, "workouts", "--id", "x")[0] == 1
        assert invoke(installed, base, "feedback", body='{"notes":NaN}')[1] == {
            "status": "error", "error": "invalid_feedback_json",
        }
        assert calls == []
