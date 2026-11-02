"""Decode verified ActivityRawData envelopes without inferring sleep or pressure.

Zepp 10.8.7: fields 1 int32 deviceSource, 2 string SN, 3 bytes, 4 day.
The 1440 eight-byte local-clock slots contain mode, activity, step, HR,
retain, posture/probability, deep probability, REM probability. These are
native sleep-algorithm inputs, not final stages. Unknown codes stay raw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from io import BytesIO
from json import dumps
from lzma import LZMAError
from pathlib import PurePosixPath
from stat import S_ISLNK
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from vitalis.domain import DenseDataFile, MetricSample

from .dense_hr import (
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    MAX_ENTRY_BYTES,
    DecodedDenseHeartRate,
    DenseHeartRateDecodeError,
    _protobuf_fields,
)

MINUTES_PER_DAY = 1440
SLOT_BYTES = 8
RAW_FIELDS = (
    ("raw_sleep_mode", 0),
    ("raw_activity", 1),
    ("raw_step", 2),
    ("raw_retain", 4),
    ("raw_sleep_posture", 5),
    ("raw_sleep_deep", 6),
    ("raw_sleep_rem", 7),
)


class ActivityArchiveDecodeError(DenseHeartRateDecodeError):
    """The archive cannot establish the verified raw-activity contract."""


@dataclass(frozen=True)
class _ActivityEntry:
    day: date
    serial: str
    device_source: int
    content: bytes


@dataclass(frozen=True)
class _IndexWindow:
    day: date
    start: datetime
    end: datetime
    device: str

    def contains(self, stamp: datetime) -> bool:
        return self.start <= stamp < self.end


def _decode_envelope(payload: bytes) -> _ActivityEntry:
    expected_wire = {1: 0, 2: 2, 3: 2, 4: 2}
    fields: dict[int, int | bytes] = {}
    try:
        for number, wire, value in _protobuf_fields(payload):
            if number > (1 << 29) - 1 or (wire == 0 and value >= 1 << 64):
                raise ActivityArchiveDecodeError("invalid activity protobuf value")
            if number not in expected_wire:
                continue  # Unknown envelope fields are not interpreted.
            if number in fields:
                raise ActivityArchiveDecodeError("repeated activity protobuf field")
            if wire != expected_wire[number]:
                raise ActivityArchiveDecodeError("invalid activity protobuf wire type")
            fields[number] = value
    except DenseHeartRateDecodeError as exc:
        if isinstance(exc, ActivityArchiveDecodeError):
            raise
        raise ActivityArchiveDecodeError("invalid activity protobuf encoding") from exc
    if fields.keys() != expected_wire.keys():
        raise ActivityArchiveDecodeError("missing activity protobuf field")

    source = fields[1]
    # protobuf int32 negatives are sign-extended to a ten-byte uint64 varint.
    if source >= (1 << 64) - (1 << 31):
        source -= 1 << 64
    if not -(1 << 31) <= source < (1 << 31):
        raise ActivityArchiveDecodeError("invalid activity deviceSource int32")
    try:
        serial = fields[2].decode("utf-8")
        day_text = fields[4].decode("utf-8")
        day = date.fromisoformat(day_text)
    except (UnicodeError, ValueError) as exc:
        raise ActivityArchiveDecodeError("invalid activity protobuf text") from exc
    if day.isoformat() != day_text:
        raise ActivityArchiveDecodeError("invalid activity calendar day")
    if serial and (
        not 8 <= len(serial) <= 128 or not serial.isascii() or not serial.isalnum()
    ):
        raise ActivityArchiveDecodeError("invalid activity device serial")
    content = fields[3]
    if len(content) != MINUTES_PER_DAY * SLOT_BYTES:
        raise ActivityArchiveDecodeError("unsupported activity minute encoding")
    return _ActivityEntry(day, serial.upper(), source, content)


def _read_archive(archive: bytes) -> list[_ActivityEntry]:
    try:
        with ZipFile(BytesIO(archive)) as zipped:
            members = zipped.infolist()
            if not members or len(members) > MAX_ARCHIVE_ENTRIES:
                raise ActivityArchiveDecodeError("invalid activity archive entry count")
            if (
                sum(member.file_size for member in members)
                > MAX_ARCHIVE_UNCOMPRESSED_BYTES
            ):
                raise ActivityArchiveDecodeError("activity archive exceeds bounds")
            names: set[str] = set()
            entries = []
            for member in members:
                path = PurePosixPath(member.filename)
                if (
                    member.is_dir()
                    or path.name != member.filename
                    or "\\" in member.filename
                    or ":" in member.filename
                    or member.orig_filename != member.filename
                    or path.suffix.lower() != ".pb"
                    or not 0 < member.file_size <= MAX_ENTRY_BYTES
                    or S_ISLNK(member.external_attr >> 16)
                    or member.flag_bits & 1
                ):
                    raise ActivityArchiveDecodeError("invalid activity archive member")
                name = member.filename.casefold()
                if name in names:
                    raise ActivityArchiveDecodeError(
                        "duplicate activity archive member"
                    )
                names.add(name)
                entries.append(_decode_envelope(zipped.read(member)))
            return entries
    except (
        BadZipFile,
        OSError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        ZlibError,
        LZMAError,
    ) as exc:
        raise ActivityArchiveDecodeError(
            "activity ZIP archive could not be read"
        ) from exc


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _index_windows(
    indexed_files: list[DenseDataFile], zone: ZoneInfo
) -> list[_IndexWindow]:
    if not indexed_files:
        raise ActivityArchiveDecodeError("activity index is empty")
    if len({item.file_id for item in indexed_files}) != 1 or any(
        not item.file_id
        or item.stream != "activity_origin"
        or item.file_type != "ACTIVE_ORIGIN_DATA"
        for item in indexed_files
    ):
        raise ActivityArchiveDecodeError("activity index must identify one archive")
    windows = []
    for item in indexed_files:
        if not item.device_id and item.source_scope != "unknown":
            raise ActivityArchiveDecodeError("activity index has no device identity")
        start, end = _utc(item.start_utc), _utc(item.end_utc)
        day = item.date or (start.astimezone(zone).date() if start else None)
        if day is None:
            raise ActivityArchiveDecodeError("activity index has no calendar day")
        day_start = datetime.combine(day, time.min, zone).astimezone(UTC)
        day_end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(
            UTC
        )
        start, end = max(start or day_start, day_start), min(end or day_end, day_end)
        if end <= start:
            raise ActivityArchiveDecodeError(
                "activity index window does not match its day"
            )
        windows.append(_IndexWindow(day, start, end, (item.device_id or "").upper()))
    return windows


def _record_identity(file_id: str, entry: _ActivityEntry) -> str:
    # This identity is exposed as sample_id by the raw API: never embed fileId/SN.
    payload = dumps(
        [file_id, entry.day.isoformat(), entry.serial, entry.device_source],
        separators=(",", ":"),
    )
    return "activity_origin:" + sha256(payload.encode()).hexdigest()


def decode_activity_archive(
    archive: bytes,
    indexed_files: list[DenseDataFile],
    *,
    timezone_name: str,
    device_aliases: dict[str, str] | None = None,
) -> DecodedDenseHeartRate:
    """Validate atomically; scope counts to exact devices or explicit containers.

    A no-ID/unknown index is archive-container metadata, not a device assignment.
    Samples always retain the PB's own SN. An empty SN remains unknown even when
    deviceSource=-1; neither that sentinel nor mode codes prove App fusion.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ActivityArchiveDecodeError("invalid activity frozen timezone") from exc
    entries = _read_archive(archive)
    try:
        windows = _index_windows(indexed_files, zone)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, ActivityArchiveDecodeError):
            raise
        raise ActivityArchiveDecodeError("invalid activity index date window") from exc
    aliases = device_aliases or {}

    def identity(value: str) -> str:
        return aliases.get(value, value).upper() if value else ""

    seen: set[tuple[date, str]] = set()
    matched: list[set[int]] = [set() for _ in windows]
    stamps_by_entry: list[list[tuple[int, datetime]]] = []
    for entry_number, entry in enumerate(entries):
        key = (entry.day, identity(entry.serial))
        if key in seen:
            raise ActivityArchiveDecodeError(
                "duplicate or ambiguous activity source entry"
            )
        seen.add(key)
        start = datetime.combine(entry.day, time.min, zone)
        try:
            stamps = [
                (minute, (start + timedelta(minutes=minute)).astimezone(UTC))
                for minute in range(MINUTES_PER_DAY)
            ]
        except (OverflowError, ValueError) as exc:
            raise ActivityArchiveDecodeError("invalid activity slot timestamp") from exc
        matching_rows = [
            index
            for index, window in enumerate(windows)
            if window.day == entry.day
            and (not window.device or identity(window.device) == identity(entry.serial))
            and any(window.contains(stamp) for _, stamp in stamps)
        ]
        for index in matching_rows:
            matched[index].add(entry_number)
        # Extra PB sources in the same archive are kept, but never outside the
        # indexed day/window. They do not establish coverage for another device.
        relevant_windows = [window for window in windows if window.day == entry.day]
        stamps_by_entry.append(
            [
                (minute, stamp)
                for minute, stamp in stamps
                if stamp.astimezone(zone).date() == entry.day
                and any(window.contains(stamp) for window in relevant_windows)
            ]
        )
    if any(not matches for matches in matched):
        raise ActivityArchiveDecodeError(
            "activity payload did not match every indexed source/window"
        )

    samples: list[MetricSample] = []
    counts = [0] * len(windows)
    for entry_number, (entry, stamps) in enumerate(
        zip(entries, stamps_by_entry, strict=True)
    ):
        record_id = _record_identity(indexed_files[0].file_id, entry)
        scope = "device" if entry.serial else "unknown"
        for minute, stamp in stamps:
            block = entry.content[minute * SLOT_BYTES : (minute + 1) * SLOT_BYTES]
            metadata = {
                "timestamp": stamp,
                "source_scope": scope,
                "device_id": entry.serial or None,
                "source_record_id": record_id,
                "sample_ordinal": minute,
            }
            if 20 <= block[3] <= 240:
                samples.append(
                    MetricSample(
                        metric="heart_rate",
                        value=block[3],
                        unit="bpm",
                        source_field="ACTIVE_ORIGIN_DATA.content[3]",
                        qualification="device_observed"
                        if entry.serial
                        else "unattributed_raw",
                        **metadata,
                    )
                )
                for index, window in enumerate(windows):
                    if entry_number in matched[index] and window.contains(stamp):
                        counts[index] += 1
            # Includes retain and every missing/unknown mode code (126/255/etc.).
            for metric, offset in RAW_FIELDS:
                samples.append(
                    MetricSample(
                        metric=metric,
                        value=block[offset],
                        unit="vendor_code",
                        source_field=f"ACTIVE_ORIGIN_DATA.content[{offset}]",
                        qualification="raw_not_sleep_stage",
                        **metadata,
                    )
                )
    return DecodedDenseHeartRate(
        samples=samples,
        files=[
            item.model_copy(update={"parse_status": "decoded", "sample_count": count})
            for item, count in zip(indexed_files, counts, strict=True)
        ],
        entry_count=len(entries),
    )
