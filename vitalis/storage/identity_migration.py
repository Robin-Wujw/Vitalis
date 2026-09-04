"""Audit and migrate authoritative vendor identity ownership."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import models as orm
from .database import Base, SessionLocal, get_engine


class SourceIdentityMigrationRequired(RuntimeError):
    """The database cannot enforce unique vendor identity ownership yet."""


@dataclass(frozen=True)
class DuplicateSourceIdentity:
    source: str
    source_user_id: str
    user_ids: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateLocalSource:
    user_id: str
    source: str
    token_count: int


@dataclass(frozen=True)
class MismatchedIdentityProjection:
    user_id: str
    token_source: str
    token_source_user_id: str
    user_source: str
    user_source_user_id: str


@dataclass(frozen=True)
class MissingTokenIdentity:
    token_id: int
    user_id: str
    source: str


@dataclass(frozen=True)
class OrphanIdentityProjection:
    user_id: str
    source: str
    source_user_id: str


@dataclass(frozen=True)
class SourceIdentityAudit:
    duplicate_identities: tuple[DuplicateSourceIdentity, ...]
    duplicate_local_sources: tuple[DuplicateLocalSource, ...]
    mismatched_projections: tuple[MismatchedIdentityProjection, ...]
    missing_token_identities: tuple[MissingTokenIdentity, ...]
    orphan_projections: tuple[OrphanIdentityProjection, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.duplicate_identities
            or self.duplicate_local_sources
            or self.mismatched_projections
            or self.missing_token_identities
            or self.orphan_projections
        )

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "duplicate_identities": [asdict(item) for item in self.duplicate_identities],
            "duplicate_local_sources": [
                asdict(item) for item in self.duplicate_local_sources
            ],
            "mismatched_projections": [
                asdict(item) for item in self.mismatched_projections
            ],
            "missing_token_identities": [
                asdict(item) for item in self.missing_token_identities
            ],
            "orphan_projections": [
                asdict(item) for item in self.orphan_projections
            ],
        }


@dataclass(frozen=True)
class IdentityResolution:
    source: str
    source_user_id: str
    canonical_user_id: str
    released_user_ids: tuple[str, ...]
    deleted_tokens: int
    revoked_browser_links: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LocalSourceResolution:
    user_id: str
    source: str
    canonical_source_user_id: str
    deleted_tokens: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MissingIdentityAssignment:
    token_id: int
    user_id: str
    source: str
    source_user_id: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectionResolution:
    user_id: str
    source: str
    source_user_id: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProjectionClearance:
    user_id: str
    source: str
    source_user_id: str

    def as_dict(self) -> dict:
        return asdict(self)


def audit_source_identities(db: Session) -> SourceIdentityAudit:
    """Find identity conflicts without reading or exposing token values."""
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in db.execute(
        select(orm.User.id, orm.User.source, orm.User.source_user_id).where(
            orm.User.source == "zepp",
            orm.User.source_user_id.is_not(None),
        )
    ):
        owners[(row.source, row.source_user_id)].add(row.id)
    for row in db.execute(
        select(
            orm.AuthToken.user_id,
            orm.AuthToken.source,
            orm.AuthToken.source_user_id,
        ).where(orm.AuthToken.source_user_id.is_not(None))
    ):
        owners[(row.source, row.source_user_id)].add(row.user_id)

    duplicate_identities = tuple(
        DuplicateSourceIdentity(source, source_user_id, tuple(sorted(user_ids)))
        for (source, source_user_id), user_ids in sorted(owners.items())
        if len(user_ids) > 1
    )

    token_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in db.execute(select(orm.AuthToken.user_id, orm.AuthToken.source)):
        token_counts[(row.user_id, row.source)] += 1
    duplicate_local_sources = tuple(
        DuplicateLocalSource(user_id, source, count)
        for (user_id, source), count in sorted(token_counts.items())
        if count > 1
    )

    users = {
        row.id: row
        for row in db.execute(
            select(
                orm.User.id,
                orm.User.source,
                orm.User.source_user_id,
            )
        )
    }
    mismatched = []
    zepp_token_keys: set[tuple[str, str]] = set()
    for token in db.execute(
        select(
            orm.AuthToken.user_id,
            orm.AuthToken.source,
            orm.AuthToken.source_user_id,
        ).where(
            orm.AuthToken.source == "zepp",
            orm.AuthToken.source_user_id.is_not(None),
        )
    ):
        zepp_token_keys.add((token.user_id, token.source_user_id))
        user = users.get(token.user_id)
        if user is None or user.source_user_id is None:
            continue
        if user.source != token.source or user.source_user_id != token.source_user_id:
            mismatched.append(
                MismatchedIdentityProjection(
                    user_id=token.user_id,
                    token_source=token.source,
                    token_source_user_id=token.source_user_id,
                    user_source=user.source,
                    user_source_user_id=user.source_user_id,
                )
            )

    missing_token_identities = tuple(
        MissingTokenIdentity(row.id, row.user_id, row.source)
        for row in db.execute(
            select(
                orm.AuthToken.id,
                orm.AuthToken.user_id,
                orm.AuthToken.source,
            ).where(
                orm.AuthToken.source == "zepp",
                orm.AuthToken.source_user_id.is_(None),
            ).order_by(orm.AuthToken.user_id, orm.AuthToken.id)
        )
    )
    orphan_projections = tuple(
        OrphanIdentityProjection(user.id, user.source, user.source_user_id)
        for user in sorted(users.values(), key=lambda item: item.id)
        if user.source == "zepp"
        and user.source_user_id is not None
        and (user.id, user.source_user_id) not in zepp_token_keys
    )

    return SourceIdentityAudit(
        duplicate_identities=duplicate_identities,
        duplicate_local_sources=duplicate_local_sources,
        mismatched_projections=tuple(sorted(mismatched, key=lambda item: item.user_id)),
        missing_token_identities=missing_token_identities,
        orphan_projections=orphan_projections,
    )


def _sync_zepp_projection(db: Session, user_id: str) -> None:
    source_user_ids = list(
        db.execute(
            select(orm.AuthToken.source_user_id).where(
                orm.AuthToken.user_id == user_id,
                orm.AuthToken.source == "zepp",
                orm.AuthToken.source_user_id.is_not(None),
            )
        ).scalars()
    )
    if len(source_user_ids) > 1:
        raise SourceIdentityMigrationRequired(
            "本地用户仍有多个 Zepp token，必须先执行 resolve-local"
        )
    user = db.get(orm.User, user_id)
    if user is None:
        user = orm.User(id=user_id)
        db.add(user)
    if source_user_ids:
        user.source = "zepp"
        user.source_user_id = source_user_ids[0]
    elif user.source == "zepp":
        user.source_user_id = None


def resolve_source_identity(
    db: Session,
    *,
    source: str,
    source_user_id: str,
    canonical_user_id: str,
) -> IdentityResolution:
    """Release duplicate credentials while preserving every user's health history."""
    audit = audit_source_identities(db)
    group = next(
        (
            item
            for item in audit.duplicate_identities
            if item.source == source and item.source_user_id == source_user_id
        ),
        None,
    )
    if group is None:
        raise ValueError("指定厂商身份当前没有跨本地用户冲突")
    if canonical_user_id not in group.user_ids:
        raise ValueError("canonical_user_id 必须是当前冲突中的本地用户")
    canonical_token = db.execute(
        select(orm.AuthToken.id).where(
            orm.AuthToken.user_id == canonical_user_id,
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == source_user_id,
        ).limit(1)
    ).scalar_one_or_none()
    if canonical_token is None:
        raise ValueError("canonical 用户必须持有该厂商身份的 token")

    canonical = db.get(orm.User, canonical_user_id)
    if canonical is None:
        canonical = orm.User(
            id=canonical_user_id,
            source=source,
            source_user_id=source_user_id,
        )
        db.add(canonical)
    elif canonical.source_user_id and (
        canonical.source != source or canonical.source_user_id != source_user_id
    ):
        raise ValueError("canonical 用户已投影到其他厂商身份")
    else:
        canonical.source = source
        canonical.source_user_id = source_user_id

    released = tuple(user_id for user_id in group.user_ids if user_id != canonical_user_id)
    deleted_tokens = db.execute(
        delete(orm.AuthToken).where(
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == source_user_id,
            orm.AuthToken.user_id.in_(released),
        )
    ).rowcount or 0
    if source == "zepp":
        for user_id in released:
            _sync_zepp_projection(db, user_id)
    revoked_links = 0
    if source == "zepp":
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        revoked_links = db.execute(
            update(orm.ZeppBrowserLink)
            .where(
                orm.ZeppBrowserLink.user_id.in_(released),
                orm.ZeppBrowserLink.revoked_at.is_(None),
            )
            .values(
                status="revoked",
                message="Zepp 身份冲突已解除；此链接不再拥有该账号",
                revoked_at=now,
            )
        ).rowcount or 0
    db.flush()
    return IdentityResolution(
        source=source,
        source_user_id=source_user_id,
        canonical_user_id=canonical_user_id,
        released_user_ids=released,
        deleted_tokens=deleted_tokens,
        revoked_browser_links=revoked_links,
    )


