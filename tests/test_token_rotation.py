"""Privileged rotation never trusts identity or authority from credential bytes."""
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from vitalis.adapters.persistence import session_scope
from vitalis.adapters.persistence.access_tokens import token_digest
from vitalis.adapters.persistence.models import AccessToken
from vitalis.entrypoints import token_rotation as rotation


@pytest.fixture
def credential(tmp_path, issue_token, monkeypatch):
    tmp_path.chmod(0o700)
    if os.name != "posix":
        monkeypatch.setattr(rotation, "_check_directory", lambda path: None)
    path = tmp_path / "access.token"
    token = issue_token("rotation-owner", {"read", "feedback"})
    path.write_text(token + "\n", encoding="ascii")
    path.chmod(0o600)
    monkeypatch.setattr(rotation, "_probe", lambda *args: None)
    return path, token


def rotate(credential, **kwargs):
    path, _ = credential
    return rotation.rotate_token(path, "rotation-owner", ["read", "feedback"], **kwargs)


def test_rotation_commits_before_probe_and_publishes_before_grace(credential, monkeypatch):
    path, old = credential
    seen = []
    def probe(origin, token, user):
        with session_scope() as db:
            row = db.get(AccessToken, token_digest(token))
            assert row is not None
            assert row.expires_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(days=89)
        assert path.read_text().strip() == old
        seen.append(token)
    monkeypatch.setattr(rotation, "_probe", probe)
    result = rotate(credential)
    assert result["state"] == "rotated"
    assert path.read_text().strip() == seen[0]
    with session_scope() as db:
        previous = db.get(AccessToken, token_digest(old))
        current = db.get(AccessToken, token_digest(seen[0]))
        assert 290 < (previous.expires_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds() <= 300
        assert 89 < (current.expires_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds() / 86400 <= 90


@pytest.mark.parametrize("kind", ["revoked", "owner", "scopes", "unknown"])
def test_untrusted_or_revoked_credential_fails_closed(credential, kind, issue_token):
    issue_token("other", {"read"})
    path, old = credential
    with session_scope() as db:
        row = db.get(AccessToken, token_digest(old))
        if kind == "revoked":
            row.revoked_at = datetime.now(UTC).replace(tzinfo=None)
        elif kind == "owner":
            row.user_id = "other"
        elif kind == "scopes":
            row.scopes = ["read"]
        else:
            path.write_text("unknown-synthetic-token\n")
    before = path.read_bytes()
    with pytest.raises(rotation.RotationError):
        rotate(credential, force=True)
    assert path.read_bytes() == before


def test_not_due_and_force_and_expired(credential):
    path, old = credential
    with session_scope() as db:
        db.get(AccessToken, token_digest(old)).expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=60)
    assert rotate(credential)["state"] == "not_due"
    assert path.read_text().strip() == old
    assert rotate(credential, force=True)["state"] == "rotated"
    current = path.read_text().strip()
    with session_scope() as db:
        db.get(AccessToken, token_digest(current)).expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    assert rotate(credential)["state"] == "rotated"
    with session_scope() as db:
        assert db.get(AccessToken, token_digest(current)).expires_at < datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.parametrize("failure", ["probe", "replace", "commit"])
def test_prepublication_failure_keeps_old_and_revokes_candidate(credential, monkeypatch, failure):
    path, old = credential
    with session_scope() as db:
        before = set(db.scalars(select(AccessToken.token_digest)))
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET-SENTINEL")
    if failure == "probe":
        monkeypatch.setattr(rotation, "_probe", fail)
    elif failure == "replace":
        monkeypatch.setattr(rotation.os, "replace", fail)
    else:
        monkeypatch.setattr(rotation, "_commit_candidate", fail)
    with pytest.raises(rotation.RotationError) as caught:
        rotate(credential)
    assert "SECRET-SENTINEL" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert path.read_text().strip() == old
    with session_scope() as db:
        rows = list(db.scalars(select(AccessToken)))
        assert all(row.revoked_at is not None for row in rows if row.token_digest not in before)
        assert db.get(AccessToken, token_digest(old)).revoked_at is None


@pytest.mark.parametrize("origin", ["http://example.com", "http://localhost", "http://127.0.0.1/x", "http://u:p@127.0.0.1", "http://127.0.0.1?x", "http://127.0.0.1#x"])
def test_probe_origin_rejects_remote_and_nonorigin(origin):
    with pytest.raises(rotation.RotationError):
        rotation._validate_origin(origin)


def test_missing_file_does_not_bootstrap(credential):
    path, _ = credential
    path.unlink()
    with pytest.raises(rotation.RotationError):
        rotate(credential)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permission contract")
@pytest.mark.parametrize("mode", [0o604, 0o606, 0o620, 0o650, 0o666])
def test_insecure_file_modes_rejected(credential, mode):
    credential[0].chmod(mode)
    with pytest.raises(rotation.RotationError):
        rotate(credential)


def test_cli_rotation_help_accepts_coach_scope(capsys):
    from vitalis.entrypoints.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["token", "rotate", "--help"])
    assert exc.value.code == 0
    assert "coach" in capsys.readouterr().out


def test_cli_rotation_requires_explicit_identity_and_scopes():
    from vitalis.entrypoints.cli import main
    with pytest.raises(SystemExit):
        main(["token", "rotate", "--token-file", "/private/access.token"])
