from configparser import ConfigParser
from pathlib import Path


def test_worker_unit_caps_memory_and_swap():
    unit = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "vitalis-worker.service"
    config = ConfigParser(interpolation=None)
    assert config.read(unit, encoding="utf-8")
    assert config.get("Service", "MemoryHigh") == "250M"
    assert config.get("Service", "MemoryMax") == "320M"
    assert config.get("Service", "MemorySwapMax") == "128M"
