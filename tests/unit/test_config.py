import pytest

from vitalis.config import load_settings


def test_load_settings_is_explicit_and_does_not_change_process_environment(monkeypatch):
    monkeypatch.setenv("PORT", "43210")
    values = load_settings({"PORT": "8765", "DATABASE_URL": "sqlite:///:memory:"})
    assert values.port == 8765
    assert values.database_url == "sqlite:///:memory:"
    assert values.zepp_mock is True
    assert values.timezone == "Asia/Shanghai"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "nope"),
        ("PORT", "65536"),
        ("SYNC_CRON_HOUR", "24"),
        ("VITALIS_WEEKLY_REPORT_ENABLED", "perhaps"),
        ("VITALIS_WEEKLY_REPORT_HOUR", "24"),
        ("VITALIS_WEEKLY_REPORT_MINUTE", "60"),
        ("VITALIS_MONTHLY_REPORT_ENABLED", "perhaps"),
        ("VITALIS_MONTHLY_REPORT_HOUR", "-1"),
        ("VITALIS_MONTHLY_REPORT_MINUTE", "-1"),
        ("ZEPP_MOCK", "perhaps"),
        ("DATABASE_URL", "mysql://host/db"),
        ("VITALIS_TIMEZONE", "not/a/timezone"),
        ("VITALIS_PUBLIC_URL", "ftp://example.com"),
        ("ZEPP_PAIRING_TTL_MINUTES", "0"),
        ("PUSHPLUS_QUERY_MAX_ATTEMPTS", "0"),
        ("PUSHPLUS_QUERY_INTERVAL_SECONDS", "0"),
    ],
)
def test_invalid_configuration_fails_by_key(name, value):
    with pytest.raises(ValueError, match=name):
        load_settings({name: value})


@pytest.mark.parametrize("environment", ["prod", "production"])
def test_production_requires_real_source_and_never_defaults_to_mock(environment):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    values = load_settings({
        "VITALIS_ENV": environment, "VITALIS_TOKEN_ENCRYPTION_KEY": key,
    })
    assert values.zepp_mock is False
    with pytest.raises(ValueError, match="ZEPP_MOCK"):
        load_settings({"VITALIS_ENV": environment, "ZEPP_MOCK": "true"})
    with pytest.raises(ValueError, match="VITALIS_TOKEN_ENCRYPTION_KEY"):
        load_settings({"VITALIS_ENV": environment})


def test_unknown_environment_cannot_silently_enable_mock():
    with pytest.raises(ValueError, match="VITALIS_ENV"):
        load_settings({"VITALIS_ENV": "produciton"})


def test_explicit_mock_connector_cannot_bypass_production_policy(monkeypatch):
    from vitalis.adapters.zepp import ZeppConnector
    from vitalis.config import settings

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "zepp_mock", False)
    with pytest.raises(ValueError, match="ZEPP_MOCK"):
        ZeppConnector(mock=True)


def test_pairing_origins_require_exact_browser_origins():
    extension_origin = "chrome-extension://" + "a" * 32
    values = load_settings({
        "VITALIS_PAIRING_ALLOWED_ORIGINS": extension_origin,
        "PORT": "8123",
    })
    assert extension_origin in values.pairing_allowed_origins
    assert "https://watchface.zepp.com" in values.pairing_allowed_origins
    assert "http://localhost:8123" in values.pairing_allowed_origins

    for value in ("*", "null", "https://evil.example/path", "http://a@localhost", "chrome-extension://bad"):
        with pytest.raises(ValueError, match="VITALIS_PAIRING_ALLOWED_ORIGINS"):
            load_settings({"VITALIS_PAIRING_ALLOWED_ORIGINS": value})


def test_settings_hold_server_delivery_config_without_leaking_secret():
    values = load_settings({
        "PUSHPLUS_TOKEN": "secret", "PUSHPLUS_ACCESS_KEY": "query-secret",
        "VITALIS_PUSH_USER": "owner",
    })
    assert values.push_user == "owner"
    assert values.pushplus_token == "secret"
    assert values.pushplus_access_key == "query-secret"
    assert values.pushplus_query_max_attempts == 3
    assert values.pushplus_query_interval_seconds == 60
    assert values.weekly_report_enabled is False
    assert values.monthly_report_enabled is False


def test_calendar_report_schedule_is_explicitly_configurable():
    values = load_settings({
        "VITALIS_WEEKLY_REPORT_ENABLED": "true",
        "VITALIS_WEEKLY_REPORT_HOUR": "8",
        "VITALIS_WEEKLY_REPORT_MINUTE": "15",
        "VITALIS_MONTHLY_REPORT_ENABLED": "true",
        "VITALIS_MONTHLY_REPORT_HOUR": "9",
        "VITALIS_MONTHLY_REPORT_MINUTE": "45",
    })
    assert values.weekly_report_enabled is True
    assert (values.weekly_report_hour, values.weekly_report_minute) == (8, 15)
    assert values.monthly_report_enabled is True
    assert (values.monthly_report_hour, values.monthly_report_minute) == (9, 45)
