"""Synthetic source-time regressions; never use device or account data."""

import base64
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from vitalis.adapters.zepp.fetcher import FetchedRecord, FetchWindow, RawRecord
from vitalis.adapters.zepp.parser import ZeppParser
from vitalis.adapters.zepp.sync_manager import SyncManager
from vitalis.config import settings
from vitalis.domain import User


def _band(day, summary, minute=540, reading=60):
    return {"data": {"items": [{
        "date": day,
        "summary": base64.b64encode(json.dumps(summary).encode()).decode(),
        "data_hr": base64.b64encode(bytes([0] * minute + [reading])).decode(),
    }]}}


@pytest.mark.parametrize(("day", "offset", "expected_hour"), [
    ("2026-03-08", -18000, 13),  # spring transition: 09:00 is EDT
    ("2026-11-01", -14400, 14),  # fall transition: 09:00 is EST
])
@pytest.mark.parametrize("with_payload_zone", [True, False])
def test_band_hr_uses_frozen_zone_at_sample_clock_not_midnight(
    monkeypatch, day, offset, expected_hour, with_payload_zone,
):
    monkeypatch.setattr(settings, "timezone", "Asia/Tokyo")
    payload = _band(day, {"tz": offset} if with_payload_zone else {})
    samples = ZeppParser.parse_band_heart_rate(payload, timezone_name="America/New_York")
    expected = datetime.combine(date.fromisoformat(day), datetime.min.time(), timezone.utc)
    assert [sample.timestamp for sample in samples] == [expected + timedelta(hours=expected_hour)]
    assert [sample.value for sample in samples] == [60]


@pytest.mark.parametrize("offset", [0, 19800, -18000])
def test_band_hr_default_preserves_valid_payload_offset(monkeypatch, offset):
    monkeypatch.setattr(settings, "timezone", "Pacific/Auckland")
    samples = ZeppParser.parse_band_heart_rate(_band("2026-03-08", {"tz": offset}, minute=1))
    assert samples[0].timestamp == datetime(2026, 3, 8, 0, 1, tzinfo=timezone.utc) - timedelta(seconds=offset)


def test_band_hr_missing_zone_defaults_like_main_sleep(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "Asia/Kolkata")
    samples = ZeppParser.parse_band_heart_rate(_band("2026-03-08", {}, minute=1))
    assert samples[0].timestamp == datetime(2026, 3, 7, 18, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize("reading", [0, 255])
def test_band_hr_missing_readings_do_not_manufacture_zero(reading):
    assert ZeppParser.parse_band_heart_rate(
        _band("2026-03-08", {}, reading=reading), timezone_name="America/New_York",
    ) == []


@pytest.mark.parametrize("day,expected_hour", [("2026-03-08", 13), ("2026-11-01", 14)])
def test_sleep_response_watermark_uses_frozen_zone_and_only_same_response(
    monkeypatch, day, expected_hour,
):
    monkeypatch.setattr(settings, "timezone", "Asia/Tokyo")
    zone = "America/New_York"
    window = FetchWindow.local_dates(date.fromisoformat(day), date.fromisoformat(day), zone)
    saved = []
    repo = SimpleNamespace(
        save_daily=lambda daily: None,
        save_metric_samples=lambda samples: saved.extend(samples) or len(samples),
    )
    manager = SyncManager(SimpleNamespace(timezone_name=zone))
    user = User(id="synthetic-time-reader")

    def write(payload):
        return manager._write_stream_result(FetchedRecord(raw=RawRecord(
            stream="sleep", source_key="band:synthetic", start_utc=window.start,
            end_utc=window.end, payload=payload,
        )), repo, user)

    result = write(_band(day, {"slp": {"st": "00:30", "ed": "08:00", "wk": 0}}))
    expected = f"{day}T{expected_hour:02}:00:00+00:00"
    assert result.sleep_dates == [day]
    assert result.heart_rate_seen_until == {day: expected}
    assert saved[0].timestamp.isoformat() == expected
    empty = write(_band(day, {"slp": {"st": "00:30", "ed": "08:00", "wk": 0}}, reading=255))
    assert empty.sleep_dates == [day]
    assert empty.heart_rate_seen_until == {}
