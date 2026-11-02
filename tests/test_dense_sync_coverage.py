"""Dense sync coverage regressions using only synthetic indexes and ZIPs."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import select

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.adapters.persistence import models as orm
from vitalis.adapters.zepp.fetcher import FetchWindow
from vitalis.adapters.zepp.sync_coordinator import ZeppSyncCoordinator
from vitalis.domain import DenseDataFile


START = datetime(2026, 11, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=10)
WINDOW = FetchWindow(START, START + timedelta(days=2))


def _varint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _archive(blocks):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for index, (start, values) in enumerate(blocks):
            inner = b"\x08" + _varint(int(start.timestamp()))
            inner += b"".join(b"\x10" + _varint(value) for value in values)
            archive.writestr(f"entry-{index}.pb", b"\x0a" + _varint(len(inner)) + inner)
    return output.getvalue()


def _sample(file_id, start, end, device="DEVICE000A", file_type="SEC_HR"):
    return {
        "s": int((start - START).total_seconds() * 1000),
        "e": int((end - START).total_seconds() * 1000),
        "fileId": file_id, "fileType": file_type,
        "dateString": start.date().isoformat(), "deviceId": device,
    }


def _payload(samples):
    return {"items": [{"value": {
        "startTime": int(START.timestamp() * 1000), "samples": samples,
    }}]}


class DenseConnector:
    source_mode = "mock"

    def __init__(self, payload, archives=None):
        self.payload = payload
        self.archives = archives or {}
        self.downloads = []
        self.assert_network_boundary = lambda: None

    def fetch_file_info_events(self, event_type, *_args):
        self.assert_network_boundary()
        return self.payload if event_type == "second_heart_rate" else {"items": []}

    def download_dense_file(self, file_type, file_id):
        self.assert_network_boundary()
        self.downloads.append((file_type, file_id))
        return self.archives[file_id]


def _attempt(user_id, connector, *, options=None, window=WINDOW, trigger="manual"):
    with session_scope() as db:
        HealthRepository(db).delete_for_user(user_id)
    coordinator = ZeppSyncCoordinator(connector=connector, wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt(user_id, window=window, options=options, trigger=trigger)
    assert attempt is not None
    # Scope execution to the dense index roots; no fake network for other streams.
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(attempt.id):
            if chunk.stream != "dense_files" or chunk.partition != "dense_index":
                chunk.status = "succeeded"
    return coordinator, attempt


def _dense_chunks(coordinator, attempt_id):
    return [row for row in coordinator.status(attempt_id)["chunks"] if row["stream"] == "dense_archive"]


@pytest.mark.parametrize("trigger", ["manual", "morning", "evening", "nightly", "pairing_initial"])
def test_full_attempt_defaults_to_dense_decoding(trigger):
    coordinator, attempt = _attempt(
        f"dense-default-{trigger}", DenseConnector(_payload([])), trigger=trigger,
    )
    assert attempt.options["decode_dense_files"] is True
    explicit = coordinator.create_attempt(
        attempt.user_id, window=WINDOW, trigger=trigger, options={"decode_dense_files": True},
    )
    assert explicit.id == attempt.id


@pytest.mark.parametrize("options,trigger", [
    ({"sleep_only": True}, "morning_probe"),
    ({"workout_only": True}, "manual"),
])
def test_specialized_attempt_does_not_auto_decode(options, trigger):
    coordinator = ZeppSyncCoordinator(connector=DenseConnector(_payload([])), wall_clock=lambda: NOW)
    attempt = coordinator.create_attempt(
        f"dense-specialized-{trigger}", window=WINDOW,
        options={**options, "decode_dense_files": None}, trigger=trigger,
    )
    assert attempt.options["decode_dense_files"] is False
    assert not any(row["stream"] == "dense_files" for row in coordinator.status(attempt.id)["chunks"])


def test_every_archive_is_persisted_and_resumed_one_at_a_time():
    samples = []
    archives = {}
    for index in range(4):
        start = START + timedelta(hours=index * 6)
        samples.append(_sample(f"file-{index}", start, start + timedelta(seconds=2)))
        archives[f"file-{index}"] = _archive([(start, [60 + index, 61 + index, 62 + index])])
    # Repeated index rows and devices must not duplicate archive requests.
    samples.append(dict(samples[0]))
    connector = DenseConnector(_payload(samples), archives)
    coordinator, attempt = _attempt("dense-all-resume", connector)
    original_session = coordinator._session
    active_sessions = []

    @contextmanager
    def observed_session(**kwargs):
        with original_session(**kwargs) as db:
            active_sessions.append(db)
            try:
                yield db
            finally:
                active_sessions.remove(db)

    coordinator._session = observed_session
    connector.assert_network_boundary = lambda: (
        pytest.fail("network inside database transaction") if active_sessions else None
    )
    coordinator.drain_once(attempt.id)
    assert len(_dense_chunks(coordinator, attempt.id)) == 4
    assert connector.downloads == []
    assert coordinator.status(attempt.id)["attempt"]["status"] == "queued"
    # A restarted worker uses the persisted children, not the original object.
    restarted = ZeppSyncCoordinator(connector=connector, wall_clock=lambda: NOW)
    for count in range(1, 5):
        restarted.drain_once(attempt.id)
        assert len(connector.downloads) == count
        assert len(_dense_chunks(restarted, attempt.id)) == 4
        assert restarted.status(attempt.id)["attempt"]["status"] == ("succeeded" if count == 4 else "queued")
    assert len(set(connector.downloads)) == 4
    with session_scope() as db:
        files = HealthRepository(db).dense_data_files(attempt.user_id, "second_heart_rate", START.date(), WINDOW.end.date())
        assert len(files) == 4
        assert {row.parse_status for row in files} == {"decoded"}
        assert db.execute(select(orm.MetricSample).where(orm.MetricSample.user_id == attempt.user_id)).scalars().all()


@pytest.mark.parametrize("status", ["decoded", "no_data"])
def test_same_file_id_new_end_range_is_indexed_and_decoded_again(status):
    start = START + timedelta(hours=1)
    connector = DenseConnector(
        _payload([_sample("growing", start, start + timedelta(seconds=5))]),
        {"growing": _archive([(start, [60, 61, 62, 63, 64, 65])])},
    )
    coordinator, attempt = _attempt(f"dense-range-{status}", connector, options={"decode_dense_files": True})
    with session_scope() as db:
        HealthRepository(db).save_dense_data_files([DenseDataFile(
            user_id=attempt.user_id, stream="second_heart_rate", file_id="growing", file_type="SEC_HR",
            date=start.date(), start_utc=start, end_utc=start + timedelta(seconds=1),
            source_scope="device", device_id="DEVICE000A", parse_status=status, sample_count=2,
        )])
    coordinator.drain_once(attempt.id)
    assert len(_dense_chunks(coordinator, attempt.id)) == 1
    with session_scope() as db:
        row, = HealthRepository(db).dense_data_file_group(attempt.user_id, "second_heart_rate", "growing")
        assert row.end_utc == (start + timedelta(seconds=5)).replace(tzinfo=None)
        assert row.parse_status == "indexed"
    assert coordinator.drain_once(attempt.id).success
    with session_scope() as db:
        row, = HealthRepository(db).dense_data_file_group(attempt.user_id, "second_heart_rate", "growing")
        assert row.parse_status == "decoded" and row.sample_count == 6


def test_metadata_only_is_explicit_partial_not_dense_coverage():
    connector = DenseConnector(_payload([_sample("metadata-only", START, START + timedelta(seconds=2))]))
    coordinator, attempt = _attempt("dense-metadata-only", connector, options={"decode_dense_files": False})
    report = coordinator.drain_once(attempt.id)
    state = coordinator.status(attempt.id)
    assert not report.success
    assert state["attempt"]["status"] == "partial"
    assert state["attempt"]["error_kind"] == "partial_coverage"
    dense = next(row for row in state["chunks"] if row["stream"] == "dense_files")
    assert dense["stages"]["capability"] == "indexed"
    assert next(stream for stream in report.streams if stream.stream == "heart_rate/dense_file").capability == "indexed"
    assert dense["stages"]["incomplete"] is True
    assert _dense_chunks(coordinator, attempt.id) == []
    assert connector.downloads == []


def test_failed_archive_does_not_claim_decoded_or_complete_coverage():
    connector = DenseConnector(
        _payload([_sample("broken", START, START + timedelta(seconds=2))]), {"broken": b"not a ZIP"},
    )
    coordinator, attempt = _attempt("dense-broken", connector, options={"decode_dense_files": True})
    coordinator.drain_once(attempt.id)
    report = coordinator.drain_once(attempt.id)
    assert not report.success
    with session_scope() as db:
        row, = HealthRepository(db).dense_data_file_group(attempt.user_id, "second_heart_rate", "broken")
        assert row.parse_status == "indexed" and row.sample_count == 0
    chunk, = _dense_chunks(coordinator, attempt.id)
    assert chunk["status"] == "failed"
    assert chunk["parse_status"] == "failed"
    assert chunk["error_kind"] == "unrecognized_payload"


def test_repeated_unchanged_index_skips_archive_without_erasing_decoded_state():
    connector = DenseConnector(
        _payload([_sample("stable", START, START + timedelta(seconds=2))]),
        {"stable": _archive([(START, [60, 61, 62])])},
    )
    coordinator, first = _attempt("dense-stable", connector, options={"decode_dense_files": True})
    assert coordinator.run_attempt(first.id).success
    second = coordinator.create_attempt(first.user_id, window=WINDOW, options={"decode_dense_files": True})
    with session_scope() as db:
        for chunk in HealthRepository(db).sync_chunks(second.id):
            if chunk.stream != "dense_files" or chunk.partition != "dense_index":
                chunk.status = "succeeded"
    assert coordinator.run_attempt(second.id).success
    assert connector.downloads == [("SEC_HR", "stable")]
    assert _dense_chunks(coordinator, second.id) == []


def test_whole_archive_keeps_out_of_window_samples_and_device_identity():
    window = FetchWindow(START + timedelta(hours=8), START + timedelta(days=1, hours=8))
    first = START + timedelta(hours=8) - timedelta(seconds=2)
    second = START + timedelta(days=1, hours=8) - timedelta(seconds=1)
    connector = DenseConnector(_payload([
        _sample("whole", first, first + timedelta(seconds=3), "DEVICE000A"),
        _sample("whole", second, second + timedelta(seconds=3), "DEVICE000B"),
        _sample("unrelated", START, START + timedelta(seconds=2), "DEVICE000A"),
    ]), {"whole": _archive([(first, [60, 61, 62, 63]), (second, [80, 81, 82, 83])])})
    coordinator, attempt = _attempt("dense-whole-file", connector, window=window, options={"decode_dense_files": True})
    assert coordinator.run_attempt(attempt.id).success
    assert connector.downloads == [("SEC_HR", "whole")]
    with session_scope() as db:
        rows = db.execute(select(orm.MetricSample).where(orm.MetricSample.user_id == attempt.user_id)).scalars().all()
        assert len(rows) == 8
        assert {(row.device_id, row.value) for row in rows} == {
            *(('DEVICE000A', float(value)) for value in [60, 61, 62, 63]),
            *(('DEVICE000B', float(value)) for value in [80, 81, 82, 83]),
        }
        assert min(row.timestamp for row in rows) < window.start.replace(tzinfo=None)
        assert max(row.timestamp for row in rows) >= window.end.replace(tzinfo=None)