def resolve_local_source_tokens(
    db: Session,
    *,
    user_id: str,
    source: str,
    canonical_source_user_id: str,
) -> LocalSourceResolution:
    """Keep one explicit token identity for a duplicated local source."""
    canonical_source_user_id = canonical_source_user_id.strip()
    rows = list(
        db.execute(
            select(orm.AuthToken)
            .where(
                orm.AuthToken.user_id == user_id,
                orm.AuthToken.source == source,
            )
            .order_by(orm.AuthToken.updated_at.desc(), orm.AuthToken.id.desc())
        ).scalars()
    )
    if len(rows) < 2:
        raise ValueError("指定本地用户和数据源没有重复 token")
    candidates = [
        row for row in rows if row.source_user_id == canonical_source_user_id
    ]
    if not candidates:
        raise ValueError("canonical_source_user_id 不属于当前重复 token")
    token_owner = db.execute(
        select(orm.AuthToken.user_id).where(
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == canonical_source_user_id,
            orm.AuthToken.user_id != user_id,
        ).limit(1)
    ).scalar_one_or_none()
    user_owner = None
    if source == "zepp":
        user_owner = db.execute(
            select(orm.User.id).where(
                orm.User.source == source,
                orm.User.source_user_id == canonical_source_user_id,
                orm.User.id != user_id,
            ).limit(1)
        ).scalar_one_or_none()
    if token_owner is not None or user_owner is not None:
        raise ValueError("canonical 厂商身份已属于其他本地用户")
    keep = candidates[0]
    deleted_tokens = db.execute(
        delete(orm.AuthToken).where(
            orm.AuthToken.id.in_([row.id for row in rows if row.id != keep.id])
        )
    ).rowcount or 0
    if source == "zepp":
        _sync_zepp_projection(db, user_id)
    elif db.get(orm.User, user_id) is None:
        db.add(orm.User(id=user_id))
    db.flush()
    return LocalSourceResolution(
        user_id=user_id,
        source=source,
        canonical_source_user_id=canonical_source_user_id,
        deleted_tokens=deleted_tokens,
    )


