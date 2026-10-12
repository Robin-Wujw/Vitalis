from configparser import ConfigParser
from pathlib import Path


def test_worker_unit_caps_memory_and_swap():
    unit = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "vitalis-worker.service"
    config = ConfigParser(interpolation=None)
    assert config.read(unit, encoding="utf-8")
    assert config.get("Service", "MemoryHigh") == "500M"
    assert config.get("Service", "MemoryMax") == "640M"
    assert config.get("Service", "MemorySwapMax") == "128M"
    # The 30 s heartbeat must fit many times into the watchdog window.
    assert config.get("Service", "WatchdogSec") == "600"
    assert config.get("Service", "Restart") == "on-failure"
    assert config.get("Service", "LimitCORE") == "0"
    assert not config.has_option("Unit", "Requires")
    assert "vitalis.service" not in config.get("Unit", "After")


def test_api_unit_does_not_set_obsolete_scheduler_override():
    unit = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "systemd" / "vitalis-api.service"
    )
    assert "VITALIS_NO_SCHEDULER" not in unit.read_text(encoding="utf-8")


def test_service_units_share_nonroot_state_and_canonical_cli():
    root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    for name, command in (
        ("vitalis-api.service", "serve"),
        ("vitalis-worker.service", "worker"),
    ):
        config = ConfigParser(interpolation=None)
        assert config.read(root / name, encoding="utf-8")
        assert config.get("Service", "User") == "vitalis"
        assert config.get("Service", "Group") == "vitalis"
        assert config.get("Service", "StateDirectory") == "vitalis"
        assert config.get("Service", "EnvironmentFile") == "/etc/vitalis/vitalis.env"
        assert config.get("Service", "WorkingDirectory") == "/var/lib/vitalis"
        assert config.get("Service", "ExecStart") == f"/opt/vitalis/.venv/bin/vitalis {command}"
        assert config.get("Service", "NoNewPrivileges") == "true"
