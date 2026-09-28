"""Provision and validate user-bound API bearer tokens."""

import hashlib
import secrets
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import AccessToken, User

ACCESS_SCOPES = frozenset({"read", "analyze", "sync", "feedback", "manage"})


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def provision_access_token(
    db: Session, user_id: str, scopes: Iterable[str], *, expires_at: datetime
) -> str:
    """Issue a token once for an existing user; caller commits the session."""
    if not user_id or db.get(User, user_id) is None:
        raise ValueError("access token user must exist")
    allowed = set(scopes)
    if not allowed or not allowed <= ACCESS_SCOPES:
        raise ValueError("access token requires valid scopes")
    expiry = _utc_naive(expires_at)
    if expiry <= _now():
        raise ValueError("access token expiry must be in the future")
    token = secrets.token_urlsafe(32)
    db.add(AccessToken(
        token_digest=token_digest(token),
        user_id=user_id,
        scopes=sorted(allowed),
        expires_at=expiry,
    ))
    db.flush()
    return token


def authenticate_access_token(db: Session, token: str) -> AccessToken | None:
    """Return only an active token; all denial paths behave alike."""
    if not token or len(token) > 256:
        return None
    row = db.get(AccessToken, token_digest(token))
    if row is None or row.revoked_at is not None or row.expires_at <= _now():
        return None
    if db.get(User, row.user_id) is None:
        return None
    return row


def revoke_access_token(db: Session, digest: str) -> bool:
    """Revoke by stored digest so operators need not retain a raw token."""
    row = db.get(AccessToken, digest)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = _now()
    db.flush()
    return True
