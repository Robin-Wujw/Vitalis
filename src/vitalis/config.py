"""Validated runtime configuration loaded from one explicit environment mapping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from dotenv import dotenv_values


@dataclass
class Settings:
    env: str
    timezone: str
    database_url: str
    zepp_app_id: str
    zepp_app_secret: str = field(repr=False)
    zepp_access_token: str = field(repr=False)
    zepp_redirect_uri: str
    zepp_scope: str
    zepp_mock: bool
    token_encryption_key: str = field(repr=False)
    pairing_ttl_minutes: int
    oauth_state_ttl_minutes: int
    pairing_processing_lease_seconds: int
    sync_cron_hour: int
    sync_cron_minute: int
    sync_dispatcher_interval_seconds: int
    sync_dispatcher_batch_chunks: int
    sync_lease_seconds: int
    sync_attempt_lease_seconds: int
    host: str
    port: int
    public_url: str
    pairing_allowed_origins: tuple[str, ...]
    pairing_rate_limit_attempts: int
    pairing_rate_limit_window_seconds: int
    push_user: str
    pushplus_token: str = field(repr=False)
    pushplus_access_key: str = field(repr=False)
    pushplus_query_max_attempts: int
    pushplus_query_interval_seconds: int
    weekly_report_enabled: bool
    weekly_report_hour: int
    weekly_report_minute: int
    monthly_report_enabled: bool
    monthly_report_hour: int
    monthly_report_minute: int


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Parse configuration without changing the process environment."""

    def integer(name: str, default: int, lower: int, upper: int) -> int:
        raw = environ.get(name, str(default))
        try:
            result = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not lower <= result <= upper:
            raise ValueError(f"{name} must be between {lower} and {upper}")
        return result

    def boolean(name: str, default: bool) -> bool:
        value = environ.get(name, str(default)).lower()
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        return value == "true"

    def origin(value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https", "chrome-extension"}
            or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
            raise ValueError("VITALIS_PAIRING_ALLOWED_ORIGINS must contain exact origins")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("VITALIS_PAIRING_ALLOWED_ORIGINS has an invalid port") from exc
        if parsed.scheme == "chrome-extension" and (
            parsed.port is not None or len(parsed.hostname) != 32
            or any(character not in "abcdefghijklmnop" for character in parsed.hostname)
        ):
            raise ValueError("VITALIS_PAIRING_ALLOWED_ORIGINS has an invalid extension ID")
        return f"{parsed.scheme}://{parsed.netloc.lower()}"

    timezone = environ.get("VITALIS_TIMEZONE", "Asia/Shanghai")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("VITALIS_TIMEZONE must be a valid IANA timezone") from exc

    database_url = environ.get("DATABASE_URL", "sqlite:///./vitalis.db")
    if not database_url.startswith(("sqlite://", "postgresql+psycopg://")):
        raise ValueError("DATABASE_URL must use sqlite or postgresql+psycopg")
    public_url = environ.get("VITALIS_PUBLIC_URL", "").rstrip("/")
    if public_url and not public_url.startswith(("http://", "https://")):
        raise ValueError("VITALIS_PUBLIC_URL must be an HTTP(S) URL")
    try:
        public_origin = origin(public_url) if public_url else None
    except ValueError as exc:
        raise ValueError("VITALIS_PUBLIC_URL must be a service origin without a path") from exc
    raw_origins = environ.get("VITALIS_PAIRING_ALLOWED_ORIGINS", "")
    configured_origins = tuple(
        origin(item.strip()) for item in raw_origins.split(",") if item.strip()
    )
    local_port = integer("PORT", 8000, 1, 65535)
    pairing_origins = tuple(dict.fromkeys((
        "https://watchface.zepp.com", "https://user.huami.com",
        f"http://localhost:{local_port}", f"http://127.0.0.1:{local_port}",
        *configured_origins,
    )))
    if public_origin and public_origin not in pairing_origins:
        pairing_origins = (*pairing_origins, public_origin)
    zepp_mock = boolean("ZEPP_MOCK", True)
    encryption_key = environ.get("VITALIS_TOKEN_ENCRYPTION_KEY", "")
    if not zepp_mock and not encryption_key:
        raise ValueError("VITALIS_TOKEN_ENCRYPTION_KEY is required for real Zepp credentials")
    if encryption_key:
        try:
            Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("VITALIS_TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc

    return Settings(
        env=environ.get("VITALIS_ENV", "dev"),
        timezone=timezone,
        database_url=database_url,
        zepp_app_id=environ.get("ZEPP_APP_ID", ""),
        zepp_app_secret=environ.get("ZEPP_APP_SECRET", ""),
        zepp_access_token=environ.get("ZEPP_ACCESS_TOKEN", ""),
        zepp_redirect_uri=environ.get(
            "ZEPP_REDIRECT_URI", "http://localhost:8000/api/connect/zepp/callback"
        ),
        zepp_scope=environ.get("ZEPP_SCOPE", "user.sleep user.activity user.training user.hr"),
        zepp_mock=zepp_mock,
        token_encryption_key=encryption_key,
        pairing_ttl_minutes=integer("ZEPP_PAIRING_TTL_MINUTES", 10, 1, 1440),
        oauth_state_ttl_minutes=integer("ZEPP_OAUTH_STATE_TTL_MINUTES", 10, 1, 1440),
        pairing_processing_lease_seconds=integer(
            "ZEPP_PAIRING_PROCESSING_LEASE_SECONDS", 120, 1, 3600
        ),
        sync_cron_hour=integer("SYNC_CRON_HOUR", 2, 0, 23),
        sync_cron_minute=integer("SYNC_CRON_MINUTE", 0, 0, 59),
        sync_dispatcher_interval_seconds=integer(
            "SYNC_DISPATCHER_INTERVAL_SECONDS", 60, 1, 86400
        ),
        sync_dispatcher_batch_chunks=integer("SYNC_DISPATCHER_BATCH_CHUNKS", 16, 1, 1024),
        sync_lease_seconds=integer("SYNC_LEASE_SECONDS", 120, 1, 86400),
        sync_attempt_lease_seconds=integer("SYNC_ATTEMPT_LEASE_SECONDS", 300, 1, 86400),
        host=environ.get("HOST", "127.0.0.1"),
        port=local_port,
        public_url=public_url,
        pairing_allowed_origins=pairing_origins,
        pairing_rate_limit_attempts=integer("ZEPP_PAIRING_RATE_LIMIT_ATTEMPTS", 12, 1, 1000),
        pairing_rate_limit_window_seconds=integer("ZEPP_PAIRING_RATE_LIMIT_WINDOW_SECONDS", 60, 1, 86400),
        push_user=environ.get("VITALIS_PUSH_USER", ""),
        pushplus_token=environ.get("PUSHPLUS_TOKEN", ""),
        pushplus_access_key=environ.get("PUSHPLUS_ACCESS_KEY", ""),
        pushplus_query_max_attempts=integer("PUSHPLUS_QUERY_MAX_ATTEMPTS", 3, 1, 1000),
        pushplus_query_interval_seconds=integer(
            "PUSHPLUS_QUERY_INTERVAL_SECONDS", 60, 1, 86400
        ),
        weekly_report_enabled=boolean("VITALIS_WEEKLY_REPORT_ENABLED", False),
        weekly_report_hour=integer("VITALIS_WEEKLY_REPORT_HOUR", 10, 0, 23),
        weekly_report_minute=integer("VITALIS_WEEKLY_REPORT_MINUTE", 0, 0, 59),
        monthly_report_enabled=boolean("VITALIS_MONTHLY_REPORT_ENABLED", False),
        monthly_report_hour=integer("VITALIS_MONTHLY_REPORT_HOUR", 10, 0, 23),
        monthly_report_minute=integer("VITALIS_MONTHLY_REPORT_MINUTE", 30, 0, 59),
    )


# Existing library callers share one validated instance; explicit callers can load
# a separate instance from a mapping without mutating global environment variables.
_dotenv = {key: value for key, value in dotenv_values().items() if value is not None}
settings = load_settings({**_dotenv, **os.environ})
