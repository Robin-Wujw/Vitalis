"""Allowlisted Zepp identity metadata; pairing credentials never leave the adapter."""
import json
import re

from .supplemental import number

DEVICE_FIELDS = frozenset({
    "deviceSource", "deviceType", "productId", "productVersion", "model", "brand",
    "firmwareVersion", "hardwareVersion", "softwareVersion", "activeStatus", "bindingStatus",
    "applicationTime", "lastSyncTime", "lastBindingTime", "bindingTime",
})


def profile(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, raw, minimum, maximum in (
        ("height_cm", data.get("height"), 30, 300),
        ("weight_kg", data.get("weight_float", data.get("weight")), 1, 1000),
        ("gender_code_unverified", data.get("gender"), 0, 255),
    ):
        value = number(raw, minimum, maximum)
        if value is not None:
            result[key] = value
    birthday = data.get("birthday")
    if isinstance(birthday, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])(?:-\d{2})?", birthday):
        result["birth_month"] = birthday[:7]
    config = data.get("config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = None
    if isinstance(config, dict) and isinstance(config.get("wearHand"), str):
        result["wear_hand"] = config["wearHand"][:32].lower()
    return result


def devices(payload: dict) -> list[dict]:
    items = payload.get("items", payload.get("data"))
    if isinstance(items, dict):
        items = items.get("items", items.get("devices"))
    if not isinstance(items, list):
        return []
    return [{key: value for key, value in item.items() if key in DEVICE_FIELDS
             and isinstance(value, (str, int, float, bool))}
            for item in items if isinstance(item, dict)]
