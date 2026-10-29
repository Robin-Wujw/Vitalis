"""Bounded streaming support for Zepp workout-detail responses."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

STREAM_CHUNK_BYTES = 64 * 1024
MAX_STRING_KEY_BYTES = 512
MAX_STRUCTURED_VALUE_BYTES = 8 * 1024 * 1024
MAX_DELIMITED_PART_BYTES = 1 * 1024 * 1024

# Search ordinary string spans in C; Python only handles escapes and boundaries.
_JSON_STRING_SPECIAL_BYTES = re.compile(rb'["\\\x00-\x1f]')
_JSON_STRING_SPECIAL_TEXT = re.compile(r'[\\\x00-\x1f]')


class WorkoutStreamError(ValueError):
    """A malformed or unsupported streamed workout response."""


class WorkoutStreamLimitError(WorkoutStreamError):
    """The streamed JSON exceeded a bounded safety limit."""


class WorkoutStreamJSONError(WorkoutStreamError):
    """The streamed body is not complete valid JSON."""


@dataclass
class SpoolPayload:
    """An expanded HTTP body stored in a private temporary directory."""

    path: Path
    size: int
    sha256: str
    directory: Path | None = None
    _closed: bool = False

    def iter_bytes(
        self,
        *,
        start: int = 0,
        end: int | None = None,
        chunk_size: int = STREAM_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        if self._closed:
            raise RuntimeError("spooled payload is closed")
        if start < 0 or start > self.size:
            raise ValueError("spool start is outside the payload")
        final = self.size if end is None else min(end, self.size)
        if final < start:
            raise ValueError("spool end precedes its start")
        with self.path.open("rb") as handle:
            handle.seek(start)
            remaining = final - start
            while remaining:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    raise WorkoutStreamError("spooled payload ended unexpectedly")
                remaining -= len(chunk)
                yield chunk

    def open(self):
        if self._closed:
            raise RuntimeError("spooled payload is closed")
        return self.path.open("rb")

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.path.unlink(missing_ok=True)
        finally:
            if self.directory is not None:
                shutil.rmtree(self.directory, ignore_errors=True)

    close = cleanup

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        return f"SpoolPayload(size={self.size}, sha256={self.sha256[:12]}...)"


def spool_bytes(payload: bytes, *, max_bytes: int) -> SpoolPayload:
    """Create a private spool from synthetic/test bytes with a hard cap."""
    if len(payload) > max_bytes:
        raise WorkoutStreamLimitError("运动明细响应超过大小上限")
    directory = Path(tempfile.mkdtemp(prefix="vitalis-workout-"))
    path = directory / "response.json"
    try:
        path.write_bytes(payload)
        return SpoolPayload(
            path=path,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            directory=directory,
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def spool_http_response(
    response: Any,
    *,
    max_bytes: int,
    check: Callable[[], None] | None = None,
) -> SpoolPayload:
    """Consume an httpx streamed response into a bounded private spool."""
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise WorkoutStreamLimitError("运动明细响应超过大小上限")

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in {"", "identity"}:
        decoder = None
    elif encoding == "gzip":
        import zlib

        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        import zlib

        decoder = zlib.decompressobj(zlib.MAX_WBITS)
    else:
        raise WorkoutStreamError("运动明细响应压缩格式不受支持")

    directory = Path(tempfile.mkdtemp(prefix="vitalis-workout-"))
    path = directory / "response.json"
    digest = hashlib.sha256()
    expanded_size = 0
    wire_size = 0

    def write_expanded(handle, expanded: bytes) -> None:
        nonlocal expanded_size
        if not expanded:
            return
        expanded_size += len(expanded)
        if expanded_size > max_bytes:
            raise WorkoutStreamLimitError("运动明细响应超过大小上限")
        handle.write(expanded)
        digest.update(expanded)

    def write_compressed(handle, part: bytes) -> None:
        if decoder is None:
            write_expanded(handle, part)
            return
        pending = part
        while pending:
            try:
                expanded = decoder.decompress(pending, STREAM_CHUNK_BYTES)
            except Exception as exc:
                import zlib

                if isinstance(exc, zlib.error):
                    raise WorkoutStreamError("运动明细响应压缩内容无效") from exc
                raise
            write_expanded(handle, expanded)
            if decoder.unused_data:
                raise WorkoutStreamError("运动明细响应压缩内容无效")
            pending = decoder.unconsumed_tail
            if not pending and not expanded:
                break

    try:
        with path.open("wb") as handle:
            for part in response.iter_raw(chunk_size=STREAM_CHUNK_BYTES):
                if check is not None:
                    check()
                wire_size += len(part)
                if wire_size > max_bytes:
                    raise WorkoutStreamLimitError("运动明细响应超过大小上限")
                write_compressed(handle, part)
            if decoder is not None:
                try:
                    write_expanded(handle, decoder.flush(STREAM_CHUNK_BYTES))
                except Exception as exc:
                    import zlib

                    if isinstance(exc, zlib.error):
                        raise WorkoutStreamError("运动明细响应压缩内容无效") from exc
                    raise
                if not decoder.eof:
                    raise WorkoutStreamError("运动明细响应压缩内容无效")
        return SpoolPayload(
            path=path,
            size=expanded_size,
            sha256=digest.hexdigest(),
            directory=directory,
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


class SampleSpool:
    """On-disk normalized sample staging with deterministic output order."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(tempfile.mkdtemp(prefix="vitalis-samples-"))
        self._owns_directory = directory is None
        self.path = self.directory / "samples.sqlite3"
        self.db = sqlite3.connect(self.path.as_posix())
        self.db.execute(
            "CREATE TABLE samples ("
            "timestamp TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, "
            "unit TEXT NOT NULL, source_scope TEXT NOT NULL, device_id TEXT NOT NULL, "
            "sample_order INTEGER NOT NULL)"
        )
        self.db.commit()
        self.counts: dict[str, int] = {}
        self.total = 0
        self._closed = False

    def add(self, sample: Any) -> None:
        if self._closed:
            raise RuntimeError("sample spool is closed")
        timestamp = sample.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        self.db.execute(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp.astimezone(UTC).isoformat(),
                sample.metric,
                float(sample.value),
                sample.unit,
                sample.source_scope or "workout_detail",
                sample.device_id or "",
                self.total,
            ),
        )
        self.total += 1
        self.counts[sample.metric] = self.counts.get(sample.metric, 0) + 1
        if self.total % 256 == 0:
            self.db.commit()

    def iter_samples(self, workout_id: str) -> Iterator[Any]:
        if self._closed:
            raise RuntimeError("sample spool is closed")
        from vitalis.domain import WorkoutMetricSample

        self.db.commit()
        rows = self.db.execute(
            "SELECT timestamp, metric, value, unit, source_scope, device_id "
            "FROM samples ORDER BY timestamp, metric, source_scope, "
            "device_id, sample_order"
        )
        ordinals: dict[tuple[str, str, str, str], int] = {}
        for timestamp, metric, value, unit, scope, device_id in rows:
            key = (timestamp, metric, scope, device_id)
            ordinal = ordinals.get(key, 0)
            ordinals[key] = ordinal + 1
            yield WorkoutMetricSample(
                workout_id=workout_id,
                timestamp=datetime.fromisoformat(timestamp),
                metric=metric,
                value=float(value),
                unit=unit,
                source_scope=scope,
                device_id=device_id or None,
                sample_ordinal=ordinal,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.db.close()
        finally:
            if self._owns_directory:
                shutil.rmtree(self.directory, ignore_errors=True)
            else:
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class _ByteCursor:
    """Buffered byte cursor with absolute byte offsets."""

    def __init__(self, payload: SpoolPayload):
        self.payload = payload
        self.handle = payload.open()
        self.buffer = b""
        self.index = 0
        self.position = 0
        self.peeked: int | None = None

    def close(self) -> None:
        self.handle.close()

    def _raw(self) -> int | None:
        if self.index >= len(self.buffer):
            self.buffer = self.handle.read(STREAM_CHUNK_BYTES)
            self.index = 0
            if not self.buffer:
                return None
        value = self.buffer[self.index]
        self.index += 1
        return value

    def read(self) -> int | None:
        if self.peeked is not None:
            value = self.peeked
            self.peeked = None
        else:
            value = self._raw()
        if value is not None:
            self.position += 1
        return value

    def peek(self) -> int | None:
        if self.peeked is None:
            self.peeked = self._raw()
        return self.peeked

    def require(self, expected: int) -> None:
        if self.read() != expected:
            raise WorkoutStreamJSONError("运动明细 JSON 结构无效")

    def skip_ws(self) -> None:
        while True:
            value = self.peek()
            if value is None or value not in b" \t\r\n":
                return
            self.read()

    def skip_string_span(self) -> None:
        """Skip buffered bytes up to a quote, escape or control character.

        The opening quote has already consumed any peeked byte. Keep absolute
        offsets in sync without a Python read/allocation for every ASCII byte.
        """
        assert self.peeked is None
        while True:
            if self.index >= len(self.buffer):
                self.buffer = self.handle.read(STREAM_CHUNK_BYTES)
                self.index = 0
                if not self.buffer:
                    return
            match = _JSON_STRING_SPECIAL_BYTES.search(self.buffer, self.index)
            end = match.start() if match is not None else len(self.buffer)
            self.position += end - self.index
            self.index = end
            if match is not None:
                return

    def read_token(self, *, limit: int = MAX_STRING_KEY_BYTES) -> bytes:
        output = bytearray()
        while True:
            value = self.peek()
            if value is None or value in b" \t\r\n,]}:":
                break
            output.append(self.read())  # type: ignore[arg-type]
            if len(output) > limit:
                raise WorkoutStreamLimitError("运动明细 JSON 标量超过安全上限")
        return bytes(output)


@dataclass(frozen=True)
class JsonValueRef:
    payload: SpoolPayload
    start: int
    end: int
    kind: str
    content_start: int | None = None
    content_end: int | None = None
    fields: dict[str, JsonValueRef] | None = None

    def raw_bytes(self, *, limit: int = MAX_STRUCTURED_VALUE_BYTES) -> bytes:
        if self.end - self.start > limit:
            raise WorkoutStreamLimitError("运动明细结构化字段超过安全上限")
        return b"".join(self.payload.iter_bytes(start=self.start, end=self.end))

    def as_value(self, field_name: str) -> Any:
        if self.kind == "string":
            assert self.content_start is not None and self.content_end is not None
            value = DelimitedText(self.payload, self.content_start, self.content_end)
            if field_name in DELIMITED_FIELDS or field_name == "strengthSets":
                return value
            return value.text(max_bytes=MAX_STRUCTURED_VALUE_BYTES)
        if self.kind == "object" and self.fields is not None:
            return {key: ref.as_value(key) for key, ref in self.fields.items()}
        try:
            return json.loads(self.raw_bytes().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkoutStreamJSONError("运动明细 JSON 值无法解码") from exc


DELIMITED_FIELDS = frozenset(
    {
        "time",
        "heart_rate",
        "speed",
        "equivPace",
        "currentDistance",
        "time_delta_altitude",
        "power_meter",
        "gait",
        "runPosture",
        "lap",
        "pause",
        "memo",
    }
)


class DelimitedText:
    """A JSON string slice decoded into bounded text chunks on demand."""

    def __init__(self, payload: SpoolPayload, start: int, end: int):
        self.payload = payload
        self.start = start
        self.end = end

    def iter_text_chunks(self, chunk_size: int = STREAM_CHUNK_BYTES) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        escaped = False
        unicode_digits: str | None = None
        output: list[str] = []
        output_size = 0

        def emit(value: str) -> Iterator[str]:
            nonlocal output_size
            index = 0
            while index < len(value):
                end = min(len(value), index + max(1, chunk_size) - output_size)
                output.append(value[index:end])
                output_size += end - index
                index = end
                if output_size >= chunk_size:
                    text = "".join(output)
                    output.clear()
                    output_size = 0
                    yield text

        def consume_text(text: str) -> Iterator[str]:
            nonlocal escaped, unicode_digits
            index = 0
            while index < len(text):
                if not escaped and unicode_digits is None:
                    match = _JSON_STRING_SPECIAL_TEXT.search(text, index)
                    end = match.start() if match is not None else len(text)
                    if end > index:
                        yield from emit(text[index:end])
                        index = end
                    if match is None:
                        return
                char = text[index]
                index += 1
                if unicode_digits is not None:
                    if char not in "0123456789abcdefABCDEF":
                        raise WorkoutStreamJSONError("运动明细字符串转义无效")
                    unicode_digits += char
                    if len(unicode_digits) == 4:
                        yield from emit(chr(int(unicode_digits, 16)))
                        unicode_digits = None
                    continue
                if escaped:
                    escaped = False
                    if char == "u":
                        unicode_digits = ""
                    else:
                        mapped = {
                            '"': '"',
                            "\\": "\\",
                            "/": "/",
                            "b": "\b",
                            "f": "\f",
                            "n": "\n",
                            "r": "\r",
                            "t": "\t",
                        }.get(char)
                        if mapped is None:
                            raise WorkoutStreamJSONError("运动明细字符串转义无效")
                        yield from emit(mapped)
                elif char == "\\":
                    escaped = True
                else:
                    if ord(char) < 0x20:
                        raise WorkoutStreamJSONError("运动明细字符串含有控制字符")
                    yield from emit(char)

        try:
            for chunk in self.payload.iter_bytes(start=self.start, end=self.end):
                yield from consume_text(decoder.decode(chunk, final=False))
            yield from consume_text(decoder.decode(b"", final=True))
        except UnicodeDecodeError as exc:
            raise WorkoutStreamJSONError("运动明细字符串不是 UTF-8") from exc
        if escaped or unicode_digits is not None:
            raise WorkoutStreamJSONError("运动明细字符串转义未结束")
        if output:
            yield "".join(output)

    def iter_parts(self) -> Iterator[str]:
        current: list[str] = []
        current_bytes = 0
        for chunk in self.iter_text_chunks():
            start = 0
            while True:
                separator = chunk.find(";", start)
                end = len(chunk) if separator < 0 else separator
                part = chunk[start:end]
                current_bytes += len(part.encode("utf-8"))
                if current_bytes > MAX_DELIMITED_PART_BYTES:
                    raise WorkoutStreamLimitError("运动明细分隔字段超过安全上限")
                if part:
                    current.append(part)
                if separator < 0:
                    break
                yield "".join(current)
                current.clear()
                current_bytes = 0
                start = separator + 1
        yield "".join(current)

    def count_parts(self) -> int:
        return sum(1 for _ in self.iter_parts())

    def text(self, *, max_bytes: int = MAX_STRUCTURED_VALUE_BYTES) -> str:
        pieces: list[str] = []
        total = 0
        for chunk in self.iter_text_chunks():
            total += len(chunk.encode("utf-8"))
            if total > max_bytes:
                raise WorkoutStreamLimitError("运动明细字符串超过安全上限")
            pieces.append(chunk)
        return "".join(pieces)

    def json_value(self, *, max_bytes: int = MAX_STRUCTURED_VALUE_BYTES) -> Any:
        try:
            return json.loads(self.text(max_bytes=max_bytes))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkoutStreamJSONError("运动明细 JSON 编码字段无效") from exc

    def is_empty(self) -> bool:
        return not any(chunk.strip() for chunk in self.iter_text_chunks())


class _JsonScanner:
    def __init__(self, payload: SpoolPayload, *, max_objects: int, max_separators: int):
        self.cursor = _ByteCursor(payload)
        self.max_objects = max_objects
        self.max_separators = max_separators
        self.objects = 0
        self.separators = 0

    def close(self) -> None:
        self.cursor.close()

    def _structural(self, value: int) -> None:
        if value == ord("{"):
            self.objects += 1
            if self.objects > self.max_objects:
                raise WorkoutStreamLimitError("运动明细 JSON 结构超过安全上限")
        elif value == ord(","):
            self.separators += 1
            if self.separators > self.max_separators:
                raise WorkoutStreamLimitError("运动明细 JSON 结构超过安全上限")

    def _read_string(self) -> tuple[int, int]:
        self.cursor.require(ord('"'))
        start = self.cursor.position
        escaped = False
        unicode_digits: str | None = None
        while True:
            if not escaped and unicode_digits is None:
                self.cursor.skip_string_span()
            value = self.cursor.read()
            if value is None:
                raise WorkoutStreamJSONError("运动明细 JSON 字符串未结束")
            char = chr(value)
            if unicode_digits is not None:
                if char not in "0123456789abcdefABCDEF":
                    raise WorkoutStreamJSONError("运动明细 JSON 转义无效")
                unicode_digits += char
                if len(unicode_digits) == 4:
                    unicode_digits = None
                continue
            if escaped:
                escaped = False
                if char == "u":
                    unicode_digits = ""
                elif char not in '"\\/bfnrt':
                    raise WorkoutStreamJSONError("运动明细 JSON 转义无效")
                continue
            if value < 0x20:
                raise WorkoutStreamJSONError("运动明细 JSON 含有控制字符")
            if char == "\\":
                escaped = True
            elif char == '"':
                return start, self.cursor.position - 1

    def _read_key(self) -> str:
        start, end = self._read_string()
        if end - start > MAX_STRING_KEY_BYTES:
            raise WorkoutStreamLimitError("运动明细 JSON 键超过安全上限")
        raw = b"".join(self.cursor.payload.iter_bytes(start=start, end=end))
        try:
            return json.loads('"' + raw.decode("utf-8") + '"')
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkoutStreamJSONError("运动明细 JSON 键无效") from exc

    def _scan_object(self, depth: int, *, capture: bool) -> JsonValueRef:
        if depth > 100:
            raise WorkoutStreamLimitError("运动明细 JSON 嵌套超过安全上限")
        start = self.cursor.position
        if self.cursor.read() != ord("{"):
            raise WorkoutStreamJSONError("运动明细 JSON 对象无效")
        self._structural(ord("{"))
        fields: dict[str, JsonValueRef] | None = {} if capture else None
        self.cursor.skip_ws()
        if self.cursor.peek() == ord("}"):
            self.cursor.read()
            return JsonValueRef(
                self.cursor.payload,
                start,
                self.cursor.position,
                "object",
                fields=fields,
            )
        while True:
            self.cursor.skip_ws()
            key = self._read_key()
            self.cursor.skip_ws()
            self.cursor.require(ord(":"))
            self.cursor.skip_ws()
            ref = self._scan_value(depth + 1, capture=capture and key == "data")
            if fields is not None:
                fields[key] = ref
            self.cursor.skip_ws()
            separator = self.cursor.read()
            if separator == ord("}"):
                break
            if separator != ord(","):
                raise WorkoutStreamJSONError("运动明细 JSON 对象分隔符无效")
            self._structural(separator)
        return JsonValueRef(
            self.cursor.payload, start, self.cursor.position, "object", fields=fields
        )

    def _scan_array(self, depth: int) -> JsonValueRef:
        if depth > 100:
            raise WorkoutStreamLimitError("运动明细 JSON 嵌套超过安全上限")
        start = self.cursor.position
        self.cursor.require(ord("["))
        self.cursor.skip_ws()
        if self.cursor.peek() == ord("]"):
            self.cursor.read()
            return JsonValueRef(
                self.cursor.payload, start, self.cursor.position, "array"
            )
        while True:
            self.cursor.skip_ws()
            self._scan_value(depth + 1, capture=False)
            self.cursor.skip_ws()
            separator = self.cursor.read()
            if separator == ord("]"):
                break
            if separator != ord(","):
                raise WorkoutStreamJSONError("运动明细 JSON 数组分隔符无效")
            self._structural(separator)
        return JsonValueRef(self.cursor.payload, start, self.cursor.position, "array")

    def _scan_value(self, depth: int, *, capture: bool = False) -> JsonValueRef:
        self.cursor.skip_ws()
        start = self.cursor.position
        value = self.cursor.peek()
        if value is None:
            raise WorkoutStreamJSONError("运动明细 JSON 值缺失")
        if value == ord('"'):
            content_start, content_end = self._read_string()
            return JsonValueRef(
                self.cursor.payload,
                start,
                self.cursor.position,
                "string",
                content_start=content_start,
                content_end=content_end,
            )
        if value == ord("{"):
            return self._scan_object(depth, capture=capture)
        if value == ord("["):
            return self._scan_array(depth)
        token = self.cursor.read_token()
        if not token:
            raise WorkoutStreamJSONError("运动明细 JSON 标量无效")
        try:
            parsed = json.loads(token.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkoutStreamJSONError("运动明细 JSON 标量无效") from exc
        if parsed is not None and not isinstance(parsed, (str, int, float, bool)):
            raise WorkoutStreamJSONError("运动明细 JSON 标量无效")
        return JsonValueRef(self.cursor.payload, start, self.cursor.position, "scalar")

    def scan_root(self) -> dict[str, JsonValueRef]:
        self.cursor.skip_ws()
        root = self._scan_object(0, capture=True)
        self.cursor.skip_ws()
        if self.cursor.read() is not None:
            raise WorkoutStreamJSONError("运动明细 JSON 尾部存在多余内容")
        return root.fields or {}


def scan_workout_payload(
    payload: SpoolPayload, *, max_objects: int, max_separators: int
) -> dict[str, JsonValueRef]:
    scanner = _JsonScanner(
        payload, max_objects=max_objects, max_separators=max_separators
    )
    try:
        return scanner.scan_root()
    finally:
        scanner.close()


def validate_json_limits(
    payload: SpoolPayload,
    *,
    max_objects: int,
    max_separators: int,
    require_complete: bool = True,
) -> None:
    try:
        scan_workout_payload(
            payload, max_objects=max_objects, max_separators=max_separators
        )
    except WorkoutStreamJSONError:
        if require_complete:
            raise


def mapping_from_fields(fields: dict[str, JsonValueRef]) -> dict[str, Any]:
    return {key: ref.as_value(key) for key, ref in fields.items()}


__all__ = [
    "STREAM_CHUNK_BYTES",
    "DelimitedText",
    "JsonValueRef",
    "SampleSpool",
    "SpoolPayload",
    "WorkoutStreamError",
    "WorkoutStreamJSONError",
    "WorkoutStreamLimitError",
    "mapping_from_fields",
    "scan_workout_payload",
    "spool_bytes",
    "spool_http_response",
    "validate_json_limits",
]
