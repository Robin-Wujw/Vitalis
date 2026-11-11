"""Application use case for durable, user-scoped synchronization jobs."""

from __future__ import annotations

from datetime import date

from vitalis.application.ports import SyncJobCommand, SyncJobCreated, SyncJobPort


class SyncJobConflict(ValueError):
    """The request conflicts with an existing durable sync state."""


class SyncJobAuthRequired(ValueError):
    """The requested source has no usable credentials."""


class SyncJobInvalid(ValueError):
    """The sync request is outside the current application contract."""


class SyncJobService:
    """Coordinate sync creation, reads, and cancellation through one port."""

    def __init__(self, port: SyncJobPort) -> None:
        self._port = port

    def create(self, command: SyncJobCommand) -> SyncJobCreated:
        self._validate(command)
        try:
            return self._port.create(command)
        except (SyncJobConflict, SyncJobAuthRequired, SyncJobInvalid):
            raise
        except ValueError as exc:
            raise SyncJobInvalid(str(exc)) from exc

    def status(self, user_id: str, job_id: str) -> dict | None:
        if not user_id or not isinstance(user_id, str):
            raise SyncJobInvalid("invalid user id")
        if not job_id or not isinstance(job_id, str):
            raise SyncJobInvalid("invalid job id")
        return self._port.status(user_id, job_id)

    def cancel(self, user_id: str, job_id: str) -> bool | None:
        if not user_id or not isinstance(user_id, str):
            raise SyncJobInvalid("invalid user id")
        if not job_id or not isinstance(job_id, str):
            raise SyncJobInvalid("invalid job id")
        return self._port.cancel(user_id, job_id)

    @staticmethod
    def _validate(command: SyncJobCommand) -> None:
        if command.raw_request is not None:
            from .vendor_request import validate_raw_request
            validate_raw_request(command.raw_request)
            if command.streams is not None or command.detail_only or command.workout_only or command.detail_backfill:
                raise SyncJobInvalid("raw GET jobs cannot be combined with synchronization modes")
        if command.streams is not None and (
            not isinstance(command.streams, tuple) or not 1 <= len(command.streams) <= 32
            or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in command.streams)
        ):
            raise SyncJobInvalid("streams must contain 1..32 source stream names")
        if not isinstance(command.user_id, str) or not command.user_id or len(command.user_id) > 64:
            raise SyncJobInvalid("invalid user id")
        if not isinstance(command.source, str) or not command.source:
            raise SyncJobInvalid("invalid source")
        if (
            not isinstance(command.idempotency_key, str)
            or not command.idempotency_key.strip()
            or len(command.idempotency_key) > 128
        ):
            raise SyncJobInvalid("invalid idempotency key")
        if type(command.days) is not int or not 1 <= command.days <= 730:
            raise SyncJobInvalid("sync days must be between 1 and 730")
        if (command.from_date is None) != (command.to_date is None):
            raise SyncJobInvalid("from and to must be provided together")
        if command.from_date is not None and (
            not isinstance(command.from_date, date)
            or not isinstance(command.to_date, date)
        ):
            raise SyncJobInvalid("invalid sync window")
        if command.from_date is not None and command.to_date is not None:
            span = abs((command.to_date - command.from_date).days) + 1
            if span > 730:
                raise SyncJobInvalid("sync window must be between 1 and 730 days")
        if command.decode_dense_files is not None and type(command.decode_dense_files) is not bool:
            raise SyncJobInvalid("decode_dense_files must be boolean or null for automatic selection")
        for name in (
            "detail_backfill",
            "workout_only",
            "detail_only",
        ):
            if type(getattr(command, name)) is not bool:
                raise SyncJobInvalid(f"{name} must be boolean")
        if command.detail_limit is not None and type(command.detail_limit) is not int:
            raise SyncJobInvalid("detail_limit must be an integer")
        if command.detail_refresh_before is not None and not isinstance(
            command.detail_refresh_before, str
        ):
            raise SyncJobInvalid("detail_refresh_before must be a UTC timestamp")


__all__ = [
    "SyncJobAuthRequired",
    "SyncJobConflict",
    "SyncJobInvalid",
    "SyncJobService",
]
