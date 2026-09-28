"""Mandatory encryption boundary for vendor credentials."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from vitalis.config import settings

_PREFIX = "fernet:"
_MOCK_PREFIX = "mock-fernet:"
# Mock mode may intentionally omit the operator key, but its synthetic token rows
# still need to be opaque and readable after a process restart.
_MOCK_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(b"vitalis-zepp-mock-credential-key-v1").digest()
)


def _cipher() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError("需要 VITALIS_TOKEN_ENCRYPTION_KEY 才能保存或读取厂商凭据")
    try:
        return Fernet(settings.token_encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("VITALIS_TOKEN_ENCRYPTION_KEY 无效") from exc


def _mock_cipher() -> Fernet:
    return Fernet(_MOCK_KEY)


def encrypt_token(value: str) -> str:
    """Encrypt nonempty vendor credentials before database storage."""
    if not value:
        return value
    if settings.token_encryption_key:
        return _PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")
    if settings.zepp_mock:
        return _MOCK_PREFIX + _mock_cipher().encrypt(value.encode("utf-8")).decode("ascii")
    # Real mode must never silently fall back to the synthetic mock key.
    return _PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_token(value: str) -> str:
    """Reject plaintext rows; current installations start from a fresh schema."""
    if not value:
        return value
    if value.startswith(_MOCK_PREFIX):
        if not settings.zepp_mock:
            raise RuntimeError("模拟凭据不能在真实 Zepp 模式下读取")
        cipher = _mock_cipher()
        encoded = value[len(_MOCK_PREFIX):]
    elif value.startswith(_PREFIX):
        cipher = _cipher()
        encoded = value[len(_PREFIX):]
    else:
        raise RuntimeError("厂商凭据不是当前加密格式，请使用新库并重新配对")
    try:
        return cipher.decrypt(encoded.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Zepp 凭据无法解密，请检查 VITALIS_TOKEN_ENCRYPTION_KEY") from exc
