"""Boundary contracts for bounded JSON string and delimited-field scanning."""

import json

import pytest

from vitalis.adapters.zepp import workout_streaming as streaming


def _spool(raw, monkeypatch, chunk_size):
    monkeypatch.setattr(streaming, "STREAM_CHUNK_BYTES", chunk_size)
    payload = streaming.spool_bytes(raw, max_bytes=len(raw))
    iter_bytes = payload.iter_bytes

    def small_chunks(**kwargs):
        kwargs["chunk_size"] = chunk_size
        return iter_bytes(**kwargs)

    monkeypatch.setattr(payload, "iter_bytes", small_chunks)
    return payload


def _fields(payload, **limits):
    return streaming.scan_workout_payload(
        payload, max_objects=limits.get("objects", 10),
        max_separators=limits.get("separators", 10),
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64])
@pytest.mark.parametrize("padding", range(8))
def test_strings_and_parts_match_json_at_byte_and_escape_boundaries(
    monkeypatch, chunk_size, padding
):
    # Both literal UTF-8 and JSON escapes cross physical read boundaries.
    encoded = (
        '"' + "x" * padding + r';\";\\;\/;\b;\f;\n;\r;\t;\u003b;\u4e2d;'
        + '中😀;;"'
    )
    raw = ('{"lap":' + encoded + ',"next":42}').encode()
    expected = json.loads(encoded)
    with _spool(raw, monkeypatch, chunk_size) as payload:
        fields = _fields(payload)
        text = fields["lap"].as_value("lap")
        chunks = list(text.iter_text_chunks(chunk_size=5))
        assert all(0 < len(chunk) <= 5 for chunk in chunks)
        assert "".join(chunks) == expected
        assert list(text.iter_parts()) == expected.split(";")
        assert text.count_parts() == len(expected.split(";"))
        assert text.text(max_bytes=len(expected.encode())) == expected
        assert fields["next"].as_value("next") == 42


@pytest.mark.parametrize("chunk_size", [1, 2, 7])
@pytest.mark.parametrize("content", [b'abc\\', b'abc\\u123', b'abc\\u12xz"',
                                      b'abc\\q"', b'abc\x00"', b'abc\n"'])
def test_malformed_string_is_rejected_across_chunks(monkeypatch, chunk_size, content):
    raw = b'{"lap":"' + content + b'}'
    with _spool(raw, monkeypatch, chunk_size) as payload:
        with pytest.raises(streaming.WorkoutStreamJSONError):
            _fields(payload)


@pytest.mark.parametrize("content", [b"\xff", b"\xe4\xb8", b"\xf0\x9f\x98"])
def test_invalid_utf8_is_rejected_when_decoding_field(monkeypatch, content):
    with _spool(b'{"lap":"' + content + b'"}', monkeypatch, 1) as payload:
        text = _fields(payload)["lap"].as_value("lap")
        with pytest.raises(streaming.WorkoutStreamJSONError, match="UTF-8"):
            list(text.iter_parts())


@pytest.mark.parametrize("chunk_size", [1, 3, 64])
def test_part_and_text_limits_count_decoded_utf8_bytes(monkeypatch, chunk_size):
    monkeypatch.setattr(streaming, "MAX_DELIMITED_PART_BYTES", 8)
    raw = b'{"lap":"12345678;\\u4e2d12345;"}'
    with _spool(raw, monkeypatch, chunk_size) as payload:
        text = _fields(payload)["lap"].as_value("lap")
        assert list(text.iter_parts()) == ["12345678", "中12345", ""]
        assert text.text(max_bytes=18) == "12345678;中12345;"
        with pytest.raises(streaming.WorkoutStreamLimitError):
            text.text(max_bytes=17)
    with _spool(b'{"lap":"123456789"}', monkeypatch, chunk_size) as payload:
        with pytest.raises(streaming.WorkoutStreamLimitError):
            list(_fields(payload)["lap"].as_value("lap").iter_parts())


@pytest.mark.parametrize("raw,limits", [
    (b'{"a":{"b":1}}', {"objects": 1}),
    (b'{"a":"commas,,,are text","b":1}', {"separators": 0}),
    (b'{"' + b"k" * 513 + b'":1}', {}),
    (b'{"a":' + b"1" * 513 + b'}', {}),
])
def test_scanner_safety_limits_are_preserved(monkeypatch, raw, limits):
    with _spool(raw, monkeypatch, 7) as payload:
        with pytest.raises(streaming.WorkoutStreamLimitError):
            _fields(payload, **limits)


def test_large_plain_string_is_scanned_by_spans_not_cursor_calls(monkeypatch):
    raw = b'{"lap":"' + b"x" * (2 * 64 * 1024) + b'","next":42}'
    reads = 0
    read = streaming._ByteCursor.read

    def counted_read(cursor):
        nonlocal reads
        reads += 1
        return read(cursor)

    monkeypatch.setattr(streaming._ByteCursor, "read", counted_read)
    with streaming.spool_bytes(raw, max_bytes=len(raw)) as payload:
        fields = _fields(payload)
        assert fields["lap"].content_end - fields["lap"].content_start == 2 * 64 * 1024
        assert fields["next"].as_value("next") == 42
    assert reads < 100, "ordinary string spans must not allocate a Python cursor step per byte"
