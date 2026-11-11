"""Signed continuation of one bounded, owner-scoped body-record query."""
import base64
import hashlib
import hmac
import json
from datetime import date, datetime


class BodyPageCursor:
    def __init__(self, secret: str):
        self._key = hmac.digest(secret.encode("utf-8"), b"vitalis:body-pagination:v1", "sha256")

    def encode(self, scope: list, upper: date, position: tuple) -> str:
        payload = json.dumps({"v": 1, "scope": scope, "upper": upper.isoformat(),
            "position": list(position)}, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return encoded + "." + hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).hexdigest()

    def decode(self, token: str, scope: list) -> tuple[date, tuple]:
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 4096:
                raise ValueError
            encoded, signature = token.split(".")
            expected = hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            payload = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            if payload["v"] != 1 or payload["scope"] != scope:
                raise ValueError
            upper = date.fromisoformat(payload["upper"])
            position = payload["position"]
            if not isinstance(position, list) or len(position) != 6:
                raise ValueError
            stamp = datetime.fromisoformat(position[0])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
            for value, maximum in zip(position[1:5], (32, 24, 128, 128), strict=True):
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError
            if type(position[5]) is not int or position[5] < 0:
                raise ValueError
            return upper, (stamp, *position[1:])
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            raise ValueError("invalid body record cursor") from None
