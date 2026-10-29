"""Exercise the copied Skill client against an in-process current-API mock."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import runpy
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
    # Outside tracked files: only the git-ignored .tmp/ scratch area is allowed.
    assert not bundle.is_relative_to(ROOT) or bundle.is_relative_to(ROOT / ".tmp")
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


def invoke(script, base, *args, token=TOKEN, body=None, token_file=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("VITALIS_")}
    env.pop("PYTHONPATH", None)
    env.update(VITALIS_API_BASE_URL=base, VITALIS_ACCESS_TOKEN=token)
    if token_file is not None:
        env["VITALIS_ACCESS_TOKEN_FILE"] = str(token_file)
    process = subprocess.run(
        [sys.executable, str(script), *args], cwd=script.parents[2], env=env,
        input=body, capture_output=True, text=True, timeout=15,
    )
    assert process.stderr == ""
    if token:
        assert token not in process.stdout
    return process.returncode, json.loads(process.stdout)


def test_access_token_hot_reloads_atomic_replacement(installed, tmp_path, monkeypatch):
    client = runpy.run_path(str(installed))
    token_file = tmp_path / "access.token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o640)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", "stale-environment-token")
    assert client["access_token"]() == TOKEN
    replacement = tmp_path / "replacement.token"
    replacement.write_text("synthetic-renewed-token\n", encoding="ascii")
    replacement.chmod(0o640)
    replacement.replace(token_file)
    assert client["access_token"]() == "synthetic-renewed-token"
    token_file.unlink()
    with pytest.raises(client["ClientError"]) as error:
        client["access_token"]()
    assert error.value.code == "invalid_access_token"


@pytest.mark.parametrize("invalid", ["empty-path", "relative", "missing", "directory", "blank",
                                        "non-ascii", "oversize", "internal-space", "nul"])
def test_token_file_fails_closed(installed, tmp_path, monkeypatch, invalid):
    client = runpy.run_path(str(installed))
    path = tmp_path / "access.token"
    contents = {"blank": b"\n", "non-ascii": b"\xff", "oversize": b"a" * 4097,
                "internal-space": b"first second", "nul": b"first\x00second"}
    if invalid in contents:
        path.write_bytes(contents[invalid])
        path.chmod(0o600)
    elif invalid == "directory":
        path.mkdir()
    value = "" if invalid == "empty-path" else "access.token" if invalid == "relative" else str(path)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN_FILE", value)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", TOKEN)
    with pytest.raises(client["ClientError"]) as error:
        client["access_token"]()
    assert error.value.as_json() == {"status": "error", "error": "invalid_access_token"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX credential file permissions")
@pytest.mark.parametrize("mode", [0o644, 0o660, 0o650, 0o604])
def test_token_file_rejects_unprotected_permissions(installed, tmp_path, monkeypatch, mode):
    client = runpy.run_path(str(installed))
    path = tmp_path / "access.token"
    path.write_text(TOKEN, encoding="ascii")
    path.chmod(mode)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN_FILE", str(path))
    with pytest.raises(client["ClientError"]):
        client["access_token"]()


def test_access_token_environment_mode_without_file(installed, monkeypatch):
    client = runpy.run_path(str(installed))
    monkeypatch.delenv("VITALIS_ACCESS_TOKEN_FILE", raising=False)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN", TOKEN)
    assert client["access_token"]() == TOKEN


def test_detached_file_credentials_send_bearer_and_fail_before_http(installed, tmp_path):
    path = tmp_path / "private.token"
    path.write_text(TOKEN + "\n", encoding="ascii")
    path.chmod(0o640)
    with mock_api() as (base, calls, _):
        assert invoke(installed, base, "status", token="stale-token", token_file=path) == (
            0, {"status": "available"},
        )
        assert calls[0][2]["Authorization"] == "Bearer " + TOKEN
        path.unlink()
        assert invoke(installed, base, "status", token_file=path) == (
            1, {"status": "error", "error": "invalid_access_token"},
        )
        assert len(calls) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink and FIFO protection")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_token_file_rejects_special_paths(installed, tmp_path, monkeypatch, kind):
    client = runpy.run_path(str(installed))
    path = tmp_path / "access.token"
    if kind == "symlink":
        target = tmp_path / "target.token"
        target.write_text(TOKEN, encoding="ascii")
        target.chmod(0o600)
        path.symlink_to(target)
    else:
        os.mkfifo(path, 0o600)
    monkeypatch.setenv("VITALIS_ACCESS_TOKEN_FILE", str(path))
    with pytest.raises(client["ClientError"]):
        client["access_token"]()


@pytest.mark.parametrize("limit", [None, "1", "20"])
def test_strength_detail_list_is_one_bounded_read(installed, limit):
    with mock_api() as (base, calls, responses):
        expected = {"workouts": [
            {"workout_id": "latest", "strength_details": {"state": "missing", "strength_sets": []}},
            {"workout_id": "older", "strength_details": {"state": "available", "strength_sets": [
                {"source": "lap_62", "exercise_name": None, "weight_value": None,
                 "weight_unit": None, "weight_kg": None, "limitations": ["unknown_weight_unit"]},
            ]}},
        ]}
        responses[("GET", "/api/workouts")] = [(200, expected, {})]
        arguments = ["workouts", "--strength-details", "--from", "2026-08-01"]
        if limit is not None:
            arguments += ["--limit", limit]
        assert invoke(installed, base, *arguments) == (0, expected)
        assert len(calls) == 1 and calls[0][0] == "GET"
        assert parse_qs(urlsplit(calls[0][1]).query) == {
            "from": ["2026-08-01"], "limit": [limit or "20"],
            "include_strength_details": ["true"],
        }


@pytest.mark.parametrize("arguments", [
    ["--strength-details", "--limit", "21"],
    ["--strength-details", "--limit", "0"],
    ["--strength-details", "--id", "run-1", "--source", "zepp"],
    ["--samples"],
    ["--strength-details", "--samples"],
])
def test_invalid_workout_expansions_do_not_send_requests(installed, arguments):
    with mock_api() as (base, calls, _responses):
        assert invoke(installed, base, "workouts", *arguments) == (
            1, {"status": "error", "error": "invalid_arguments"},
        )
        assert calls == []


def test_detail_samples_are_explicit_client_opt_in(installed):
    with mock_api() as (base, calls, _responses):
        assert invoke(installed, base, "workouts", "--id", "run-1", "--source", "zepp", "--samples")[0] == 0
        assert parse_qs(urlsplit(calls[0][1]).query) == {"source": ["zepp"], "include_samples": ["true"]}


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
        assert parse_qs(urlsplit(calls[3][1]).query) == {"source": ["zepp"], "include_samples": ["false"]}
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


def test_report_state_reads_saved_status_without_starting_analysis(installed):
    with mock_api() as (base, calls, responses):
        state = {
            "state": "queued", "job_id": "job-1", "failure_code": None,
            "last_good_snapshot": {"analysis_run_id": "saved-run"},
            "stale_since": "2026-09-20T13:00:00Z", "next_action": "wait_for_analysis",
        }
        responses[("GET", "/api/reports/daily/state")] = [(200, state, {})]
        assert invoke(installed, base, "report", "daily", "--day", "2026-09-20", "--state") == (0, state)
        assert len(calls) == 1
        assert calls[0][0] == "GET"
        assert urlsplit(calls[0][1]).path == "/api/reports/daily/state"
        assert parse_qs(urlsplit(calls[0][1]).query) == {"day": ["2026-09-20"]}


def test_product_reads_public_view_progress_and_explicit_keyed_actions(installed, tmp_path):
    with mock_api() as (base, calls, responses):
        responses.update({
            ("GET", "/api/product/goals"): [(200, {"goals": []}, {})],
            ("GET", "/api/product/feedback"): [(200, [], {})],
            ("GET", "/api/product/summary"): [(200, {"goals": []}, {})],
            ("GET", "/api/product/metrics"): [(200, {"coverage": {}}, {})],
            ("GET", "/api/product/context"): [(200, {"user_id": "synthetic"}, {})],
            ("GET", "/api/connect/zepp/progress"): [(200, {"state": "connected"}, {})],
            ("GET", "/api/reports/daily/view"): [(200, {"blocks": []}, {})],
            ("POST", "/api/product/goals"): [
                (201, {"goal": {"id": "goal-1"}}, {}),
                (201, {"goal": {"id": "goal-1"}}, {}),
            ],
            ("PATCH", "/api/product/goals/goal-1"): [(200, {"goal": {"id": "goal-1"}}, {})],
            ("POST", "/api/product/feedback"): [(201, {"feedback": {"id": "feedback-1"}}, {})],
        })
        assert invoke(installed, base, "query", "product-goals") == (0, {"goals": []})
        assert invoke(installed, base, "query", "product-feedback", "--start", "2026-09-01", "--end", "2026-09-20", "--limit", "10") == (0, [])
        assert invoke(installed, base, "query", "product-summary", "--start", "2026-09-01", "--end", "2026-09-20") == (0, {"goals": []})
        assert invoke(installed, base, "query", "product-metrics", "--start", "2026-09-01", "--end", "2026-09-20") == (0, {"coverage": {}})
        assert invoke(installed, base, "query", "product-context", "--day", "2026-09-20") == (0, {"user_id": "synthetic"})
        assert invoke(installed, base, "query", "connection-progress") == (0, {"state": "connected"})
        assert invoke(installed, base, "report", "daily", "--day", "2026-09-20", "--view") == (0, {"blocks": []})
        assert invoke(installed, base, "report", "daily", "--state", "--view")[1] == {
            "status": "error", "error": "invalid_arguments",
        }

        body = {
            "confirmed": True, "goal_type": "sleep_duration", "target_value": 450,
            "target_unit": "min", "target_date": "2026-10-20",
        }
        goal_key = tmp_path / "goal-create-key.json"
        assert invoke(installed, base, "action", "goal-create", "--key-file", str(goal_key), body=json.dumps(body)) == (
            0, {"goal": {"id": "goal-1"}},
        )
        assert calls[-1][0:2] == ("POST", "/api/product/goals")
        assert json.loads(calls[-1][3]) == body
        assert calls[-1][2]["Idempotency-Key"]
        record = json.loads(goal_key.read_text(encoding="utf-8"))
        assert record["operation"] == "POST /api/product/goals"
        assert record["request_sha256"] == __import__("hashlib").sha256(json.dumps(
            {"method": "POST", "path": "/api/product/goals", "body": body},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        assert invoke(installed, base, "action", "goal-create", "--key-file", str(goal_key), body=json.dumps(body)) == (
            0, {"goal": {"id": "goal-1"}},
        )

        patch_body = {"confirmed": True, "expected_revision": 1, "target_value": 460}
        patch_key = tmp_path / "goal-patch-key.json"
        assert invoke(installed, base, "action", "goal-patch", "--id", "goal-1", "--key-file", str(patch_key), body=json.dumps(patch_body))[0] == 0
        assert calls[-1][0:2] == ("PATCH", "/api/product/goals/goal-1")
        assert json.loads(calls[-1][3]) == patch_body

        feedback_body = {"confirmed": True, "kind": "report_usefulness", "report_run_id": "run-1", "usefulness": "useful"}
        feedback_key = tmp_path / "product-feedback-key.json"
        assert invoke(installed, base, "action", "product-feedback", "--key-file", str(feedback_key), body=json.dumps(feedback_body))[0] == 0
        assert calls[-1][0:2] == ("POST", "/api/product/feedback")
        assert json.loads(calls[-1][3]) == feedback_body

        responses[("POST", "/api/product/feedback")] = [(503, {"detail": "busy"}, {}), (201, {"feedback": {"id": "retry"}}, {})]
        no_retry_key = tmp_path / "product-feedback-no-retry.json"
        assert invoke(installed, base, "action", "product-feedback", "--key-file", str(no_retry_key), body=json.dumps(feedback_body)) == (
            1, {"status": "error", "error": "http_error", "http_status": 503},
        )
        assert calls[-1][0:2] == ("POST", "/api/product/feedback")
        assert len(responses[("POST", "/api/product/feedback")]) == 1
        assert invoke(installed, base, "action", "goal-create", body=json.dumps(body))[1] == {
            "status": "error", "error": "invalid_arguments",
        }


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
