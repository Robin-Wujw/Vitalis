"""Small encryption boundary for vendor credentials stored by Vitalis."""

from __future__ import annotations

from vitalis.config import settings

_PREFIX = "fernet:"


def encrypt_token(value: str) -> str:
    """Encrypt a token when a deployment key is configured.

    Plaintext is retained only for local/backwards-compatible deployments. Cloud
    deployments should always set VITALIS_TOKEN_ENCRYPTION_KEY.
    """
    if not value or value.startswith(_PREFIX) or not settings.token_encryption_key:
        return value
    from cryptography.fernet import Fernet

    encrypted = Fernet(settings.token_encryption_key.encode("ascii")).encrypt(value.encode("utf-8"))
    return _PREFIX + encrypted.decode("ascii")


def decrypt_token(value: str) -> str:
    """Decrypt a stored token while accepting legacy plaintext rows."""
    if not value or not value.startswith(_PREFIX):
        return value
    if not settings.token_encryption_key:
        raise RuntimeError("凭据已加密，但未配置 VITALIS_TOKEN_ENCRYPTION_KEY")
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(settings.token_encryption_key.encode("ascii")).decrypt(
            value[len(_PREFIX):].encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Zepp 凭据无法解密，请检查 VITALIS_TOKEN_ENCRYPTION_KEY") from exc
