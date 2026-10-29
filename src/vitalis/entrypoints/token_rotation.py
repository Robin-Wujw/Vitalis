"""Local database-authorized credential rotation; never an HTTP refresh endpoint.

The existing token is an identity check, not an issuance authority. The caller
must have DB access and control a directory the consuming agent cannot modify.
A committed candidate is probed before atomic publication; only after publication
is the predecessor shortened. A process/power loss between these independent
resources may leave two credentials until their bounded original expiries (at
most the configured 365 days for the candidate). No credential is made perpetual,
and no unpublished secret is logged. A grace-update failure retains the working
published credential, reports failure, and never extends the predecessor.
"""
import ipaddress
import os
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vitalis.adapters.persistence.access_tokens import (
    ACCESS_SCOPES,
    provision_access_token,
    revoke_access_token,
    token_digest,
)
from vitalis.adapters.persistence.database import SessionLocal
from vitalis.adapters.persistence.models import AccessToken


class RotationError(ValueError):
    """Safe operator message; underlying exceptions may contain credentials."""


def _validate_origin(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or "?" in origin or "#" in origin
                or not ipaddress.ip_address(parsed.hostname or "").is_loopback):
            raise ValueError
        _ = parsed.port
        return origin
    except (ValueError, TypeError):
        raise RotationError("rotation requires a literal loopback API origin") from None


def _probe(origin: str, token: str, user_id: str) -> None:
    # No proxy environment, redirects, response bodies or HTTP exception logging.
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
            response = client.get(
                origin + "/api/intelligence/profile",
                headers={"Authorization": "Bearer " + token, "X-User-Id": user_id},
            )
            if response.status_code != 200 or response.json().get("user_id") != user_id:
                raise ValueError
    except Exception:  # noqa: BLE001 - never expose transport/DB/file secrets
        raise RotationError("rotation local API validation failed") from None


def _check_directory(path: Path) -> None:
    # POSIX production contract: owner is this privileged service (or root),
    # readers may traverse but cannot replace files or any ancestor directory.
    for directory in (path.parent, *path.parent.parents):
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RotationError("rotation requires trusted real directories")
        if os.name == "posix" and (info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
            raise RotationError("rotation directory is not trusted")
    if os.name == "posix" and path.parent.stat().st_mode & 0o007:
        raise RotationError("rotation requires a private credential directory")


def _check_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RotationError("rotation requires a regular private credential file")
    if os.name == "posix" and (info.st_mode & 0o037 or info.st_uid not in {0, os.geteuid()}):
        raise RotationError("rotation credential permissions are unsafe")


def _read_credential(path: Path) -> tuple[str, os.stat_result]:
    info = path.lstat()
    _check_file(info)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _check_file(opened)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise RotationError("rotation credential changed during read")
        raw = stream.read(258)
    token = raw.decode("ascii").rstrip("\r\n")
    if not token or len(raw) > 257 or len(token) > 256 or any(not 33 <= ord(c) <= 126 for c in token):
        raise RotationError("rotation credential is invalid")
    return token, opened


@contextmanager
def _lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        _check_file(os.fstat(stream.fileno()))
        if os.name == "posix":
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    # Do not unlink: waiters must share the same inode across atomic replacement.


def _commit_candidate(db) -> None:
    db.commit()


def _publish(path: Path, token: str, info: os.stat_result) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".rotation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "posix":
                os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write((token + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RotationError("rotation credential changed before publication")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def rotate_token(
    token_file: Path, user_id: str, scopes: list[str], *, expires_days: int = 90,
    renew_before_days: int = 14, grace_seconds: int = 300,
    api_base_url: str = "http://127.0.0.1:8000", force: bool = False,
) -> dict[str, str]:
    """Rotate using explicit local administrator authority, returning no secrets."""
    candidate_digest = None
    published = False
    try:
        path = Path(token_file)
        allowed = set(scopes)
        if (not path.is_absolute() or ".." in path.parts or not user_id.strip()
                or user_id != user_id.strip() or not allowed or not allowed <= ACCESS_SCOPES
                or "read" not in allowed or not 1 <= expires_days <= 365
                or not 0 <= renew_before_days < expires_days
                or not 0 <= grace_seconds <= 86400):
            raise RotationError("invalid explicit rotation configuration")
        origin = _validate_origin(api_base_url)
        _check_directory(path)
        with _lock(path):
            old_token, metadata = _read_credential(path)
            old_digest = token_digest(old_token)
            now = datetime.now(UTC)
            with SessionLocal() as db:
                previous = db.get(AccessToken, old_digest)
                if (previous is None or previous.revoked_at is not None
                        or previous.user_id != user_id or set(previous.scopes) != allowed):
                    raise RotationError("rotation credential is unknown, revoked or mismatched")
                if not force and previous.expires_at > (now + timedelta(days=renew_before_days)).replace(tzinfo=None):
                    return {"state": "not_due"}
                candidate = provision_access_token(
                    db, user_id, allowed, expires_at=now + timedelta(days=expires_days),
                )
                candidate_digest = token_digest(candidate)
                _commit_candidate(db)
            _probe(origin, candidate, user_id)
            # Probe may be slow: do not publish if an operator revoked or changed
            # the predecessor while it ran. Unrelated admin writers do not take
            # our file lock; an already concurrent revocation after this check
            # must revoke the successor separately, as with any local issuance.
            with SessionLocal() as db:
                previous = db.get(AccessToken, old_digest)
                if (previous is None or previous.revoked_at is not None
                        or previous.user_id != user_id or set(previous.scopes) != allowed):
                    raise RotationError("rotation predecessor changed before publication")
            _publish(path, candidate, metadata)
            published = True
            _sync_directory(path)
            with SessionLocal() as db:
                previous = db.get(AccessToken, old_digest)
                previous.expires_at = min(
                    previous.expires_at,
                    datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=grace_seconds),
                )
                db.commit()
            return {"state": "rotated"}
    except Exception:  # noqa: BLE001 - never expose transport/DB/file secrets
        if candidate_digest and not published:
            try:
                with SessionLocal() as db:
                    revoke_access_token(db, candidate_digest)
                    db.commit()
            except Exception:  # noqa: BLE001 - never expose transport/DB/file secrets
                # Even cleanup failure leaves only a bounded-expiry credential.
                raise RotationError("rotation failed before publication; candidate cleanup requires operator review") from None
        if published:
            raise RotationError("rotation published; grace update requires operator review") from None
        raise RotationError("rotation failed before publication; old credential retained; check configuration, permissions and local API") from None
