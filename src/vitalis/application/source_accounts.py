"""Application use cases for managing vendor source accounts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .ports import UnitOfWorkPort


@dataclass(frozen=True)
class SourceAccountRevokeResult:
    """Safe API projection of a source-account revoke operation."""

    status: str
    source: str
    user_id: str
    revoked: bool
    account: dict | None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source": self.source,
            "user_id": self.user_id,
            "revoked": self.revoked,
            "account": self.account,
        }


class SourceAccountService:
    """Coordinate source-account changes without depending on a SQL adapter."""

    def __init__(self, unit_of_work_factory: Callable[[], UnitOfWorkPort]):
        self._unit_of_work_factory = unit_of_work_factory

    def revoke(
        self,
        user_id: str,
        source: str = "zepp",
        *,
        reason: str = "source_account_revoked",
    ) -> SourceAccountRevokeResult:
        with self._unit_of_work_factory() as unit_of_work:
            changed = unit_of_work.repository.revoke_source_account(
                user_id, source, reason=reason
            )
            account = unit_of_work.repository.source_account_status(user_id, source)
            unit_of_work.commit()
        return SourceAccountRevokeResult(
            status="revoked" if changed else "not_connected",
            source=source,
            user_id=user_id,
            revoked=changed,
            account=account,
        )
