"""Synthetic ACTIVE_ORIGIN_DATA protobuf/ZIP decoding evidence."""

from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from zipfile import ZIP_STORED, ZipFile

import pytest

from vitalis.adapters.zepp.dense_hr import DenseHeartRateDecodeError
from vitalis.adapters.zepp.raw_activity import (
    ActivityArchiveDecodeError,
    decode_activity_archive,
)
from vitalis.domain import DenseDataFile

DAY = date(2026, 11, 1)
START = datetime(2026, 10, 31, 16, tzinfo=UTC)


def varint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint((number << 3) | 2) + varint(len(value)) + value


def content(slots=None):
    raw = bytearray(bytes([126, 0, 0, 255, 0, 0, 0, 0]) * 1440)
    for minute, block in (slots or {0: [1, 2, 3, 60, 4, 5, 6, 7]}).items():
        raw[minute * 8 : minute * 8 + 8] = bytes(block)
    return bytes(raw)


def protobuf(sn="WATCH0001", hr=60, source=9568512, day=DAY, raw=None):
    return b"".join(
        (
            field(1, source),
            field(2, sn.encode()),
            field(
                3, raw if raw is not None else content({0: [1, 2, 3, hr, 4, 5, 6, 7]})
            ),
            field(4, day.isoformat().encode()),
        )
    )


def zip_bytes(members):
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as zipped:
        for name, payload in members:
            zipped.writestr(name, payload)
    return buffer.getvalue()


def archive():
    return zip_bytes(
        [
            ("watch.pb", protobuf()),
            ("strap.pb", protobuf("STRAP0002", 80, 10289410)),
            ("virtual.pb", protobuf("", 70, -1)),
        ]
    )


def index(device="WATCH0001", **updates):
    values = {
        "file_id": "synthetic-private-file",
        "file_type": "ACTIVE_ORIGIN_DATA",
        "stream": "activity_origin",
        "date": DAY,
        "device_id": device,
        "source_scope": "device" if device else "unknown",
        "start_utc": START,
        "end_utc": START + timedelta(days=1),
    }
    values.update(updates)
    return DenseDataFile(**values)


def decode(payload, rows=None, **kwargs):
    return decode_activity_archive(
        payload,
        [index()] if rows is None else rows,
        timezone_name=kwargs.pop("timezone_name", "Asia/Shanghai"),
        **kwargs,
    )


def test_activity_archive_keeps_two_devices_and_unknown_app_lineage():
    out = decode(archive(), [index(), index("STRAP0002")])
    hr = [sample for sample in out.samples if sample.metric == "heart_rate"]
    assert {(sample.device_id, sample.value) for sample in hr} == {
        ("WATCH0001", 60),
        ("STRAP0002", 80),
        (None, 70),
    }
    assert {sample.timestamp for sample in hr} == {START}
    assert [(row.parse_status, row.sample_count) for row in out.files] == [
        ("decoded", 1),
        ("decoded", 1),
    ]
    assert all(
        sample.source_scope == "unknown" for sample in hr if sample.device_id is None
    )
    assert not any(sample.source_scope == "user_fused" for sample in out.samples)
    assert not any(sample.metric in {"stress", "sleep_stage"} for sample in out.samples)
    assert out.entry_count == 3


@pytest.mark.parametrize("mode", [126, 255, 251])
def test_missing_and_unknown_modes_keep_all_raw_codes_without_sleep_inference(mode):
    raw = content({0: [mode, 252, 253, 255, 254, 249, 248, 247]})
    out = decode(zip_bytes([("watch.pb", protobuf(raw=raw))]))
    first = {
        sample.metric: sample for sample in out.samples if sample.timestamp == START
    }
    for metric, value in {
        "raw_sleep_mode": mode,
        "raw_activity": 252,
        "raw_step": 253,
        "raw_retain": 254,
        "raw_sleep_posture": 249,
        "raw_sleep_deep": 248,
        "raw_sleep_rem": 247,
    }.items():
        assert first[metric].value == value
        assert first[metric].unit == "vendor_code"
        assert first[metric].qualification == "raw_not_sleep_stage"
    assert "heart_rate" not in first


