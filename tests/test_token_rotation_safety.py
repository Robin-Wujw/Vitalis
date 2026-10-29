"""Publication, transport and lock regression tests for local rotation."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vitalis.adapters.persistence import session_scope
from vitalis.adapters.persistence.access_tokens import token_digest
from vitalis.adapters.persistence.models import AccessToken
from vitalis.entrypoints import token_rotation as rotation


@pytest.fixture
def managed(tmp_path, issue_token, monkeypatch):
    tmp_path.chmod(0o700)
    if os.name != "posix":
        monkeypatch.setattr(rotation, "_check_directory", lambda path: None)
    path = tmp_path / "access.token"
    path.write_text(issue_token("rotation-safe", {"read"}) + "\n", encoding="ascii")
    path.chmod(0o600)
    monkeypatch.setattr(rotation, "_probe", lambda *args: None)
    return path


def test_concurrent_due_rotations_publish_only_once(managed):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rotation.rotate_token(managed, "rotation-safe", ["read"]), range(2)))
    assert sorted(result["state"] for result in results) == ["not_due", "rotated"]


def test_postpublication_failure_keeps_valid_candidate(managed, monkeypatch):
    old = managed.read_text().strip()
    def fail(*args):
        raise RuntimeError("SECRET")
    monkeypatch.setattr(rotation, "_sync_directory", fail)
    with pytest.raises(rotation.RotationError, match="published"):
        rotation.rotate_token(managed, "rotation-safe", ["read"])
    current = managed.read_text().strip()
    assert current != old
    with session_scope() as db:
        assert db.get(AccessToken, token_digest(current)).revoked_at is None
        assert db.get(AccessToken, token_digest(old)).expires_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=23)


def test_grace_commit_failure_keeps_new_and_original_expiry(managed):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    old = managed.read_text().strip()
    def fail_grace(session):
        if managed.read_text().strip() != old:
            raise RuntimeError("synthetic-secret-database-error")
    event.listen(Session, "before_commit", fail_grace)
    try:
        with pytest.raises(rotation.RotationError, match="published"):
            rotation.rotate_token(managed, "rotation-safe", ["read"])
    finally:
        event.remove(Session, "before_commit", fail_grace)
    current = managed.read_text().strip()
    assert current != old
    with session_scope() as db:
        assert db.get(AccessToken, token_digest(current)).revoked_at is None
        assert db.get(AccessToken, token_digest(old)).expires_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=23)


def test_zero_grace_expires_old_without_reviving(managed):
    old = managed.read_text().strip()
    rotation.rotate_token(managed, "rotation-safe", ["read"], grace_seconds=0)
    with session_scope() as db:
        assert db.get(AccessToken, token_digest(old)).expires_at <= datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.skipif(os.name != "posix", reason="POSIX metadata")
def test_preserves_owner_group_and_mode(managed):
    managed.chmod(0o640)
    before = managed.stat()
    rotation.rotate_token(managed, "rotation-safe", ["read"])
    after = managed.stat()
    assert (before.st_uid, before.st_gid, before.st_mode) == (after.st_uid, after.st_gid, after.st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_untrusted_directory_and_symlink_rejected(managed):
    managed.parent.chmod(0o770)
    with pytest.raises(rotation.RotationError):
        rotation.rotate_token(managed, "rotation-safe", ["read"])
    managed.parent.chmod(0o700)
    link = managed.with_name("link.token")
    link.symlink_to(managed)
    with pytest.raises(rotation.RotationError):
        rotation.rotate_token(link, "rotation-safe", ["read"])


@pytest.mark.parametrize("status,user", [(302, "rotation-safe"), (401, "rotation-safe"), (200, "other")])
def test_probe_never_redirects_or_trusts_wrong_identity(monkeypatch, status, user):
    real_client = httpx.Client
    def handler(request):
        assert request.url.path == "/api/intelligence/profile"
        return httpx.Response(status, json={"user_id": user}, headers={"Location": "https://example.com"})
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(rotation.httpx, "Client", client)
    with pytest.raises(rotation.RotationError):
        rotation._probe("http://127.0.0.1:8000", "synthetic-secret", "rotation-safe")


def test_revocation_during_probe_retains_old_and_rejects_successor(managed, monkeypatch):
    old = managed.read_text().strip()
    def revoke_during_probe(*args):
        with session_scope() as db:
            db.get(AccessToken, token_digest(old)).revoked_at = datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(rotation, "_probe", revoke_during_probe)
    with pytest.raises(rotation.RotationError):
        rotation.rotate_token(managed, "rotation-safe", ["read"])
    assert managed.read_text().strip() == old


@pytest.mark.parametrize("content", ["synthetic\x00token", "synthetic\ntoken", "synthetic" + "\n" * 300])
def test_malformed_credential_is_rejected(managed, content):
    managed.write_text(content, encoding="ascii")
    with pytest.raises(rotation.RotationError):
        rotation.rotate_token(managed, "rotation-safe", ["read"])


def test_cli_schema_failure_is_sanitized(managed, capsys, monkeypatch):
    from vitalis.adapters.persistence import database
    from vitalis.entrypoints.cli import main
    def fail():
        raise RuntimeError("synthetic-secret-schema-error")
    monkeypatch.setattr(database, "check_schema", fail)
    assert main(["token", "rotate", "--token-file", str(managed), "--user", "rotation-safe", "--scope", "read"]) == 1
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.err + output.out
    assert '"failure_code": "token_rotation_failed"' in output.err


def test_cli_output_is_only_safe_status(managed, capsys):
    from vitalis.entrypoints.cli import main
    assert main(["token", "rotate", "--token-file", str(managed), "--user", "rotation-safe", "--scope", "read"]) == 0
    assert capsys.readouterr().out.strip() == '{"state": "rotated"}'
