"""Zepp vendor identity ownership and legacy migration tests."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from vitalis.models import AuthToken, NormalizedDaily, SleepRecord
from vitalis.storage.database import Base
from vitalis.storage.identity_migration import (
    SourceIdentityMigrationRequired,
    assign_missing_token_identity,
    audit_source_identities,
    ensure_source_identity_indexes,
    migrate_source_identities,
    resolve_identity_projection,
    resolve_local_source_tokens,
    resolve_source_identity,
)
from vitalis.storage.repositories import HealthRepository, SourceIdentityConflict
from vitalis.storage import models as orm


IDENTITY_INDEXES = (
    "uq_users_source_identity",
    "uq_auth_tokens_user_source",
    "uq_auth_tokens_source_identity",
)


def _database():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _token(user_id: str, source_user_id: str, value: str = "token") -> AuthToken:
    return AuthToken(
        user_id=user_id,
        source="zepp",
        access_token=value,
        source_user_id=source_user_id,
    )


def _drop_identity_indexes(engine):
    for table_name in ("users", "auth_tokens"):
        for index in Base.metadata.tables[table_name].indexes:
            if index.name in IDENTITY_INDEXES:
                index.drop(bind=engine, checkfirst=True)


def test_save_token_claims_identity_and_updates_user_projection():
    _engine, sessions = _database()

    with sessions.begin() as db:
        repo = HealthRepository(db)
        repo.save_token(_token("local-a", "vendor-a"))
        user = db.get(orm.User, "local-a")
        saved = repo.get_token("local-a", "zepp")

    assert user is not None
    assert user.source == "zepp"
    assert user.source_user_id == "vendor-a"
    assert saved is not None
    assert saved.source_user_id == "vendor-a"


def test_database_constraint_is_final_fence_for_cross_user_claim():
    _engine, sessions = _database()
    with sessions.begin() as db:
        HealthRepository(db).save_token(_token("local-a", "vendor-shared", "first"))

    with sessions.begin() as db:
        repo = HealthRepository(db)
        repo.source_identity_owned_by_other = lambda *_args: False
        with pytest.raises(SourceIdentityConflict):
            repo.save_token(_token("local-b", "vendor-shared", "second"))

    with sessions() as db:
        owners = db.execute(
            select(orm.AuthToken.user_id).where(
                orm.AuthToken.source == "zepp",
                orm.AuthToken.source_user_id == "vendor-shared",
            )
        ).scalars().all()
    assert owners == ["local-a"]


def test_database_allows_only_one_token_per_local_user_and_source():
    _engine, sessions = _database()
    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(id="local-a", source="zepp", source_user_id="vendor-a"),
                orm.AuthToken(
                    user_id="local-a",
                    source="zepp",
                    access_token="one",
                    source_user_id="vendor-a",
                ),
            ]
        )

    with pytest.raises(IntegrityError):
        with sessions.begin() as db:
            db.add(
                orm.AuthToken(
                    user_id="local-a",
                    source="zepp",
                    access_token="two",
                    source_user_id="vendor-b",
                )
            )


def test_init_db_refuses_legacy_duplicate_identity(monkeypatch):
    from vitalis.storage import database

    engine, sessions = _database()
    _drop_identity_indexes(engine)
    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(id="local-b", source="zepp", source_user_id="vendor-shared"),
                orm.AuthToken(
                    user_id="local-a",
                    source="zepp",
                    access_token="legacy-token",
                    source_user_id="vendor-shared",
                ),
            ]
        )
    monkeypatch.setattr(database, "_engine", engine)

    with pytest.raises(SourceIdentityMigrationRequired):
        database.init_db()


def test_delete_for_user_releases_vendor_identity_for_new_owner():
    _engine, sessions = _database()
    with sessions.begin() as db:
        repo = HealthRepository(db)
        repo.save_token(_token("local-a", "vendor-released"))
        repo.create_browser_link("release-link", "local-a")
        repo.delete_for_user("local-a")
        repo.save_token(_token("local-b", "vendor-released"))

    with sessions() as db:
        repo = HealthRepository(db)
        assert repo.get_token("local-a", "zepp") is None
        assert repo.get_token("local-b", "zepp") is not None
        assert repo.browser_link("release-link") is None
        assert db.get(orm.User, "local-a") is None


def test_missing_legacy_token_identity_requires_explicit_assignment():
    engine, sessions = _database()
    _drop_identity_indexes(engine)
    with sessions.begin() as db:
        tokens = [
            orm.AuthToken(
                user_id="local-missing",
                source="zepp",
                access_token=value,
                source_user_id=None,
            )
            for value in ("legacy-one", "legacy-two")
        ]
        db.add_all(tokens)
        db.flush()
        token_id = tokens[0].id

    with sessions.begin() as db:
        audit = audit_source_identities(db)
        assert not audit.clean
        assert audit.duplicate_local_sources[0].token_count == 2
        assert {item.token_id for item in audit.missing_token_identities} == {
            token.id for token in tokens
        }
        with pytest.raises(SourceIdentityMigrationRequired):
            migrate_source_identities(db)
        assigned = assign_missing_token_identity(
            db,
            token_id=token_id,
            source_user_id="vendor-assigned",
        )
        assert assigned.source_user_id == "vendor-assigned"
        resolved = resolve_local_source_tokens(
            db,
            user_id="local-missing",
            source="zepp",
            canonical_source_user_id="vendor-assigned",
        )
        assert resolved.deleted_tokens == 1
        assert migrate_source_identities(db).clean
    ensure_source_identity_indexes(engine)


def test_mismatched_projection_requires_explicit_resolution():
    engine, sessions = _database()
    _drop_identity_indexes(engine)
    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(
                    id="local-mismatch",
                    source="zepp",
                    source_user_id="vendor-old",
                ),
                orm.AuthToken(
                    user_id="local-mismatch",
                    source="zepp",
                    access_token="retained-token",
                    source_user_id="vendor-current",
                ),
            ]
        )

    with sessions.begin() as db:
        audit = audit_source_identities(db)
        assert audit.mismatched_projections
        with pytest.raises(SourceIdentityMigrationRequired):
            migrate_source_identities(db)
        resolved = resolve_identity_projection(
            db,
            user_id="local-mismatch",
            source="zepp",
            source_user_id="vendor-current",
        )
        assert resolved.source_user_id == "vendor-current"
        assert migrate_source_identities(db).clean
    ensure_source_identity_indexes(engine)


def test_local_duplicate_resolution_requires_explicit_vendor_identity():
    engine, sessions = _database()
    _drop_identity_indexes(engine)

    with sessions.begin() as db:
        db.add(orm.User(id="local-a", source="zepp", source_user_id="vendor-old"))
        db.add_all(
            [
                orm.AuthToken(
                    user_id="local-a",
                    source="zepp",
                    access_token="old-token",
                    source_user_id="vendor-old",
                ),
                orm.AuthToken(
                    user_id="local-a",
                    source="zepp",
                    access_token="new-token",
                    source_user_id="vendor-new",
                ),
            ]
        )

    with sessions.begin() as db:
        audit = audit_source_identities(db)
        assert audit.duplicate_local_sources[0].token_count == 2
        result = resolve_local_source_tokens(
            db,
            user_id="local-a",
            source="zepp",
            canonical_source_user_id="vendor-new",
        )
        assert result.deleted_tokens == 1
        assert migrate_source_identities(db).clean
    ensure_source_identity_indexes(engine)

    with sessions() as db:
        token = HealthRepository(db).get_token("local-a", "zepp")
        user = db.get(orm.User, "local-a")
        assert token is not None and token.source_user_id == "vendor-new"
        assert user is not None and user.source_user_id == "vendor-new"


def test_cross_user_resolution_requires_canonical_to_hold_token():
    engine, sessions = _database()
    _drop_identity_indexes(engine)
    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(
                    id="projection-only",
                    source="zepp",
                    source_user_id="vendor-x",
                ),
                orm.AuthToken(
                    user_id="token-owner",
                    source="zepp",
                    access_token="retained-token",
                    source_user_id="vendor-x",
                ),
            ]
        )

    with sessions.begin() as db:
        with pytest.raises(ValueError, match="必须持有"):
            resolve_source_identity(
                db,
                source="zepp",
                source_user_id="vendor-x",
                canonical_user_id="projection-only",
            )
        assert db.execute(select(orm.AuthToken.id)).scalars().all()


def test_non_zepp_resolution_does_not_revoke_browser_links():
    engine, sessions = _database()
    _drop_identity_indexes(engine)
    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(id="garmin-a", source="garmin", source_user_id="garmin-x"),
                orm.User(id="garmin-b", source="garmin", source_user_id="garmin-x"),
                orm.AuthToken(
                    user_id="garmin-a",
                    source="garmin",
                    access_token="one",
                    source_user_id="garmin-x",
                ),
                orm.AuthToken(
                    user_id="garmin-b",
                    source="garmin",
                    access_token="two",
                    source_user_id="garmin-x",
                ),
                orm.ZeppBrowserLink(
                    token_digest="garmin-user-zepp-link",
                    user_id="garmin-b",
                    status="connected",
                ),
            ]
        )

    with sessions.begin() as db:
        result = resolve_source_identity(
            db,
            source="garmin",
            source_user_id="garmin-x",
            canonical_user_id="garmin-a",
        )
        assert result.revoked_browser_links == 0
        link = db.get(orm.ZeppBrowserLink, "garmin-user-zepp-link")
        assert link is not None and link.status == "connected"


def test_legacy_resolution_preserves_health_history_and_revokes_loser_link():
    engine, sessions = _database()
    _drop_identity_indexes(engine)

    with sessions.begin() as db:
        db.add_all(
            [
                orm.User(id="canonical", source="zepp", source_user_id="vendor-x"),
                orm.User(id="legacy-copy", source="zepp", source_user_id="vendor-x"),
                orm.AuthToken(
                    user_id="canonical",
                    source="zepp",
                    access_token="canonical-token",
                    source_user_id="vendor-x",
                ),
                orm.AuthToken(
                    user_id="legacy-copy",
                    source="zepp",
                    access_token="legacy-token",
                    source_user_id="vendor-x",
                ),
                orm.ZeppBrowserLink(
                    token_digest="legacy-link",
                    user_id="legacy-copy",
                    status="connected",
                ),
            ]
        )
        repo = HealthRepository(db)
        for user_id in ("canonical", "legacy-copy"):
            repo.save_daily(
                NormalizedDaily(
                    user_id=user_id,
                    date=date(2026, 9, 4),
                    sleep=SleepRecord(
                        user_id=user_id,
                        date=date(2026, 9, 4),
                        sleep_duration=420,
                    ),
                )
            )

    with sessions.begin() as db:
        audit = audit_source_identities(db)
        assert not audit.clean
        assert audit.duplicate_identities[0].user_ids == (
            "canonical",
            "legacy-copy",
        )
        result = resolve_source_identity(
            db,
            source="zepp",
            source_user_id="vendor-x",
            canonical_user_id="canonical",
        )
        assert result.released_user_ids == ("legacy-copy",)
        assert result.deleted_tokens == 1
        assert result.revoked_browser_links == 1

    with sessions.begin() as db:
        audit = migrate_source_identities(db)
        assert audit.clean
    ensure_source_identity_indexes(engine)

    with sessions() as db:
        repo = HealthRepository(db)
        assert repo.get_token("canonical", "zepp") is not None
        assert repo.get_token("legacy-copy", "zepp") is None
        assert repo.get_sleep("canonical", date(2026, 9, 4)) is not None
        assert repo.get_sleep("legacy-copy", date(2026, 9, 4)) is not None
        legacy_user = db.get(orm.User, "legacy-copy")
        legacy_link = db.get(orm.ZeppBrowserLink, "legacy-link")
        assert legacy_user is not None and legacy_user.source_user_id is None
        assert legacy_link is not None and legacy_link.status == "revoked"
        assert legacy_link.revoked_at is not None

    with pytest.raises(IntegrityError):
        with sessions.begin() as db:
            db.add(
                orm.AuthToken(
                    user_id="third-user",
                    source="zepp",
                    access_token="third-token",
                    source_user_id="vendor-x",
                )
            )
