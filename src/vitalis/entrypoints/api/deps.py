"""API dependencies for scoped user requests."""

from collections.abc import Callable, Iterator

from fastapi import Header, HTTPException

from vitalis.adapters.persistence import get_session, session_scope
from vitalis.adapters.persistence.access_tokens import ACCESS_SCOPES, authenticate_access_token


def get_db() -> Iterator:
    yield from get_session()


def authenticated_user(authorization: str, x_user_id: str | None, scope: str) -> str:
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator or scheme.lower() != "bearer" or not token
        or token.strip() != token or " " in token
    ):
        raise HTTPException(
            status_code=401, detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    with session_scope() as db:
        row = authenticate_access_token(db, token)
        if row is None:
            raise HTTPException(
                status_code=401, detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_id = row.user_id
        scopes = row.scopes
    if x_user_id is not None and x_user_id.strip() != user_id:
        raise HTTPException(status_code=403, detail="X-User-Id does not match access token")
    if scope not in scopes:
        raise HTTPException(status_code=403, detail=f"Scope '{scope}' required")
    return user_id


def require_scope(scope: str) -> Callable[..., str]:
    """Make each write route's required API permission explicit."""
    if scope not in ACCESS_SCOPES:
        raise ValueError(f"Unknown access scope: {scope}")

    def dependency(
        authorization: str = Header(default="", alias="Authorization"),
        x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    ) -> str:
        return authenticated_user(authorization, x_user_id, scope)

    return dependency


def require_user_id(
    authorization: str = Header(default="", alias="Authorization"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str:
    """Read-only default for existing user-scoped query routes."""
    return authenticated_user(authorization, x_user_id, "read")
