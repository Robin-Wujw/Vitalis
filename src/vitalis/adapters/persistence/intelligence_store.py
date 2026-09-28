"""SQL composition for the intelligence application ports.

The application layer receives this adapter from bootstrap and never imports
SQLAlchemy, ORM rows, or process configuration itself.
"""

from __future__ import annotations

from typing import Callable

from vitalis.application.ports import IntelligenceUnitOfWork

from . import database


class SqlIntelligenceUnitOfWork:
    """Adapt the shared SQL UnitOfWork to the intelligence port."""

    def __init__(self, factory: Callable[[], database.UnitOfWork]) -> None:
        self._factory = factory
        self._delegate: database.UnitOfWork | None = None
        self.repository = None
        self.transaction = None

    def __enter__(self) -> "SqlIntelligenceUnitOfWork":
        if self._delegate is not None:
            raise RuntimeError("intelligence unit of work cannot be re-entered")
        self._delegate = self._factory()
        self._delegate.__enter__()
        self.repository = self._delegate.repository
        self.transaction = self._delegate.transaction
        return self

    def commit(self) -> None:
        if self._delegate is None:
            raise RuntimeError("intelligence unit of work is not active")
        self._delegate.commit()

    def rollback(self) -> None:
        if self._delegate is not None:
            self._delegate.rollback()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._delegate is None:
            return False
        try:
            return self._delegate.__exit__(exc_type, exc_value, traceback)
        finally:
            self._delegate = None
            self.repository = None
            self.transaction = None


class SqlIntelligenceStore:
    """Factory for SQL-backed intelligence transactions."""

    def __init__(
        self,
        uow_factory: Callable[[], database.UnitOfWork] = database.UnitOfWork,
    ) -> None:
        self._uow_factory = uow_factory

    def uow(self) -> IntelligenceUnitOfWork:
        return SqlIntelligenceUnitOfWork(self._uow_factory)


__all__ = ["SqlIntelligenceStore", "SqlIntelligenceUnitOfWork"]
