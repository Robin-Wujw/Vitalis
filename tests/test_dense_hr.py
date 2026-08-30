from datetime import datetime, timedelta, timezone
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from vitalis.connectors.zepp.client import ZeppAPIClient, ZeppAuthError
from vitalis.connectors.zepp.dense_hr import (
    DenseHeartRateDecodeError,
    decode_sec_hr_archive,
)
from vitalis.models import DenseDataFile


def _varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _heartbeat(timestamp: int, values: list[int], *, packed: bool = False) -> bytes:
    inner = b"\x08" + _varint(timestamp)
    if packed:
        encoded = b"".join(_varint(value) for value in values)
        inner += b"\x12" + _varint(len(encoded)) + encoded
    else:
        inner += b"".join(b"\x10" + _varint(value) for value in values)
    return b"\x0a" + _varint(len(inner)) + inner


def make_archive(entries: dict[str, list[tuple[int, list[int], bool]]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as zipped:
        for name, blocks in entries.items():
            zipped.writestr(
                name,
                b"".join(
                    _heartbeat(timestamp, values, packed=packed)
                    for timestamp, values, packed in blocks
                ),
            )
    return output.getvalue()


def _indexed(device_id: str, start: int, seconds: int) -> DenseDataFile:
    return DenseDataFile(
        user_id="user-1",
        stream="second_heart_rate",
        file_id="file-1",
        file_type="SEC_HR",
        start_utc=datetime.fromtimestamp(start, timezone.utc),
        end_utc=datetime.fromtimestamp(start + seconds - 1, timezone.utc),
        source_scope="device",
        device_id=device_id,
    )


def test_decode_sec_hr_archive_maps_devices_and_ignores_missing_values():
    first = 1_777_334_400
    second = first + 100
    archive = make_archive({
        "first.pb": [(first, [50, 51, 255, 52], False)],
        "second.pb": [(second, [70, 71, 72], True)],
    })

    decoded = decode_sec_hr_archive(
        archive,
        [_indexed("DEVICE-A", first, 4), _indexed("DEVICE-B", second, 3)],
    )

    assert decoded.entry_count == 2
    assert [(sample.device_id, sample.value) for sample in decoded.samples] == [
        ("DEVICE-A", 50),
        ("DEVICE-A", 51),
        ("DEVICE-A", 52),
        ("DEVICE-B", 70),
        ("DEVICE-B", 71),
        ("DEVICE-B", 72),
    ]
    assert [(item.parse_status, item.sample_count) for item in decoded.files] == [
        ("decoded", 3),
        ("decoded", 3),
    ]


def test_decode_sec_hr_archive_rejects_ambiguous_device_mapping():
    start = 1_777_334_400
    archive = make_archive({"heart.pb": [(start, [50, 51], False)]})
    with pytest.raises(DenseHeartRateDecodeError, match="ambiguous"):
        decode_sec_hr_archive(
            archive,
            [_indexed("DEVICE-A", start, 2), _indexed("DEVICE-B", start, 2)],
        )


def test_decode_sec_hr_archive_rejects_non_zip_payload():
    with pytest.raises(DenseHeartRateDecodeError, match="not a ZIP"):
        decode_sec_hr_archive(b"not-a-zip", [_indexed("DEVICE-A", 1_777_334_400, 2)])


def test_client_resolves_and_downloads_dense_file_without_forwarding_token():
    archive = make_archive({"heart.pb": [(1_777_334_400, [50], False)]})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api-mifitcn.zepp.com":
            assert request.url.path == "/files/SEC_HR/users/vendor-user/queryDownUrlList"
            assert request.url.params["fileIds"] == "file-1"
            assert request.headers["apptoken"] == "private-token"
            return httpx.Response(
                200,
                json={"file-1": "https://download.example/heart.zip?signature=private"},
            )
        assert request.url.host == "download.example"
        assert "apptoken" not in request.headers
        return httpx.Response(200, content=archive, headers={"content-type": "application/zip"})

    client = ZeppAPIClient("private-token", "vendor-user", "api-mifitcn.zepp.com")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    assert client.download_dense_file("SEC_HR", "file-1") == archive
    assert len(requests) == 2


def test_client_rejects_non_https_dense_file_url():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"file-1": "http://download.example/heart.zip"})

    client = ZeppAPIClient("private-token", "vendor-user", "api-mifitcn.zepp.com")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)

    with pytest.raises(ZeppAuthError, match="不安全"):
        client.download_dense_file("SEC_HR", "file-1")
