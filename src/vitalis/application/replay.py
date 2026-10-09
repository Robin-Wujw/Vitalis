"""Pure application contract for explicitly requested offline source replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
from typing import Any, Mapping, Protocol


class ReplayError(ValueError):
    """A replay request cannot be completed within its isolation contract."""


@dataclass(frozen=True)
class ReplayCommand:
    source_user_id: str
    target_user_id: str
    start_date: date
    end_date: date
    as_of: datetime
    parser_version: str
    journal_ids: tuple[str, ...] | None = None
    timezone_name: str = "UTC"


@dataclass(frozen=True)
class ReplayResult:
    run_id: str
    user_id: str
    source_mode: str
    parser_version: str
    manifest_hash: str
    records_replayed: int
    records_written: int
    partial_records: int
    unrecognized_records: int
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return execution metadata; source payloads never enter this result."""
        return asdict(self)


class SourceReplayPort(Protocol):
    def replay(self, command: ReplayCommand) -> ReplayResult: ...


class ReplayService:
    """Validate the use case and delegate its transaction to an offline port."""

    def __init__(self, port: SourceReplayPort):
        self._port = port

    def replay(self, command: ReplayCommand) -> ReplayResult:
        if not command.source_user_id or not command.target_user_id:
            raise ReplayError("replay requires explicit source and target users")
        if command.source_user_id == command.target_user_id:
            raise ReplayError("replay requires an independent target user")
        if command.start_date > command.end_date:
            raise ReplayError("replay start_date follows end_date")
        if command.as_of.tzinfo is None or command.as_of.utcoffset() is None:
            raise ReplayError("replay as_of requires an explicit timezone")
        if not command.parser_version or len(command.parser_version) > 96:
            raise ReplayError("replay parser version is invalid")
        return self._port.replay(command)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """Hash the complete metadata manifest, excluding its self-referential hash.

    This function has no infrastructure dependency and can be used when an
    analysis run saves or checks its exact input manifest. Object key ordering
    never affects the digest; list ordering remains part of the contract.
    """
    material = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ReplayCommand", "ReplayError", "ReplayResult", "ReplayService",
    "SourceReplayPort", "manifest_digest",
]