def assign_missing_token_identity(
    db: Session,
    *,
    token_id: int,
    source_user_id: str,
) -> MissingIdentityAssignment:
    """Assign an operator-verified vendor ID to one legacy Zepp token."""
    token = db.get(orm.AuthToken, token_id)
    if token is None:
        raise ValueError("指定 token 不存在")
    if token.source != "zepp" or token.source_user_id is not None:
        raise ValueError("指定 token 不是缺少厂商身份的 Zepp token")
    source_user_id = source_user_id.strip()
    if not source_user_id:
        raise ValueError("source_user_id 不能为空")

    token_owner = db.execute(
        select(orm.AuthToken.user_id).where(
            orm.AuthToken.source == token.source,
            orm.AuthToken.source_user_id == source_user_id,
            orm.AuthToken.user_id != token.user_id,
        ).limit(1)
    ).scalar_one_or_none()
    user_owner = db.execute(
        select(orm.User.id).where(
            orm.User.source == token.source,
            orm.User.source_user_id == source_user_id,
            orm.User.id != token.user_id,
        ).limit(1)
    ).scalar_one_or_none()
    if token_owner is not None or user_owner is not None:
        raise ValueError("该厂商身份已属于其他本地用户")

    user = db.get(orm.User, token.user_id)
    if user is None:
        user = orm.User(id=token.user_id)
        db.add(user)
    elif (
        user.source == "zepp"
        and user.source_user_id
        and user.source_user_id != source_user_id
    ):
        raise ValueError("用户的 Zepp 投影与待分配厂商身份不一致")
    token.source_user_id = source_user_id
    user.source = token.source
    user.source_user_id = source_user_id
    db.flush()
    return MissingIdentityAssignment(
        token_id=token.id,
        user_id=token.user_id,
        source=token.source,
        source_user_id=source_user_id,
    )


