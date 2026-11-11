"""Contract for explicit, bounded, account-scoped upstream GET jobs."""
import json
import math
import re

MAX_RAW_BYTES = 2 * 1024 * 1024
_PRIVATE = re.compile(r"token|secret|password|cookie|authorization|authkey|privatekey|sessionkey|credential|apikey|accesskey|refreshkey|bearer", re.I)
_IDENTITY = {"userid", "uid", "accountid", "memberid"}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def validate_raw_request(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) - {"path", "params"}:
        raise ValueError("raw request requires path and scalar query params")
    path, params = request.get("path"), request.get("params") or {}
    if (not isinstance(path, str) or not 1 < len(path) <= 512
        or not re.fullmatch(r"/[A-Za-z0-9_./{}-]+", path)
        or "//" in path or ".." in path or "{" in path or "}" in path):
        raise ValueError("path must be an absolute Zepp path without query, encoding or host")
    parts = path.split("/")
    for index, part in enumerate(parts[:-1]):
        allowed_identity = {"me", "-1"} if part.lower() == "members" else {"me"}
        if part.lower() in {"users", "user", "accounts", "members"} and parts[index + 1] not in allowed_identity:
            raise ValueError("account paths must use me; the worker supplies the authenticated identity")
    if not isinstance(params, dict) or len(params) > 32:
        raise ValueError("at most 32 scalar query parameters are allowed")
    for key, value in params.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ValueError("invalid query key")
        if _PRIVATE.search(_key(key)) or _key(key) in _IDENTITY:
            raise ValueError("identity and credential parameters are supplied by the worker")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("query values must be scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("query values must be finite")
    if len(json.dumps(params).encode("utf-8")) > 8192:
        raise ValueError("query exceeds the 8192 byte budget")
    return {"path": path, "params": params}


def public_raw_payload(value, *, _depth=0):
    """Mask credential-bearing keys even inside JSON encoded event fields."""
    if _depth > 32:
        return "[depth limit]"
    if isinstance(value, dict):
        return {key: public_raw_payload(item, _depth=_depth + 1) for key, item in value.items()
                if not _PRIVATE.search(_key(str(key))) and _key(str(key)) not in {"additionalinfo"}}
    if isinstance(value, list):
        return [public_raw_payload(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(public_raw_payload(json.loads(value), _depth=_depth + 1), ensure_ascii=False)
        except (ValueError, RecursionError):
            return "[unparsed structured text]"
    return value
