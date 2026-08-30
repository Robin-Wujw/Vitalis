"""Decode Zepp SEC_HR archives into device-scoped second-level heart rate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from vitalis.models import DenseDataFile, MetricSample


MAX_ARCHIVE_ENTRIES = 32
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_HEART_VALUES_PER_BLOCK = 172_800
MISSING_HEART_RATE = 255


class DenseHeartRateDecodeError(ValueError):
    """The vendor archive does not match the verified SEC_HR contract."""


@dataclass(frozen=True)
class _HeartRateBlock:
    start_second: int
    values: tuple[int, ...]

    @property
    def end_second(self) -> int:
        return self.start_second + len(self.values) - 1


@dataclass(frozen=True)
class DecodedDenseHeartRate:
    samples: list[MetricSample]
    files: list[DenseDataFile]
    entry_count: int


def _read_varint(data: bytes, position: int, limit: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < limit and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, position
        shift += 7
    raise DenseHeartRateDecodeError("SEC_HR protobuf contains a truncated varint")


def _protobuf_fields(data: bytes):
    position = 0
    limit = len(data)
    while position < limit:
        tag, position = _read_varint(data, position, limit)
        field_number = tag >> 3
        wire_type = tag & 7
        if field_number == 0:
            raise DenseHeartRateDecodeError("SEC_HR protobuf contains an invalid field")
        if wire_type == 0:
            value, position = _read_varint(data, position, limit)
        elif wire_type == 1:
            end = position + 8
            if end > limit:
                raise DenseHeartRateDecodeError("SEC_HR protobuf fixed64 field is truncated")
            value = data[position:end]
            position = end
        elif wire_type == 2:
            size, position = _read_varint(data, position, limit)
            end = position + size
            if end > limit:
                raise DenseHeartRateDecodeError("SEC_HR protobuf field is truncated")
            value = data[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            if end > limit:
                raise DenseHeartRateDecodeError("SEC_HR protobuf fixed32 field is truncated")
            value = data[position:end]
            position = end
        else:
            raise DenseHeartRateDecodeError(
                f"SEC_HR protobuf uses unsupported wire type {wire_type}"
            )
        yield field_number, wire_type, value


def _packed_varints(data: bytes) -> list[int]:
    values: list[int] = []
    position = 0
    while position < len(data):
        value, position = _read_varint(data, position, len(data))
        values.append(value)
    return values


def _decode_daily_heart_rate(data: bytes) -> list[_HeartRateBlock]:
    blocks: list[_HeartRateBlock] = []
    for field_number, wire_type, value in _protobuf_fields(data):
        if field_number != 1 or wire_type != 2:
            continue
        timestamp: int | None = None
        heart_values: list[int] = []
        for inner_field, inner_wire, inner_value in _protobuf_fields(value):
            if inner_field == 1 and inner_wire == 0:
                timestamp = int(inner_value)
            elif inner_field == 2 and inner_wire == 0:
                heart_values.append(int(inner_value))
            elif inner_field == 2 and inner_wire == 2:
                heart_values.extend(_packed_varints(inner_value))
        if timestamp is None or timestamp <= 0:
            raise DenseHeartRateDecodeError("SEC_HR heartbeat block has no timestamp")
        if not heart_values:
            raise DenseHeartRateDecodeError("SEC_HR heartbeat block has no samples")
        if len(heart_values) > MAX_HEART_VALUES_PER_BLOCK:
            raise DenseHeartRateDecodeError("SEC_HR heartbeat block is too large")
        if any(value < 0 or value > MISSING_HEART_RATE for value in heart_values):
            raise DenseHeartRateDecodeError("SEC_HR heartbeat value is outside uint8 range")
        blocks.append(_HeartRateBlock(timestamp, tuple(heart_values)))
    if not blocks:
        raise DenseHeartRateDecodeError("SEC_HR protobuf contains no heartbeat blocks")
    return blocks


def _utc_second(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _overlap_seconds(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b) + 1)


def _device_intervals(
    indexed_files: list[DenseDataFile],
) -> dict[str, list[tuple[int, int]]]:
    intervals: dict[str, list[tuple[int, int]]] = {}
    for item in indexed_files:
        start = _utc_second(item.start_utc)
        end = _utc_second(item.end_utc)
        if item.device_id and start is not None and end is not None and end >= start:
            intervals.setdefault(item.device_id, []).append((start, end))
    if not intervals:
        raise DenseHeartRateDecodeError("SEC_HR index has no device coverage")
    return intervals


def _assign_devices(
    entries: list[list[_HeartRateBlock]], indexed_files: list[DenseDataFile]
) -> list[str]:
    """Globally match one protobuf entry to one device by indexed overlap."""
    intervals = _device_intervals(indexed_files)
    devices = sorted(intervals)
    if len(entries) > len(devices) or len(devices) > 12:
        raise DenseHeartRateDecodeError("SEC_HR archive and device counts do not match")
    scores = [
        [
            sum(
                _overlap_seconds(block.start_second, block.end_second, start, end)
                for block in blocks
                for start, end in intervals[device_id]
            )
            for device_id in devices
        ]
        for blocks in entries
    ]

    # mask -> (score, assignments, number of equally scoring assignments)
    states: dict[int, tuple[int, list[int], int]] = {0: (0, [], 1)}
    for entry_scores in scores:
        next_states: dict[int, tuple[int, list[int], int]] = {}
        for mask, (total, assignments, ways) in states.items():
            for device_index, score in enumerate(entry_scores):
                bit = 1 << device_index
                if mask & bit or score <= 0:
                    continue
                next_mask = mask | bit
                candidate = (total + score, assignments + [device_index], ways)
                current = next_states.get(next_mask)
                if current is None or candidate[0] > current[0]:
                    next_states[next_mask] = candidate
                elif candidate[0] == current[0]:
                    next_states[next_mask] = (
                        current[0], current[1], current[2] + candidate[2]
                    )
        states = next_states
    if not states:
        raise DenseHeartRateDecodeError("SEC_HR payload does not overlap its file index")
    best_score = max(state[0] for state in states.values())
    best = [state for state in states.values() if state[0] == best_score]
    if len(best) != 1 or best[0][2] != 1:
        raise DenseHeartRateDecodeError("SEC_HR device mapping is ambiguous")
    return [devices[index] for index in best[0][1]]


def decode_sec_hr_archive(
    archive: bytes, indexed_files: list[DenseDataFile]
) -> DecodedDenseHeartRate:
    """Decode the verified DailySecondHeartBeat protobuf archive."""
    if not archive or not indexed_files:
        raise DenseHeartRateDecodeError("SEC_HR archive or index is empty")
    try:
        zipped = ZipFile(BytesIO(archive))
    except BadZipFile as exc:
        raise DenseHeartRateDecodeError("SEC_HR payload is not a ZIP archive") from exc

    with zipped:
        entries = zipped.infolist()
        if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
            raise DenseHeartRateDecodeError("SEC_HR archive entry count is invalid")
        total_size = sum(entry.file_size for entry in entries)
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise DenseHeartRateDecodeError("SEC_HR archive is too large")

        entry_blocks: list[list[_HeartRateBlock]] = []
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                entry.is_dir()
                or path.name != entry.filename
                or path.suffix.lower() != ".pb"
                or entry.file_size <= 0
                or entry.file_size > MAX_ENTRY_BYTES
            ):
                raise DenseHeartRateDecodeError("SEC_HR archive contains an invalid entry")
            entry_blocks.append(_decode_daily_heart_rate(zipped.read(entry)))

    assigned_devices = _assign_devices(entry_blocks, indexed_files)
    decoded_entries = list(zip(assigned_devices, entry_blocks, strict=True))

    samples_by_key: dict[tuple[str, int], MetricSample] = {}
    blocks_by_device: dict[str, list[_HeartRateBlock]] = {}
    for device_id, blocks in decoded_entries:
        blocks_by_device.setdefault(device_id, []).extend(blocks)
        for block in blocks:
            for offset, value in enumerate(block.values):
                if value == MISSING_HEART_RATE or value <= 0:
                    continue
                second = block.start_second + offset
                samples_by_key[(device_id, second)] = MetricSample(
                    metric="heart_rate",
                    timestamp=datetime.fromtimestamp(second, timezone.utc),
                    value=float(value),
                    unit="bpm",
                    source_scope="device",
                    device_id=device_id,
                )

    decoded_files: list[DenseDataFile] = []
    matched_rows = 0
    for item in indexed_files:
        start = _utc_second(item.start_utc)
        end = _utc_second(item.end_utc)
        blocks = blocks_by_device.get(item.device_id or "", [])
        if start is None or end is None or not any(
            _overlap_seconds(block.start_second, block.end_second, start, end) > 0
            for block in blocks
        ):
            decoded_files.append(item.model_copy(update={
                "parse_status": "no_data",
                "sample_count": 0,
            }))
            continue
        count = sum(
            1
            for device_id, second in samples_by_key
            if device_id == item.device_id and start <= second <= end
        )
        decoded_files.append(item.model_copy(update={
            "parse_status": "decoded",
            "sample_count": count,
        }))
        matched_rows += 1
    if matched_rows == 0:
        raise DenseHeartRateDecodeError("SEC_HR payload did not match any indexed interval")

    return DecodedDenseHeartRate(
        samples=list(samples_by_key.values()),
        files=decoded_files,
        entry_count=len(decoded_entries),
    )
