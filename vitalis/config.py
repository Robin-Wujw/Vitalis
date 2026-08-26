"""全局配置：从环境变量加载。"""

import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    env: str = os.getenv("VITALIS_ENV", "dev")

    # 存储
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite:///./vitalis.db"
    )

    # Zepp
    zepp_app_id: str = os.getenv("ZEPP_APP_ID", "")
    zepp_app_secret: str = os.getenv("ZEPP_APP_SECRET", "")
    zepp_access_token: str = os.getenv("ZEPP_ACCESS_TOKEN", "")
    # 扫码登录（OAuth2 授权码模式）回调地址：如 http://localhost:8000/api/v1/connect/zepp/callback
    zepp_redirect_uri: str = os.getenv("ZEPP_REDIRECT_URI", "http://localhost:8000/api/v1/connect/zepp/callback")
    zepp_scope: str = os.getenv("ZEPP_SCOPE", "user.sleep user.activity user.training user.hr")
    zepp_mock: bool = os.getenv("ZEPP_MOCK", "true").lower() == "true"
    # Fernet key (`Fernet.generate_key()`), used to encrypt cloud-stored tokens.
    # Development remains backwards compatible when unset; production should set it.
    token_encryption_key: str = os.getenv("VITALIS_TOKEN_ENCRYPTION_KEY", "")
    pairing_ttl_minutes: int = int(os.getenv("ZEPP_PAIRING_TTL_MINUTES", "10"))

    # LLM
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai_compatible")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")

    # 调度
    sync_cron_hour: int = int(os.getenv("SYNC_CRON_HOUR", "2"))
    sync_cron_minute: int = int(os.getenv("SYNC_CRON_MINUTE", "0"))

    # 服务
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    public_url: str = os.getenv("VITALIS_PUBLIC_URL", "").rstrip("/")


settings = Settings()