@pytest.mark.parametrize(
    "number,value",
    [(1, 3), (2, b"WATCH0001"), (3, content()), (4, b"2026-11-01")],
    ids=["source", "sn", "content", "day"],
)
def test_repeated_singular_fields_are_rejected(number, value):
    with pytest.raises(ActivityArchiveDecodeError, match="repeated"):
        decode(zip_bytes([("watch.pb", protobuf() + field(number, value))]))


@pytest.mark.parametrize("number,value", [(1, b"\x01"), (2, 1), (3, 1), (4, 1)])
def test_wrong_wire_types_are_rejected(number, value):
    values = {1: 1, 2: b"WATCH0001", 3: content(), 4: b"2026-11-01"}
    values[number] = value
    payload = b"".join(field(key, item) for key, item in values.items())
    with pytest.raises(ActivityArchiveDecodeError, match="wire"):
        decode(zip_bytes([("watch.pb", payload)]))


@pytest.mark.parametrize("source", [2**31, 2**32 - 1, 2**63, 2**64])
def test_source_must_be_valid_signed_int32_wire_value(source):
    # Avoid the fixture's signed-to-uint64 encoding for the overlong integer.
    encoded = b"\x80" * 9 + b"\x02" if source == 2**64 else varint(source)
    payload = (
        b"\x08"
        + encoded
        + field(2, b"WATCH0001")
        + field(3, content())
        + field(4, b"2026-11-01")
    )
    with pytest.raises(ActivityArchiveDecodeError):
        decode(zip_bytes([("watch.pb", payload)]))


@pytest.mark.parametrize(
    "payload", [b"\x80", b"\x00", b"\x1a\xff", b"", protobuf(raw=b"bad")]
)
def test_invalid_or_unsupported_protobuf_uses_dense_decode_error(payload):
    with pytest.raises(ActivityArchiveDecodeError):
        decode(zip_bytes([("watch.pb", payload)]))
    assert issubclass(ActivityArchiveDecodeError, DenseHeartRateDecodeError)


@pytest.mark.parametrize(
    "name",
    [
        "../watch.pb",
        "/watch.pb",
        "folder/watch.pb",
        "folder\\watch.pb",
        "C:watch.pb",
        "watch.txt",
    ],
)
def test_archive_rejects_invalid_member_paths(name):
    with pytest.raises(ActivityArchiveDecodeError):
        decode(zip_bytes([(name, protobuf())]))


def test_archive_read_failures_use_dense_decode_error():
    payload = bytearray(zip_bytes([("watch.pb", protobuf())]))
    payload[30 + len("watch.pb") + 10] ^= 1  # Break the stored entry's CRC.
    with pytest.raises(ActivityArchiveDecodeError):
        decode(bytes(payload))
    with pytest.raises(ActivityArchiveDecodeError):
        decode(b"not ZIP")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [index("OTHER0003")],
        [index(None, source_scope="device")],
        [index(), index("OTHER0003")],
    ],
)
def test_every_index_row_requires_an_exact_source_match(rows):
    with pytest.raises(ActivityArchiveDecodeError, match="index|match"):
        decode(zip_bytes([("watch.pb", protobuf())]), rows)


def test_unknown_identity_index_is_a_container_and_never_a_device_assignment():
    out = decode(archive(), [index(None)])
    assert [
        (row.source_scope, row.device_id, row.sample_count) for row in out.files
    ] == [
        ("unknown", None, 3),
    ]
    hr = [sample for sample in out.samples if sample.metric == "heart_rate"]
    assert {(sample.source_scope, sample.device_id) for sample in hr} == {
        ("device", "WATCH0001"),
        ("device", "STRAP0002"),
        ("unknown", None),
    }
    assert len({sample.source_record_id for sample in hr}) == 3