def resolve_identity_projection(
    db: Session,
    *,
    user_id: str,
    source: str,
    source_user_id: str,
) -> ProjectionResolution:
    """Explicitly align the Zepp user projection to its retained token."""
    if source != "zepp":
        raise ValueError("users.source_user_id 仅投影 Zepp 身份")
    source_user_id = source_user_id.strip()
    token_id = db.execute(
        select(orm.AuthToken.id).where(
            orm.AuthToken.user_id == user_id,
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == source_user_id,
        ).limit(1)
    ).scalar_one_or_none()
    if token_id is None:
        raise ValueError("指定厂商身份不属于该用户的 token")
    token_owner = db.execute(
        select(orm.AuthToken.user_id).where(
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == source_user_id,
            orm.AuthToken.user_id != user_id,
        ).limit(1)
    ).scalar_one_or_none()
    user_owner = db.execute(
        select(orm.User.id).where(
            orm.User.source == source,
            orm.User.source_user_id == source_user_id,
            orm.User.id != user_id,
        ).limit(1)
    ).scalar_one_or_none()
    if token_owner is not None or user_owner is not None:
        raise ValueError("该 Zepp 身份已属于其他本地用户")
    user = db.get(orm.User, user_id)
    if user is None:
        user = orm.User(id=user_id)
        db.add(user)
    user.source = source
    user.source_user_id = source_user_id
    db.flush()
    return ProjectionResolution(user_id, source, source_user_id)


def clear_orphan_identity_projection(
    db: Session,
    *,
    user_id: str,
    source: str,
    source_user_id: str,
) -> ProjectionClearance:
    """Clear an audited Zepp projection that has no matching credential."""
    if source != "zepp":
        raise ValueError("users.source_user_id 仅投影 Zepp 身份")
    source_user_id = source_user_id.strip()
    user = db.get(orm.User, user_id)
    if (
        user is None
        or user.source != source
        or user.source_user_id != source_user_id
    ):
        raise ValueError("指定孤立投影不存在")
    matching_token = db.execute(
        select(orm.AuthToken.id).where(
            orm.AuthToken.user_id == user_id,
            orm.AuthToken.source == source,
            orm.AuthToken.source_user_id == source_user_id,
        ).limit(1)
    ).scalar_one_or_none()
    if matching_token is not None:
        raise ValueError("该投影仍有匹配 token，不能清除")
    user.source_user_id = None
    db.flush()
    return ProjectionClearance(user_id, source, source_user_id)


def migrate_source_identities(db: Session) -> SourceIdentityAudit:
    """Normalize absent Zepp projections after every conflict is resolved."""
    audit = audit_source_identities(db)
    if not audit.clean:
        raise SourceIdentityMigrationRequired(
            "仍存在身份冲突或缺失映射，必须先显式完成解析"
        )
    user_ids = set(db.execute(
        select(orm.AuthToken.user_id).where(
            orm.AuthToken.source == "zepp",
            orm.AuthToken.source_user_id.is_not(None),
        )
    ).scalars())
    for user_id in user_ids:
        _sync_zepp_projection(db, user_id)
    db.flush()
    migrated = audit_source_identities(db)
    if not migrated.clean:
        raise SourceIdentityMigrationRequired(
            "身份迁移后仍存在冲突，事务已中止"
        )
    return migrated


