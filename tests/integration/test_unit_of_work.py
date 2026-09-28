"""Explicit transaction boundary tests for the current persistence adapter."""

import pytest
from sqlalchemy import create_engine

from vitalis.adapters.persistence.database import UnitOfWork, init_db
from vitalis.adapters.persistence.models import User
from vitalis.adapters.persistence.repositories import HealthRepository
from vitalis.application.source_accounts import SourceAccountService
from vitalis.domain import AuthToken


def _engine():
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


def test_unit_of_work_requires_explicit_commit():
    engine = _engine()
    try:
        with UnitOfWork(engine) as uow:
            uow.db.add(User(id="rolled-back"))

        with UnitOfWork(engine) as uow:
            assert uow.db.get(User, "rolled-back") is None
            uow.db.add(User(id="committed"))
            uow.commit()

        with UnitOfWork(engine) as uow:
            assert uow.db.get(User, "committed") is not None
    finally:
        engine.dispose()


def test_unit_of_work_rolls_back_exception():
    engine = _engine()
    try:
        with pytest.raises(RuntimeError):
            with UnitOfWork(engine) as uow:
                uow.db.add(User(id="failed"))
                raise RuntimeError("synthetic failure")

        with UnitOfWork(engine) as uow:
            assert uow.db.get(User, "failed") is None
    finally:
        engine.dispose()


def test_source_account_service_composes_explicit_uow():
    engine = _engine()
    try:
        with UnitOfWork(engine) as uow:
            uow.db.add(User(id="source-owner"))
            HealthRepository(uow.db).save_token(AuthToken(
                user_id="source-owner",
                source="zepp",
                source_user_id="vendor-a",
                access_token="synthetic-token",
            ))
            uow.commit()

        service = SourceAccountService(lambda: UnitOfWork(engine))
        result = service.revoke("source-owner", "zepp")
        assert result.revoked is True
        assert result.account["status"] == "revoked"
    finally:
        engine.dispose()