def test_aliases_are_exact_and_samples_keep_original_serial_identity():
    out = decode(
        archive(),
        [index("AA:BB:CC:DD:EE:FF")],
        device_aliases={"WATCH0001": "AA:BB:CC:DD:EE:FF"},
    )
    samples = [
        sample
        for sample in out.samples
        if sample.value == 60 and sample.metric == "heart_rate"
    ]
    assert {sample.device_id for sample in samples} == {"WATCH0001"}
    with pytest.raises(ActivityArchiveDecodeError):
        decode(archive(), [index("WATCH000")])


def test_alias_collapsed_archive_sources_are_rejected():
    with pytest.raises(ActivityArchiveDecodeError, match="duplicate|ambiguous"):
        decode(archive(), device_aliases={"STRAP0002": "WATCH0001"})


def test_source_identity_is_stable_opaque_and_separates_minute_from_second_data():
    payload = zip_bytes([("watch.pb", protobuf())])
    first = decode(payload)
    again = decode(payload)
    other_file = decode(payload, [index(file_id="different-private-file")])
    identities = {sample.source_record_id for sample in first.samples}
    assert len(identities) == 1
    identity = identities.pop()
    assert identity and identity.startswith("activity_origin:")
    assert "synthetic-private-file" not in identity and "WATCH0001" not in identity
    assert identity == again.samples[0].source_record_id
    assert identity != other_file.samples[0].source_record_id
    assert all(sample.sample_ordinal is not None for sample in first.samples)
    assert {
        sample.source_field for sample in first.samples if sample.metric == "heart_rate"
    } == {
        "ACTIVE_ORIGIN_DATA.content[3]",
    }


def test_index_date_and_window_are_both_enforced_with_frozen_timezone():
    raw = content(
        {
            0: [1, 0, 0, 60, 0, 0, 0, 0],
            1: [1, 0, 0, 61, 0, 0, 0, 0],
            2: [1, 0, 0, 62, 0, 0, 0, 0],
        }
    )
    payload = zip_bytes([("watch.pb", protobuf(raw=raw))])
    row = index(
        start_utc=START + timedelta(minutes=1), end_utc=START + timedelta(minutes=2)
    )
    out = decode(payload, [row])
    assert out.files[0].sample_count == 1
    assert {sample.timestamp for sample in out.samples} == {
        START + timedelta(minutes=1)
    }
    with pytest.raises(ActivityArchiveDecodeError):
        decode(payload, [index(date=DAY + timedelta(days=1))])
    with pytest.raises(ActivityArchiveDecodeError):
        decode(
            payload,
            [
                index(
                    start_utc=START + timedelta(days=1),
                    end_utc=START + timedelta(days=2),
                )
            ],
        )
    with pytest.raises(ActivityArchiveDecodeError):
        decode(payload, timezone_name="invalid/frozen-zone")


def test_date_only_index_and_naive_utc_index_are_supported():
    payload = zip_bytes([("watch.pb", protobuf())])
    for row in [
        index(start_utc=None, end_utc=None),
        index(start_utc=START.replace(tzinfo=None)),
    ]:
        out = decode(payload, [row])
        assert out.files[0].sample_count == 1
        assert (
            next(
                sample for sample in out.samples if sample.metric == "heart_rate"
            ).timestamp
            == START
        )


def test_local_slots_resolve_dst_using_the_frozen_zone():
    raw = content({180: [1, 0, 0, 65, 0, 0, 0, 0]})
    row = index(start_utc=None, end_utc=None)
    out = decode(
        zip_bytes([("watch.pb", protobuf(raw=raw))]),
        [row],
        timezone_name="America/New_York",
    )
    hr = [sample for sample in out.samples if sample.metric == "heart_rate"]
    assert hr[0].timestamp == datetime(2026, 11, 1, 8, tzinfo=UTC)
    assert hr[0].sample_ordinal == 180