def ensure_source_identity_indexes(engine: Engine) -> None:
    """Create current unique indexes after a clean migration."""
    for table_name in ("users", "auth_tokens"):
        for index in Base.metadata.tables[table_name].indexes:
            if index.name and index.name.startswith("uq_"):
                index.create(bind=engine, checkfirst=True)


def _require_tables(engine: Engine) -> None:
    existing = set(inspect(engine).get_table_names())
    missing = {"users", "auth_tokens"} - existing
    if missing:
        raise SourceIdentityMigrationRequired(
            f"数据库缺少表: {', '.join(sorted(missing))}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit and migrate source identities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit", help="print duplicate identity ownership")

    resolve = subparsers.add_parser("resolve", help="choose one canonical local user")
    resolve.add_argument("--source", default="zepp")
    resolve.add_argument("--source-user-id", required=True)
    resolve.add_argument("--canonical-user-id", required=True)
    resolve.add_argument("--apply", action="store_true")

    resolve_local = subparsers.add_parser(
        "resolve-local", help="choose one token for a duplicated local source"
    )
    resolve_local.add_argument("--user-id", required=True)
    resolve_local.add_argument("--source", default="zepp")
    resolve_local.add_argument("--canonical-source-user-id", required=True)
    resolve_local.add_argument("--apply", action="store_true")

    assign_missing = subparsers.add_parser(
        "assign-missing", help="assign a vendor ID to one legacy Zepp token"
    )
    assign_missing.add_argument("--token-id", type=int, required=True)
    assign_missing.add_argument("--source-user-id", required=True)
    assign_missing.add_argument("--apply", action="store_true")

    resolve_projection = subparsers.add_parser(
        "resolve-projection", help="align a user projection to its retained token"
    )
    resolve_projection.add_argument("--user-id", required=True)
    resolve_projection.add_argument("--source", default="zepp")
    resolve_projection.add_argument("--source-user-id", required=True)
    resolve_projection.add_argument("--apply", action="store_true")

    clear_projection = subparsers.add_parser(
        "clear-projection", help="clear an orphaned Zepp user projection"
    )
    clear_projection.add_argument("--user-id", required=True)
    clear_projection.add_argument("--source", default="zepp")
    clear_projection.add_argument("--source-user-id", required=True)
    clear_projection.add_argument("--apply", action="store_true")

    migrate = subparsers.add_parser("migrate", help="create uniqueness indexes")
    migrate.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = get_engine()
    _require_tables(engine)

    if args.command == "audit":
        with SessionLocal() as db:
            print(json.dumps(audit_source_identities(db).as_dict(), ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        raise SystemExit("这是数据库变更操作；确认审计结果后显式传入 --apply")

    if args.command == "resolve":
        with SessionLocal.begin() as db:
            result = resolve_source_identity(
                db,
                source=args.source,
                source_user_id=args.source_user_id,
                canonical_user_id=args.canonical_user_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "resolve-local":
        with SessionLocal.begin() as db:
            result = resolve_local_source_tokens(
                db,
                user_id=args.user_id,
                source=args.source,
                canonical_source_user_id=args.canonical_source_user_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "assign-missing":
        with SessionLocal.begin() as db:
            result = assign_missing_token_identity(
                db,
                token_id=args.token_id,
                source_user_id=args.source_user_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "resolve-projection":
        with SessionLocal.begin() as db:
            result = resolve_identity_projection(
                db,
                user_id=args.user_id,
                source=args.source,
                source_user_id=args.source_user_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "clear-projection":
        with SessionLocal.begin() as db:
            result = clear_orphan_identity_projection(
                db,
                user_id=args.user_id,
                source=args.source,
                source_user_id=args.source_user_id,
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    with SessionLocal.begin() as db:
        audit = migrate_source_identities(db)
    ensure_source_identity_indexes(engine)
    print(json.dumps(audit.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
