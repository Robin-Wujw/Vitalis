#!/usr/bin/env python3
"""Standalone, Bearer-authenticated client for the current Vitalis /api routes."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_FEEDBACK_BYTES = 16 * 1024
TRANSIENT_STATUSES = {429, 502, 503, 504}
REPORT_KINDS = (
    "daily", "morning", "evening", "weekly", "monthly",
    "weekly-briefing", "monthly-briefing",
)
READ_OPERATIONS = {
    "profile": "intelligence/profile",
    "trends": "intelligence/trends",
    "events": "intelligence/events",
    "explain": "intelligence/explain",
    "context": "intelligence/context",
    "training-responses": "intelligence/training-responses",
    "personal-model": "intelligence/personal-model",
    "personal-associations": "intelligence/personal-associations",
    "timeline": "intelligence/timeline",
    "training-preferences": "intelligence/training-preferences",
    "feedback": "feedback",
    "product-goals": "product/goals",
    "product-feedback": "product/feedback",
    "product-summary": "product/summary",
    "product-metrics": "product/metrics",
    "product-context": "product/context",
    "connection-progress": "connect/zepp/progress",
}
WRITE_OPERATIONS = {
    "profile-patch": ("PATCH", "intelligence/profile"),
    "preferences-put": ("PUT", "intelligence/training-preferences"),
    "preferences-patch": ("PATCH", "intelligence/training-preferences"),
    "strength-confirm": ("POST", "intelligence/workouts/{id}/strength-exercises"),
    "recommendation-complete": ("POST", "intelligence/recommendations/{id}/complete"),
    "event-acknowledge": ("POST", "intelligence/events/{id}/acknowledge"),
    "goal-create": ("POST", "product/goals"),
    "goal-patch": ("PATCH", "product/goals/{id}"),
    "product-feedback": ("POST", "product/feedback"),
}
KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{16,128}\Z")


class ClientError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        self.code = code
        self.http_status = http_status

    def as_json(self) -> dict:
        result = {"status": "error", "error": self.code}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse may interpolate an invalid argument (including a secret).
        raise ClientError("invalid_arguments")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # No authenticated redirect is necessary for these exact /api paths.
        return None


def api_origin() -> str:
    value = os.environ.get("VITALIS_API_BASE_URL", "")
    try:
        parts = urllib.parse.urlsplit(value)
        if (
            parts.scheme not in ("http", "https") or not parts.netloc
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None
            or "@" in parts.netloc or "\\" in value or "?" in value or "#" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or parts.hostname is None
        ):
            raise ValueError
        port = parts.port  # Also validates malformed ports.
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parts.scheme == "http" and parts.hostname.lower() not in (
            "localhost", "127.0.0.1", "::1",
        ):
            raise ValueError
    except ValueError as exc:
        raise ClientError("invalid_api_origin") from exc
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def access_token() -> str:
    token = os.environ.get("VITALIS_ACCESS_TOKEN", "")
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ClientError("invalid_access_token")
    return token


def iso_date(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except ValueError:
        pass
    raise ClientError("invalid_arguments")


def iso_aware_datetime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return value
    except ValueError:
        pass
    raise ClientError("invalid_arguments")


def positive_int(value: str, maximum: int) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ClientError("invalid_arguments") from exc
    if not 1 <= number <= maximum:
        raise ClientError("invalid_arguments")
    return number


def strict_json(value):
    def reject_constant(_value):
        raise ValueError("non-standard JSON constant")

    def finite_float(raw):
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    return json.loads(value, parse_constant=reject_constant, parse_float=finite_float)


def key_from_file(filename: str, operation: str, body: dict) -> str:
    """Persist a key and method/path/body fingerprint before sending a write."""
    method, separator, path = operation.partition(" ")
    fingerprint = (
        {"method": method, "path": path, "body": body}
        if separator and method and path.startswith("/")
        else {"operation": operation, "body": body}
    )
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = Path(filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ClientError("idempotency_file_unavailable") from exc
    else:
        key = str(uuid.uuid4())
        record = {"version": 1, "operation": operation, "request_sha256": digest, "key": key}
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(record, output, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            if os.name == "posix":
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            # Leave an incomplete file in place so an uncertain write cannot
            # silently acquire another key on the next invocation.
            raise ClientError("idempotency_file_unavailable") from exc
        return key

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stored:
            if not stat.S_ISREG(os.fstat(stored.fileno()).st_mode):
                raise ClientError("invalid_idempotency_file")
            content = stored.read(2049)
            if len(content) > 2048:
                raise ClientError("invalid_idempotency_file")
            record = strict_json(content)
        if (
            not isinstance(record, dict) or record.get("version") != 1
            or record.get("operation") != operation
            or record.get("request_sha256") != digest
            or not isinstance(record.get("key"), str)
            or not KEY_PATTERN.fullmatch(record["key"])
        ):
            raise ClientError("idempotency_key_mismatch")
        return record["key"]
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise ClientError("invalid_idempotency_file") from exc


def feedback_body() -> dict:
    if sys.stdin.isatty():
        raise ClientError("feedback_requires_stdin")
    try:
        data = sys.stdin.buffer.read(MAX_FEEDBACK_BYTES + 1)
        if len(data) > MAX_FEEDBACK_BYTES:
            raise ClientError("feedback_too_large")
        body = strict_json(data)
        if not isinstance(body, dict):
            raise ClientError("invalid_feedback_json")
        return body
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ClientError("invalid_feedback_json") from exc
    except OSError as exc:
        raise ClientError("feedback_input_unavailable") from exc


def redact(value, token: str):
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(key, token): redact(item, token) for key, item in value.items()}
    return value


def request(method: str, path: str, token: str, body: dict | None = None,
            params: dict | None = None, key: str | None = None):
    origin = api_origin()
    url = origin + "/api/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if key is not None:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # Disable implicit proxies as well as redirects; credentials go only to the configured origin.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for attempt in range(2 if method == "GET" else 1):
        try:
            with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ClientError("response_too_large")
                try:
                    result = strict_json(raw)
                    if not isinstance(result, (dict, list)):
                        raise ValueError("expected JSON object or list")
                    return redact(result, token)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ClientError("invalid_response_json") from exc
        except urllib.error.HTTPError as exc:
            exc.close()
            if 300 <= exc.code < 400:
                raise ClientError("redirect_blocked", exc.code) from None
            if method == "GET" and attempt == 0 and exc.code in TRANSIENT_STATUSES:
                time.sleep(0.2)
                continue
            if path.startswith("reports/") and exc.code == 404:
                return {"status": "snapshot_missing", "http_status": 404}
            code = {401: "unauthorized", 403: "forbidden"}.get(exc.code, "http_error")
            raise ClientError(code, exc.code) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            if method == "GET" and attempt == 0:
                time.sleep(0.2)
                continue
            raise ClientError("network_error") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = JsonArgumentParser(description="Vitalis current API client")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    commands.add_parser("status")

    report = commands.add_parser("report")
    report.add_argument("kind", choices=REPORT_KINDS)
    report.add_argument("--day", type=iso_date)
    report_views = report.add_mutually_exclusive_group()
    report_views.add_argument("--state", action="store_true", help="Read freshness and last-good snapshot state")
    report_views.add_argument("--view", action="store_true", help="Read the saved public report projection")

    analyze = commands.add_parser("analyze")
    analyze.add_argument("--day", type=iso_date, required=True)
    analyze.add_argument("--key-file", required=True)

    job = commands.add_parser("job")
    job.add_argument("id")

    query = commands.add_parser("query")
    query.add_argument("operation", choices=tuple(READ_OPERATIONS))
    query.add_argument("--day", type=iso_date)
    query.add_argument("--start", type=iso_date)
    query.add_argument("--end", type=iso_date)
    query.add_argument("--event-type")
    query.add_argument("--limit", type=lambda value: positive_int(value, 100))

    action = commands.add_parser("action")
    action.add_argument("operation", choices=tuple(WRITE_OPERATIONS))
    action.add_argument("--id")
    action.add_argument("--source")
    action.add_argument("--key-file")

    feedback = commands.add_parser("feedback")
    feedback.add_argument("--key-file")

    sync = commands.add_parser("sync")
    sync.add_argument("--key-file", required=True)
    sync.add_argument("--days", type=lambda value: positive_int(value, 730), default=7)
    sync.add_argument("--decode-dense-files", action="store_true")
    sync.add_argument("--detail-backfill", action="store_true")
    sync.add_argument("--workout-only", action="store_true")
    sync.add_argument("--detail-only", action="store_true")
    sync.add_argument("--detail-limit", type=lambda value: positive_int(value, 4))
    sync.add_argument("--detail-refresh-before", type=iso_aware_datetime)

    workouts = commands.add_parser("workouts")
    workouts.add_argument("--from", dest="from_date", type=iso_date)
    workouts.add_argument("--to", type=iso_date)
    workouts.add_argument("--limit", type=lambda value: positive_int(value, 500))
    workouts.add_argument("--id")
    workouts.add_argument("--source")
    return parser.parse_args(argv)


def run(args: argparse.Namespace):
    # Validate config before creating an idempotency file or reading private feedback.
    api_origin()
    token = access_token()
    if args.command == "status":
        return request("GET", "data-status", token)
    if args.command == "report":
        suffix = "/state" if args.state else "/view" if args.view else ""
        path = "reports/" + args.kind + suffix
        return request("GET", path, token,
                       params={"day": args.day} if args.day else None)
    if args.command == "job":
        return request("GET", "jobs/" + urllib.parse.quote(args.id, safe=""), token)
    if args.command == "query":
        allowed = {
            "profile": set(), "training-preferences": set(),
            "trends": {"day"}, "explain": {"day"}, "context": {"day"},
            "training-responses": {"day"}, "personal-model": {"day"},
            "personal-associations": {"day"},
            "events": {"start", "end", "event_type"},
            "timeline": {"start", "end", "limit"},
            "feedback": {"start", "end"},
            "product-goals": set(),
            "product-feedback": {"start", "end", "limit"},
            "product-summary": {"start", "end"},
            "product-metrics": {"start", "end"},
            "product-context": {"day"},
            "connection-progress": set(),
        }[args.operation]
        supplied = {
            "day": args.day, "start": args.start, "end": args.end,
            "event_type": args.event_type, "limit": args.limit,
        }
        if any(value is not None and name not in allowed for name, value in supplied.items()):
            raise ClientError("invalid_arguments")
        if args.event_type is not None and len(args.event_type) > 64:
            raise ClientError("invalid_arguments")
        return request("GET", READ_OPERATIONS[args.operation], token,
                       params={name: value for name, value in supplied.items() if value is not None} or None)
    if args.command == "action":
        method, path = WRITE_OPERATIONS[args.operation]
        needs_id = "{id}" in path
        if needs_id != (args.id is not None) or (args.source is not None) != (args.operation == "strength-confirm"):
            raise ClientError("invalid_arguments")
        if args.id is not None and (not args.id or len(args.id) > 128):
            raise ClientError("invalid_arguments")
        keyed_actions = {"goal-create", "goal-patch", "product-feedback"}
        if (args.operation in keyed_actions) != (args.key_file is not None):
            raise ClientError("invalid_arguments")
        if needs_id:
            path = path.replace("{id}", urllib.parse.quote(args.id, safe=""))
        body = None if args.operation == "event-acknowledge" else feedback_body()
        key = None
        if args.operation in keyed_actions:
            key = key_from_file(args.key_file, f"{method} /api/{path}", body)
        return request(method, path, token, body=body, key=key,
                       params={"source": args.source} if args.source else None)
    if args.command == "workouts":
        if args.id is not None:
            if args.source is None or args.from_date or args.to or args.limit:
                raise ClientError("invalid_arguments")
            return request("GET", "workouts/" + urllib.parse.quote(args.id, safe=""), token,
                           params={"source": args.source})
        if args.source is not None:
            raise ClientError("invalid_arguments")
        params = {name: value for name, value in (("from", args.from_date), ("to", args.to),
                                                  ("limit", args.limit)) if value is not None}
        return request("GET", "workouts", token, params=params)
    if args.command == "feedback":
        body = feedback_body()
        key = key_from_file(args.key_file, "POST /api/feedback", body) if args.key_file else None
        return request("POST", "feedback", token, body=body, key=key)
    if args.command == "analyze":
        body = {"day": args.day}
        key = key_from_file(args.key_file, "POST /api/analysis-runs", body)
        return request("POST", "analysis-runs", token, body=body, key=key)
    body = {"days": args.days, "source": "zepp"}
    for field in ("decode_dense_files", "detail_backfill", "workout_only", "detail_only",
                  "detail_limit", "detail_refresh_before"):
        value = getattr(args, field)
        if value is not None:
            body[field] = value
    key = key_from_file(args.key_file, "POST /api/sync-jobs", body)
    return request("POST", "sync-jobs", token, body=body, key=key)


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(sys.argv[1:] if argv is None else argv))
    except ClientError as exc:
        print(json.dumps(exc.as_json()))
        return 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
