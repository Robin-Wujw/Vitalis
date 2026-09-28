"""Current Zepp identity ownership and database constraints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import pytest

from vitalis.domain import AuthToken
from vitalis.adapters.persistence.access_tokens import authenticate_access_token, provision_access_token, token_digest
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.repositories import HealthRepository, SourceIdentityConflict
from vitalis.adapters.persistence import models as orm


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'identity.db').as_posix()}")
    init_db(engine)
    return engine


def _token(user_id: str, source_user_id: str, value: str = "synthetic-token") -> AuthToken:
    return AuthToken(user_id=user_id, source="zepp", access_token=value, source_user_id=source_user_id)


def test_save_token_claims_vendor_identity_and_preserves_projection(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as db:
            with db.begin():
                repo = HealthRepository(db)
                db.add(orm.User(id="owner"))
                repo.save_token(_token("owner", " vendor-a "))
                repo.upsert_user("owner", name="Updated")
                repo.save_token(AuthToken(
                    user_id="owner", source="garmin", access_token="synthetic-garmin",
                    source_user_id="garmin-a",
                ))
        with Session(engine) as db:
            user = db.get(orm.User, "owner")
            assert user.name == "Updated"
            accounts = db.execute(select(orm.SourceAccount).where(
                orm.SourceAccount.user_id == "owner"
            )).scalars().all()
            assert {(row.source, row.vendor_id) for row in accounts} == {
                ("zepp", "vendor-a"), ("garmin", "garmin-a")
            }
            assert HealthRepository(db).get_token("owner", "zepp").source_user_id == "vendor-a"
            assert HealthRepository(db).get_token("owner", "garmin").source_user_id == "garmin-a"
    finally:
        engine.dispose()


def test_claim_rejects_other_user_even_if_precheck_is_bypassed(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as db:
            with db.begin():
                db.add(orm.User(id="owner"))
                HealthRepository(db).save_token(_token("owner", "vendor-shared"))
        with Session(engine) as db:
            with db.begin():
                db.add(orm.User(id="attacker"))
            repo = HealthRepository(db)
            repo.source_identity_owned_by_other = lambda *_args: False
            with pytest.raises(SourceIdentityConflict):
                repo.save_token(_token("attacker", "vendor-shared"))
            db.rollback()
        with Session(engine) as db:
            owners = db.execute(select(orm.SourceAccount.user_id).where(
                orm.SourceAccount.source == "zepp",
                orm.SourceAccount.vendor_id == "vendor-shared",
            )).scalars().all()
            assert owners == ["owner"]
    finally:
        engine.dispose()


def test_database_rejects_duplicate_local_source_and_cross_user_vendor_identity(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as db:
            with db.begin():
                db.add_all([orm.User(id="owner"), orm.User(id="other")])
                db.add(orm.SourceAccount(
                    id="account-a", user_id="owner", source="zepp", vendor_id="vendor-a"
                ))
        with Session(engine) as db:
            db.add(orm.SourceAccount(
                id="account-b", user_id="owner", source="zepp", vendor_id="vendor-b"
            ))
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()
        with Session(engine) as db:
            db.add(orm.SourceAccount(
                id="account-c", user_id="other", source="zepp", vendor_id="vendor-a"
            ))
            with pytest.raises(IntegrityError):
                db.flush()
    finally:
        engine.dispose()


def test_current_delete_releases_vendor_ownership_and_revokes_link(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as db:
            with db.begin():
                repo = HealthRepository(db)
                db.add_all([orm.User(id="owner"), orm.User(id="new-owner")])
                repo.save_token(_token("owner", "vendor-released"))
                repo.create_browser_link("synthetic-link", "owner")
                api_token = provision_access_token(
                    db, "owner", {"read"},
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
                repo.delete_for_user("owner")
                repo.save_token(_token("new-owner", "vendor-released"))
                repo.upsert_user("owner")
        with Session(engine) as db:
            repo = HealthRepository(db)
            assert repo.get_token("owner", "zepp") is None
            assert repo.get_token("new-owner", "zepp") is not None
            assert repo.browser_link("synthetic-link") is None
            assert db.get(orm.User, "owner").source_user_id is None
            assert db.get(orm.AccessToken, token_digest(api_token)) is None
            assert authenticate_access_token(db, api_token) is None
    finally:
        engine.dispose()
