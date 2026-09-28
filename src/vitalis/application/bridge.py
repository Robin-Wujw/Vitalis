"""Balance 2 bridge ingestion and device-link capability use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import secrets

from vitalis.domain import MetricSample

from .ports import BridgeDeviceCapability, BridgeUnitOfWork


_DEVICE_SAMPLE_ID = re.compile(
    r"^z2:(\d{13}):(\d{1,3}):([a-z0-9]{4,32}):(\d+)$"
)


class BridgeAuthenticationError(RuntimeError):
    """The device capability is missing, revoked, or no longer owned."""


class BridgeProtocolError(ValueError):
    """The bridge request does not match the supported protocol contract."""


@dataclass(frozen=True)
class BridgeDeviceLinkResult:
    """Safe response projection for one newly issued device capability."""

    token: str

    def as_dict(self) -> dict:
        return {
            "status": "created",
            "device": "Balance 2",
            "upload_path": "/api/bridge/batches",
            "device_link_token": self.token,
            "message": "上传令牌只显示一次，请配置到 Zepp App 中的 Vitalis Bridge 设置",
        }


@dataclass(frozen=True)
class _AcceptedSample:
    sample_id: str
    timestamp_ms: int
    sample_ordinal: int
    heart_rate: int


@dataclass(frozen=True)
class BridgeBatchResult:
    """Stable settlement response for one bridge batch."""

    received_count: int
    acknowledged: tuple[dict, ...]
    rejected: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "protocol_version": 2,
            "status": "processed",
            "received_count": self.received_count,
            "acknowledged_count": len(self.acknowledged),
            "rejected_count": len(self.rejected),
            "acknowledged": [dict(item) for item in self.acknowledged],
            "rejected": [dict(item) for item in self.rejected],
        }


class BridgeBatchIngest:
    """Validate and atomically settle one Balance 2 upload batch.

    The application owns protocol validation and normalized ``MetricSample``
    construction.  The persistence port owns capability fencing and writes,
    keeping authentication and sample persistence in one transaction.
    """

    def __init__(
        self,
        unit_of_work_factory: Callable[[], BridgeUnitOfWork],
        *,
        now_factory: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def issue_device_link(self, user_id: str) -> BridgeDeviceLinkResult:
        """Issue a one-time-displayed capability through the application boundary."""
        token = self._token_factory()
        digest = _token_digest(token)
        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.repository.create_device_link(digest, user_id)
            unit_of_work.commit()
        return BridgeDeviceLinkResult(token=token)

    def ingest(
        self,
        token_digest: str,
        protocol_version: int,
        samples: list[object],
    ) -> BridgeBatchResult:
        """Validate, fence, persist, and settle one device upload batch."""
        if protocol_version != 2:
            raise BridgeProtocolError("不支持的设备上传协议")

        now = self._utc_now()
        now_ms = int(now.timestamp() * 1000)
        minimum_ms = now_ms - 31 * 24 * 60 * 60 * 1000
        maximum_ms = now_ms + 5 * 60 * 1000
        sample_id_counts: dict[str, int] = {}
        for raw in samples:
            if not isinstance(raw, dict):
                continue
            sample_id = raw.get("sample_id")
            if (
                isinstance(sample_id, str)
                and len(sample_id) <= 128
                and _DEVICE_SAMPLE_ID.fullmatch(sample_id)
            ):
                sample_id_counts[sample_id] = sample_id_counts.get(sample_id, 0) + 1

        rejected: list[dict] = []
        accepted_entries: list[_AcceptedSample] = []
        reported_duplicate_ids: set[str] = set()
        for index, raw in enumerate(samples):
            if not isinstance(raw, dict):
                rejected.append(_device_sample_rejection(index, None, "invalid_sample"))
                continue
            raw_sample_id = raw.get("sample_id")
            sample_id = (
                raw_sample_id
                if (
                    isinstance(raw_sample_id, str)
                    and len(raw_sample_id) <= 128
                    and _DEVICE_SAMPLE_ID.fullmatch(raw_sample_id)
                )
                else None
            )
            if sample_id is None:
                rejected.append(_device_sample_rejection(index, None, "invalid_sample_id"))
                continue
            if sample_id_counts[sample_id] > 1:
                if sample_id not in reported_duplicate_ids:
                    rejected.append(
                        _device_sample_rejection(index, sample_id, "duplicate_sample_id")
                    )
                    reported_duplicate_ids.add(sample_id)
                continue

            timestamp = raw.get("timestamp")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                rejected.append(_device_sample_rejection(index, sample_id, "invalid_timestamp"))
                continue
            sample_ordinal = raw.get("sample_ordinal")
            if (
                not isinstance(sample_ordinal, int)
                or isinstance(sample_ordinal, bool)
                or not 0 <= sample_ordinal < 1000
            ):
                rejected.append(
                    _device_sample_rejection(index, sample_id, "invalid_sample_ordinal")
                )
                continue
            identity = _DEVICE_SAMPLE_ID.fullmatch(sample_id)
            if (
                identity is None
                or int(identity.group(1)) != timestamp
                or int(identity.group(2)) != sample_ordinal
            ):
                rejected.append(
                    _device_sample_rejection(index, sample_id, "sample_identity_mismatch")
                )
                continue
            heart_rate = raw.get("heart_rate")
            if not isinstance(heart_rate, int) or isinstance(heart_rate, bool):
                rejected.append(_device_sample_rejection(index, sample_id, "invalid_heart_rate"))
                continue
            if timestamp < minimum_ms:
                rejected.append(_device_sample_rejection(index, sample_id, "timestamp_too_old"))
                continue
            if timestamp > maximum_ms:
                rejected.append(_device_sample_rejection(index, sample_id, "timestamp_too_future"))
                continue
            if not 20 <= heart_rate <= 240:
                rejected.append(
                    _device_sample_rejection(index, sample_id, "heart_rate_out_of_range")
                )
                continue
            accepted_entries.append(
                _AcceptedSample(sample_id, timestamp, sample_ordinal, heart_rate)
            )

        def build_samples(capability: BridgeDeviceCapability) -> list[MetricSample]:
            return [
                MetricSample(
                    user_id=capability.user_id,
                    source="zepp_os",
                    metric="heart_rate",
                    timestamp=datetime.fromtimestamp(
                        entry.timestamp_ms / 1000, tz=timezone.utc
                    ),
                    value=entry.heart_rate,
                    unit="bpm",
                    source_scope="device_callback",
                    device_id=capability.device_label,
                    source_record_id=entry.sample_id,
                    sample_ordinal=entry.sample_ordinal,
                )
                for entry in accepted_entries
            ]

        with self._unit_of_work_factory() as unit_of_work:
            capability = unit_of_work.repository.write_bridge_batch(
                token_digest,
                now,
                build_samples,
            )
            if capability is None:
                raise BridgeAuthenticationError("设备上传令牌无效或已撤销")
            unit_of_work.commit()

        acknowledged = tuple(
            {
                "sample_id": entry.sample_id,
                "timestamp": entry.timestamp_ms,
                "sample_ordinal": entry.sample_ordinal,
            }
            for entry in accepted_entries
        )
        return BridgeBatchResult(
            received_count=len(samples),
            acknowledged=acknowledged,
            rejected=tuple(rejected),
        )

    def _utc_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)


def _device_sample_rejection(index: int, sample_id: str | None, code: str) -> dict:
    return {
        "index": index,
        "sample_id": sample_id,
        "code": code,
        "retryable": False,
    }


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
