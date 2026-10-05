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
    ],
)
def test_invalid_configuration_fails_by_key(name, value):
    with pytest.raises(ValueError, match=name):
        load_settings({name: value})


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
    values = load_settings({"PUSHPLUS_TOKEN": "secret", "VITALIS_PUSH_USER": "owner"})
    assert values.push_user == "owner"
    assert values.pushplus_token == "secret"
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
