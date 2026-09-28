"""Pure Zepp detail classifications and the storage gate for unknown payloads."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from vitalis.adapters.zepp.client import MockZeppClient
from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
from vitalis.adapters.zepp.parser import WorkoutDetailLimitError
from vitalis.adapters.zepp.parsers import ParseContext, parse_workout_detail
from vitalis.adapters.zepp.sync_manager import SyncManager
from vitalis.domain import User
from vitalis.time import local_day_utc_bounds


TRACK_ID = int(datetime(2026, 8, 1, 7, tzinfo=timezone.utc).timestamp())
CONTEXT = ParseContext(training_family="strength")


def test_verified_series_and_additional_vendor_metadata_remain_recognized():
    result = parse_workout_detail({"data": {
        "trackid": TRACK_ID, "heart_rate": "1,80", "futureMetadata": {"flag": True},
    }}, CONTEXT)
    assert result.status == "recognized"
    assert result.detail is not None
    assert result.detail.samples
    assert result.diagnostics == ()


def test_trackid_only_and_known_empty_strength_fields_are_explicitly_empty():
    for payload in (
        {"data": {"trackid": TRACK_ID}},
        {"data": {"trackid": TRACK_ID, "strengthSets": "[]", "memo": "", "strengthAssess": {}}},
    ):
        result = parse_workout_detail(payload, CONTEXT)
        assert result.status == "empty"
        assert result.detail is not None
        assert not result.detail.samples and not result.detail.strength_sets


def test_mock_history_has_windowed_stable_ids_and_parseable_empty_detail():
    client = MockZeppClient(seed=7)
    start = int(datetime(2026, 8, 28, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    history = client.fetch_sport_history("run", start, end)
    rows = history["data"]["items"]
    assert rows and history["data"]["next"] == -1
    assert history == client.fetch_sport_history("run", start, end)
    assert all(start <= int(item["trackid"]) < end for item in rows)
    for item in rows:
        detail = parse_workout_detail(
            client.fetch_sport_detail(str(item["trackid"]), item["source"]),
            ParseContext(training_family="aerobic"),
        )
        assert detail.status == "empty" and detail.detail is not None
    sunday_start, sunday_end = local_day_utc_bounds(date(2026, 8, 30), "Asia/Shanghai")
    assert client.fetch_sport_history(
        "run", int(sunday_start.timestamp()), int(sunday_end.timestamp())
    )["data"]["items"] == []


def test_unknown_nonempty_and_malformed_detail_never_appear_empty():
    private_value = "SYNTHETIC_PRIVATE_CONTENT"
    for payload in (
        {"data": {"trackid": TRACK_ID, "unknownField": {"token": private_value}}},
        {"data": {"trackid": "not-a-time", "memo": private_value}},
        {"data": {"trackid": TRACK_ID}, "futureEnvelope": private_value},
        {"data": "malformed"},
    ):
        result = parse_workout_detail(payload, CONTEXT)
        assert result.status == "unrecognized"
        assert result.detail is None
        assert private_value not in repr(result.diagnostics)


def test_resource_limit_is_not_disguised_as_successful_empty_detail():
    payload = {"data": {"trackid": TRACK_ID, "heart_rate": ";".join(["1,80"] * 20_002)}}
    with pytest.raises(WorkoutDetailLimitError):
        parse_workout_detail(payload, CONTEXT)


class _Repository:
    def __init__(self):
        self.saved = []

    def workout(self, _user_id, _workout_id, *, source):
        return SimpleNamespace(data={"training_family": "strength"})

    def save_workout_detail(self, user_id, workout_id, detail, **kwargs):
        self.saved.append((user_id, workout_id, detail, kwargs))
        return True


def test_sync_gate_rejects_unknown_without_writing_but_saves_known_empty():
    repo = _Repository()
    manager = SyncManager(None)
    user = User(id="synthetic-owner")

    def fetched(data):
        return FetchedRecord(RawRecord(
            "workout_detail", f"workout_detail:{TRACK_ID}:run",
            datetime(2026, 8, 1, 7, tzinfo=timezone.utc), None,
            {"data": {"trackid": TRACK_ID, **data}},
        ))

    unknown = manager._persist_record(fetched({"unknownField": "opaque"}), repo, user)
    assert unknown.status == "unverified"
    assert unknown.parse_status == "unrecognized"
    assert unknown.error_kind == "unrecognized_payload"
    assert not repo.saved

    empty = manager._persist_record(fetched({"strengthSets": "[]"}), repo, user)
    assert empty.status == "success"
    assert empty.parse_status == "empty"
    assert len(repo.saved) == 1
