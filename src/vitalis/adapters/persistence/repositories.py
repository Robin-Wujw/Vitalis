"""仓储层：封装对 ORM 的读写，业务层只依赖仓储接口。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import DateTime, Integer, Text, and_, case, cast, delete, exists, func, or_, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vitalis.application.ports import (
    BrowserLinkClaim,
    FeedbackIdempotencyConflict,
    PairingClaim,
    SourceClaim,
)
from vitalis.domain import (
    AuthToken,
    Device,
    NormalizedDaily,
    DailyMetric,
    DenseDataFile,
    MetricSample,
    TrainingRecord,
    Workout,
    WORKOUT_DETAIL_SCHEMA_VERSION,
    WorkoutMetricSample,
)
from vitalis.intelligence.contracts import (
    HealthEvent,
    HealthEventObservation,
    EventLifecycle,
    RecommendationInstance,
    RecommendationStatus,
    StrengthExerciseRecord,
    SubjectiveFeedback,
    TrainingPreferenceInput,
    TrainingPreferencePatch,
    TrainingPreferences,
    DAILY_SCHEMA_VERSION,
    WEEKLY_SCHEMA_VERSION,
    MONTHLY_SCHEMA_VERSION,
    INTELLIGENCE_VERSION,
    DECISION_POLICY_VERSION,
    EVIDENCE_VERSION,
    ConfidenceBand,
    ProfileRevisionConflict,
    ProfileSource,
    ProfileField,
    Sex,
    UserProfile,
    UserProfilePatch,
)

from vitalis.time import local_day, local_day_utc_bounds

from . import models as orm


class SourceIdentityConflict(ValueError):
    """A vendor identity is already owned by another local user."""


class SyncIdempotencyConflict(ValueError):
    """One API idempotency key was reused for a different sync command."""


_EXPECTED_UNSET = object()


@dataclass(frozen=True)
class WorkoutAnalysisSample:
    source: str
    workout_id: str
    timestamp: datetime
    metric: str
    value: float
    unit: str
    source_scope: str
    device_id: str | None


def _current_analysis_config_digest() -> str:
    from vitalis.application.analysis_policy import analysis_policy_digest, installed_analysis_rules_digest
    from vitalis.adapters.zepp.catalog import load_catalog
    from vitalis.config import settings

    return analysis_policy_digest(
        settings.timezone,
        INTELLIGENCE_VERSION,
        DECISION_POLICY_VERSION,
        EVIDENCE_VERSION,
        load_catalog().catalog_revision,
        installed_analysis_rules_digest(),
    )


_CURRENT_SNAPSHOT_SCHEMAS = {
    "daily": DAILY_SCHEMA_VERSION,
    "weekly": WEEKLY_SCHEMA_VERSION,
    "monthly": MONTHLY_SCHEMA_VERSION,
    "training_responses": "1.0",
    "personal_model": "2.0",
    "personal_associations": "1.0",
}


class HealthRepository:
    """健康数据仓储：负责 vitalis.domain（schema）<-> ORM（表）的映射。"""

    def __init__(self, db: Session):
        self.db = db

    # ---- 用户 ----
    def user_exists(self, user_id: str) -> bool:
        return self.db.get(orm.User, user_id) is not None

    def upsert_user(
        self,
        user_id: str,
        name: str = "",
        source: str = "zepp",
        source_user_id: str | None = None,
    ) -> orm.User:
        """Create/update only the local user; vendor identity belongs to SourceAccount."""
        user = self.db.get(orm.User, user_id)
        if user is None:
            user = orm.User(id=user_id, name=name)
            self.db.add(user)
        elif name:
            user.name = name
        if source_user_id is not None:
            self.ensure_source_account(user_id, source, source_user_id)
        return user

    def analysis_input_revision(self, user_id: str) -> int:
        row = self.db.get(orm.User, user_id)
        return int(row.analysis_input_revision or 0) if row is not None else 0

    def bump_analysis_input_revision(self, user_id: str) -> int:
        result = self.db.execute(
            update(orm.User)
            .where(orm.User.id == user_id)
            .values(analysis_input_revision=orm.User.analysis_input_revision + 1)
        )
        if not result.rowcount:
            raise ValueError("用户不存在")
        self.db.flush()
        return self.analysis_input_revision(user_id)

    def lock_analysis_input_revision(self, user_id: str, revision: int) -> bool:
        # The conditional write serializes input changes with result publication.
        result = self.db.execute(
            update(orm.User)
            .where(
                orm.User.id == user_id,
                orm.User.analysis_input_revision == revision,
            )
            .values(analysis_input_revision=orm.User.analysis_input_revision)
        )
        return bool(result.rowcount)

    def _bump_existing_input_revisions(self, user_ids: set[str]) -> None:
        for user_id in sorted(user_ids):
            if self.db.get(orm.User, user_id) is not None:
                self.bump_analysis_input_revision(user_id)

    def source_account(
        self,
        user_id: str,
        source: str = "zepp",
        *,
        for_update: bool = False,
        active_only: bool = False,
    ) -> orm.SourceAccount | None:
        statement = select(orm.SourceAccount).where(
            orm.SourceAccount.user_id == user_id,
            orm.SourceAccount.source == source,
        )
        if active_only:
            statement = statement.where(orm.SourceAccount.status == "active")
        if for_update:
            statement = statement.with_for_update()
        return self.db.execute(statement).scalar_one_or_none()

    def source_account_by_id(
        self, account_id: str, *, for_update: bool = False
    ) -> orm.SourceAccount | None:
        statement = select(orm.SourceAccount).where(orm.SourceAccount.id == account_id)
        if for_update:
            statement = statement.with_for_update()
        return self.db.execute(statement).scalar_one_or_none()

    def ensure_source_account(
        self, user_id: str, source: str, vendor_id: str, *,
        allow_reactivate: bool = True,
    ) -> orm.SourceAccount:
        """Claim or reactivate one source identity without rebinding another user.

        The local user row is always locked before any source-account row.  This
        matches deletion and revocation, so a credential callback cannot recreate
        an owner after a concurrent destructive operation has committed.
        """
        vendor_id = vendor_id.strip()
        if not source or not vendor_id:
            raise SourceIdentityConflict("厂商账号必须包含来源和厂商用户 id")
        # ``upsert_user`` may have a pending local row; flush it before the
        # user-first lock ordering used by revoke/delete and credential writes.
        self.db.flush()
        owner_user = self.db.execute(select(orm.User).where(
            orm.User.id == user_id,
        ).with_for_update()).scalar_one_or_none()
        if owner_user is None:
            raise SourceIdentityConflict("本地用户不存在")
        account = self.source_account(user_id, source, for_update=True)
        owner = self.db.execute(select(orm.SourceAccount).where(
            orm.SourceAccount.source == source,
            orm.SourceAccount.vendor_id == vendor_id,
        ).with_for_update()).scalar_one_or_none()
        if owner is not None and owner.user_id != user_id:
            raise SourceIdentityConflict("该厂商账号已绑定到其他本地用户")
        if account is not None and account.vendor_id != vendor_id:
            raise SourceIdentityConflict("当前本地用户已绑定其他厂商账号")
        identity_changed = account is None or account.status != "active"
        if account is None:
            account = orm.SourceAccount(
                id=uuid4().hex,
                user_id=user_id,
                source=source,
                vendor_id=vendor_id,
                status="active",
                fence_epoch=0,
            )
            self.db.add(account)
        elif account.status != "active":
            if not allow_reactivate:
                raise SourceIdentityConflict("数据源账号已撤销，请重新发起配对")
            account.status = "active"
            account.revoked_at = None
            account.fence_epoch = int(account.fence_epoch or 0) + 1
        account.updated_at = datetime.utcnow()
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise SourceIdentityConflict("该厂商账号已绑定到其他本地用户") from exc
        if identity_changed:
            self.bump_analysis_input_revision(user_id)
        return account

    def identity_context(self, user_id: str) -> dict:
        """Describe local/vendor identity mapping without merging any records."""
        account = self.db.execute(select(orm.SourceAccount).where(
            orm.SourceAccount.user_id == user_id,
        ).order_by(orm.SourceAccount.source).limit(1)).scalar_one_or_none()
        source = account.source if account else "zepp"
        vendor_id = account.vendor_id if account else None
        local_user_ids = {user_id}
        if account is not None:
            local_user_ids.update(self.db.execute(select(orm.SourceAccount.user_id).where(
                orm.SourceAccount.source == account.source,
                orm.SourceAccount.vendor_id == account.vendor_id,
            )).scalars().all())
        return {
            "local_user_id": user_id,
            "source": source,
            "source_user_id_present": vendor_id is not None,
            "shared_local_user_count": len(local_user_ids),
            "source_account_id": account.id if account else None,
            "source_account_status": account.status if account else None,
        }

    def source_identity_owned_by_other(
        self, user_id: str, source: str, source_user_id: str
    ) -> bool:
        source_user_id = source_user_id.strip()
        if not source_user_id:
            return False
        return self.db.execute(select(orm.SourceAccount.id).where(
            orm.SourceAccount.source == source,
            orm.SourceAccount.vendor_id == source_user_id,
            orm.SourceAccount.user_id != user_id,
        ).limit(1)).scalar_one_or_none() is not None

    def claim_source_account(self, user_id: str, source: str = "zepp") -> SourceClaim:
        """Lock the local owner and snapshot source fencing before vendor I/O."""
        owner = self.db.execute(select(orm.User).where(
            orm.User.id == user_id,
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            raise SourceIdentityConflict("本地用户不存在")
        account = self.source_account(user_id, source, for_update=True)
        return SourceClaim(
            user_id=user_id,
            source=source,
            account_id=account.id if account else None,
            fence_epoch=int(account.fence_epoch) if account else None,
            vendor_id=account.vendor_id if account else None,
            status=account.status if account else None,
        )

    def source_claim_current(self, claim: SourceClaim) -> bool:
        """Check a pre-I/O source snapshot under the caller's transaction."""
        owner = self.db.execute(select(orm.User).where(
            orm.User.id == claim.user_id,
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            return False
        account = self.source_account(claim.user_id, claim.source, for_update=True)
        if claim.account_id is None:
            return account is None
        return bool(
            account is not None
            and account.id == claim.account_id
            and account.status == "active"
            and int(account.fence_epoch) == int(claim.fence_epoch or 0)
        )

    # ---- 设备 ----
    def upsert_device(self, device: Device) -> orm.Device:
        # Flush a caller's pending User before FK-enforced device insertion.
        self.db.flush()
        stable_id = hashlib.sha256(
            f"{device.user_id}:{device.source}:{device.device_id}".encode("utf-8")
        ).hexdigest()
        model = device.model or ""
        dialect = self.db.get_bind().dialect.name
        changed = False
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(orm.Device).values(
                id=stable_id,
                user_id=device.user_id,
                source=device.source,
                model=model,
                device_id=device.device_id,
            )
            result = self.db.execute(
                statement.on_conflict_do_update(
                    index_elements=["id"],
                    set_={"model": statement.excluded.model},
                    where=(
                        (statement.excluded.model != "")
                        & (orm.Device.model != statement.excluded.model)
                    ),
                ).returning(orm.Device.user_id)
            )
            changed = bool(result.fetchall())
        else:
            row = self.db.execute(select(orm.Device).where(
                orm.Device.id == stable_id,
            )).scalar_one_or_none()
            changed = row is None or (bool(model) and row.model != model)
            if row is None:
                row = orm.Device(
                    id=stable_id,
                    user_id=device.user_id,
                    source=device.source,
                    model=model,
                    device_id=device.device_id,
                )
                self.db.add(row)
            elif model:
                row.model = model
        self.db.flush()
        if changed:
            self._bump_existing_input_revisions({device.user_id})
        row = self.db.get(orm.Device, stable_id)
        assert row is not None
        return row

    def devices(self, user_id: str) -> list[orm.Device]:
        return list(self.db.execute(
            select(orm.Device).where(orm.Device.user_id == user_id).order_by(
                orm.Device.model, orm.Device.device_id
            )
        ).scalars().all())

    # ---- 每日健康 ----
    def save_daily(self, daily: NormalizedDaily) -> None:
        """Persist normalized daily facts and invalidate changed analysis inputs."""
        changed = False
        if daily.sleep:
            changed |= self._upsert(orm.SleepRecord, daily.user_id, daily.date,
                                    daily.sleep.model_dump(mode="json", exclude_none=True))
        if daily.activity:
            changed |= self._upsert(orm.ActivityRecord, daily.user_id, daily.date,
                                    daily.activity.model_dump(mode="json", exclude_none=True))
        if daily.training:
            changed |= self._upsert(orm.TrainingRecord, daily.user_id, daily.date,
                                    daily.training.model_dump(mode="json", exclude_none=True))
        if daily.metric_samples:
            for sample in daily.metric_samples:
                sample.user_id = daily.user_id
            written, _changed_users = self._save_metric_samples(daily.metric_samples)
            changed |= written > 0 and bool(_changed_users)

        self.db.flush()
        if changed:
            self._bump_existing_input_revisions({daily.user_id})

    def _upsert(self, model, user_id: str, day, data: dict) -> bool:
        """Atomically upsert one current-contract daily row by ``(user_id, date)``."""
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(model).values(
                user_id=user_id,
                date=day,
                data=data,
            )
            changed = self.db.execute(
                statement.on_conflict_do_update(
                    index_elements=["user_id", "date"],
                    set_={"data": statement.excluded.data},
                    where=cast(model.data, Text) != cast(statement.excluded.data, Text),
                ).returning(model.user_id)
            ).fetchall()
            return bool(changed)

        existing = self.db.execute(select(model).where(
            model.user_id == user_id,
            model.date == day,
        )).scalar_one_or_none()
        changed = existing is None or existing.data != data
        if existing is None:
            self.db.add(model(user_id=user_id, date=day, data=data))
        else:
            existing.data = data
        return changed

    # ---- 查询 ----
    def get_sleep(self, user_id: str, day: date) -> dict | None:
        row = self.db.execute(
            select(orm.SleepRecord).where(orm.SleepRecord.user_id == user_id, orm.SleepRecord.date == day)
        ).scalar_one_or_none()
        return row.data if row else None

    def sleep_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.SleepRecord).where(
                orm.SleepRecord.user_id == user_id,
                orm.SleepRecord.date.between(start, end),
            ).order_by(orm.SleepRecord.date)
        ).scalars().all()
        return [r.data for r in rows]


    def activity_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.ActivityRecord).where(
                orm.ActivityRecord.user_id == user_id,
                orm.ActivityRecord.date.between(start, end),
            ).order_by(orm.ActivityRecord.date)
        ).scalars().all()
        return [r.data for r in rows]

    def training_range(self, user_id: str, start: date, end: date) -> list[dict]:
        rows = self.db.execute(
            select(orm.TrainingRecord).where(
                orm.TrainingRecord.user_id == user_id,
                orm.TrainingRecord.date.between(start, end),
            ).order_by(orm.TrainingRecord.date)
        ).scalars().all()
        return [r.data for r in rows]

    # ---- 通用指标时序 ----

    def save_metric_samples(self, samples: list[MetricSample]) -> int:
        """Idempotently upsert timestamped measurements."""
        written, changed_users = self._save_metric_samples(samples)
        self._bump_existing_input_revisions(changed_users)
        return written

    def _save_metric_samples(self, samples: list[MetricSample]) -> tuple[int, set[str]]:
        deduplicated: dict[
            tuple[str, str, str, datetime, str, str, str, int], MetricSample
        ] = {}
        for sample in samples:
            key = (
                sample.user_id,
                sample.source,
                sample.metric,
                _naive_utc(sample.timestamp),
                sample.source_scope or "unknown",
                sample.device_id or "",
                sample.source_record_id or "",
                sample.sample_ordinal if sample.sample_ordinal is not None else 0,
            )
            deduplicated[key] = sample

        if not deduplicated:
            return 0, set()

        changed_users: set[str] = set()
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert

            rows = [
                {
                    "user_id": user_id,
                    "source": source,
                    "metric": metric,
                    "timestamp": timestamp,
                    "source_scope": source_scope,
                    "device_id": device_id,
                    "source_record_id": source_record_id,
                    "sample_ordinal": sample_ordinal,
                    "value": sample.value,
                    "unit": sample.unit,
                }
                for (
                    user_id, source, metric, timestamp, source_scope, device_id,
                    source_record_id, sample_ordinal,
                ), sample in deduplicated.items()
            ]
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.MetricSample).values(rows[offset:offset + 500])
                result = self.db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            "user_id", "source", "metric", "timestamp", "source_scope",
                            "device_id", "source_record_id", "sample_ordinal",
                        ],
                        set_={
                            "value": statement.excluded.value,
                            "unit": statement.excluded.unit,
                        },
                        where=(
                            orm.MetricSample.value.is_distinct_from(statement.excluded.value)
                            | orm.MetricSample.unit.is_distinct_from(statement.excluded.unit)
                        ),
                    ).returning(orm.MetricSample.user_id)
                )
                changed_users.update(row[0] for row in result.fetchall())
            self.db.flush()
            return len(rows), changed_users

        written = 0
        for (
            user_id, source, metric, timestamp, source_scope, device_id,
            source_record_id, sample_ordinal,
        ), sample in deduplicated.items():
            row = self.db.execute(
                select(orm.MetricSample).where(
                    orm.MetricSample.user_id == user_id,
                    orm.MetricSample.source == source,
                    orm.MetricSample.metric == metric,
                    orm.MetricSample.timestamp == timestamp,
                    orm.MetricSample.source_scope == source_scope,
                    orm.MetricSample.device_id == device_id,
                    orm.MetricSample.source_record_id == source_record_id,
                    orm.MetricSample.sample_ordinal == sample_ordinal,
                )
            ).scalar_one_or_none()
            changed = row is None or row.value != sample.value or row.unit != sample.unit
            if row is None:
                row = orm.MetricSample(
                    user_id=user_id,
                    source=source,
                    metric=metric,
                    timestamp=timestamp,
                    source_scope=source_scope,
                    device_id=device_id,
                    source_record_id=source_record_id,
                    sample_ordinal=sample_ordinal,
                )
                self.db.add(row)
            row.value = sample.value
            row.unit = sample.unit
            if changed:
                changed_users.add(user_id)
            written += 1
        self.db.flush()
        return written, changed_users

    def metric_samples(
        self, user_id: str, metric: str, start: datetime, end: datetime, limit: int = 50_000
    ) -> list[orm.MetricSample]:
        return list(self.metric_sample_rows(user_id, metric, start, end, limit=limit))

    def metric_sample_rows(
        self,
        user_id: str,
        metric: str,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ):
        statement = select(orm.MetricSample).where(
            orm.MetricSample.user_id == user_id,
            orm.MetricSample.metric == metric,
            orm.MetricSample.timestamp.between(_naive_utc(start), _naive_utc(end)),
        ).order_by(orm.MetricSample.timestamp, orm.MetricSample.source_record_id, orm.MetricSample.id)
        if limit is not None:
            statement = statement.limit(limit)
        return self.db.execute(
            statement.execution_options(yield_per=10_000)
        ).scalars()

    def heart_rate_minute_medians(
        self, user_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float, str, str, str | None, str]]:
        """Aggregate a dense heart-rate window before rows leave the database."""
        params = {
            "user_id": user_id,
            "start": _naive_utc(start),
            "end": _naive_utc(end),
        }
        if self.db.get_bind().dialect.name == "postgresql":
            statement = text("""
                SELECT date_trunc('minute', timestamp) AS minute,
                       source, source_scope, device_id, unit,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS median_value
                FROM metric_samples
                WHERE user_id = :user_id
                  AND metric = 'heart_rate'
                  AND timestamp BETWEEN :start AND :end
                  AND value BETWEEN 25 AND 240
                GROUP BY minute, source, source_scope, device_id, unit
                ORDER BY minute, source, source_scope, device_id, unit
            """)
        else:
            statement = text("""
                WITH ranked AS (
                    SELECT strftime('%Y-%m-%d %H:%M:00', timestamp) AS minute,
                           source, source_scope, device_id, unit, value,
                           row_number() OVER (
                               PARTITION BY strftime('%Y-%m-%d %H:%M:00', timestamp),
                                            source, source_scope, device_id, unit
                               ORDER BY value
                           ) AS sample_rank,
                           count(*) OVER (
                               PARTITION BY strftime('%Y-%m-%d %H:%M:00', timestamp),
                                            source, source_scope, device_id, unit
                           ) AS sample_count
                    FROM metric_samples
                    WHERE user_id = :user_id
                      AND metric = 'heart_rate'
                      AND timestamp BETWEEN :start AND :end
                      AND value BETWEEN 25 AND 240
                )
                SELECT minute, source, source_scope, device_id, unit,
                       avg(value) AS median_value
                FROM ranked
                WHERE sample_rank IN (
                    (sample_count + 1) / 2,
                    (sample_count + 2) / 2
                )
                GROUP BY minute, source, source_scope, device_id, unit
                ORDER BY minute, source, source_scope, device_id, unit
            """)
        output = []
        for row in self.db.execute(statement, params).mappings():
            minute = row["minute"]
            if isinstance(minute, str):
                minute = datetime.fromisoformat(minute)
            output.append((
                minute,
                float(row["median_value"]),
                row["source"],
                row["source_scope"],
                row["device_id"] or None,
                row["unit"],
            ))
        return output

    def metric_window_summaries(
        self,
        user_id: str,
        metrics: tuple[str, ...],
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Aggregate a bounded single-local-day metric window in SQL.

        This intentionally returns group summaries rather than raw points.  The
        caller supplies UTC bounds derived from a local day; a 23/24/25-hour
        window is accepted, while broad history queries are rejected here.
        """
        allowed_metrics = {"heart_rate", "stress"}
        requested = tuple(dict.fromkeys(metrics))
        if not requested or any(metric not in allowed_metrics for metric in requested):
            raise ValueError("metric_window_summaries 仅支持 heart_rate/stress")
        start_utc = _naive_utc(start)
        end_utc = _naive_utc(end)
        if end_utc <= start_utc:
            raise ValueError("指标窗口结束时间必须晚于开始时间")
        if end_utc - start_utc > timedelta(hours=26):
            raise ValueError("指标窗口必须是单一本地日的合理范围")

        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            minute_bucket = func.strftime(
                "%Y-%m-%d %H:%M:00", orm.MetricSample.timestamp
            )
        elif dialect == "postgresql":
            minute_bucket = func.date_trunc("minute", orm.MetricSample.timestamp)
        else:
            raise ValueError("metric_window_summaries 仅支持 SQLite/PostgreSQL")

        statement = (
            select(
                orm.MetricSample.metric,
                orm.MetricSample.unit,
                orm.MetricSample.source,
                orm.MetricSample.source_scope,
                orm.MetricSample.device_id,
                func.count(orm.MetricSample.id).label("sample_count"),
                func.count(func.distinct(minute_bucket)).label("observed_minutes"),
                func.min(orm.MetricSample.timestamp).label("first_observed_at"),
                func.max(orm.MetricSample.timestamp).label("last_observed_at"),
                func.min(orm.MetricSample.value).label("minimum"),
                func.max(orm.MetricSample.value).label("maximum"),
                func.avg(orm.MetricSample.value).label("average"),
            )
            .where(
                orm.MetricSample.user_id == user_id,
                orm.MetricSample.metric.in_(requested),
                orm.MetricSample.timestamp >= start_utc,
                orm.MetricSample.timestamp < end_utc,
            )
            .group_by(
                orm.MetricSample.metric,
                orm.MetricSample.unit,
                orm.MetricSample.source,
                orm.MetricSample.source_scope,
                orm.MetricSample.device_id,
            )
            .order_by(
                orm.MetricSample.metric,
                orm.MetricSample.source,
                orm.MetricSample.source_scope,
                orm.MetricSample.device_id,
                orm.MetricSample.unit,
            )
            .limit(257)
        )
        rows = list(self.db.execute(statement).mappings())
        truncated = len(rows) > 256
        rows = rows[:256]
        output = []
        for row in rows:
            first = row["first_observed_at"]
            last = row["last_observed_at"]
            if isinstance(first, str):
                first = datetime.fromisoformat(first.replace("Z", "+00:00"))
            if isinstance(last, str):
                last = datetime.fromisoformat(last.replace("Z", "+00:00"))
            output.append({
                "metric": row["metric"],
                "unit": row["unit"],
                "source": row["source"],
                "source_scope": row["source_scope"] or "unknown",
                "device_id": row["device_id"] or None,
                "sample_count": int(row["sample_count"] or 0),
                "observed_minutes": int(row["observed_minutes"] or 0),
                "first_observed_at": first,
                "last_observed_at": last,
                "minimum": float(row["minimum"]) if row["minimum"] is not None else None,
                "maximum": float(row["maximum"]) if row["maximum"] is not None else None,
                "average": float(row["average"]) if row["average"] is not None else None,
                "truncated": truncated,
            })
        return output

    def save_daily_metrics(self, metrics: list[DailyMetric]) -> int:
        """Idempotently upsert sparse daily vendor metrics by source stream."""
        deduplicated: dict[
            tuple[str, str, date, str, str, str], DailyMetric
        ] = {}
        for metric in metrics:
            key = (
                metric.user_id,
                metric.source,
                metric.date,
                metric.metric,
                metric.source_scope or "unknown",
                metric.device_id or "",
            )
            deduplicated[key] = metric

        if not deduplicated:
            return 0

        rows = [
            {
                "user_id": user_id,
                "source": source,
                "date": day,
                "metric": metric_name,
                "source_scope": source_scope,
                "device_id": device_id,
                "value": metric.value,
                "unit": metric.unit,
            }
            for (
                user_id, source, day, metric_name, source_scope, device_id
            ), metric in deduplicated.items()
        ]
        changed_users: set[str] = set()
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.DailyMetric).values(
                    rows[offset:offset + 500]
                )
                result = self.db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            "user_id", "source", "date", "metric", "source_scope",
                            "device_id",
                        ],
                        set_={
                            "value": statement.excluded.value,
                            "unit": statement.excluded.unit,
                        },
                        where=(
                            orm.DailyMetric.value.is_distinct_from(statement.excluded.value)
                            | orm.DailyMetric.unit.is_distinct_from(statement.excluded.unit)
                        ),
                    ).returning(orm.DailyMetric.user_id)
                )
                changed_users.update(row[0] for row in result.fetchall())
            self.db.flush()
            self._bump_existing_input_revisions(changed_users)
            return len(rows)

        for values in rows:
            row = self.db.execute(select(orm.DailyMetric).where(
                orm.DailyMetric.user_id == values["user_id"],
                orm.DailyMetric.source == values["source"],
                orm.DailyMetric.date == values["date"],
                orm.DailyMetric.metric == values["metric"],
                orm.DailyMetric.source_scope == values["source_scope"],
                orm.DailyMetric.device_id == values["device_id"],
            )).scalar_one_or_none()
            changed = row is None or row.value != values["value"] or row.unit != values["unit"]
            if row is None:
                self.db.add(orm.DailyMetric(**values))
            else:
                row.value = values["value"]
                row.unit = values["unit"]
            if changed:
                changed_users.add(values["user_id"])
        self.db.flush()
        self._bump_existing_input_revisions(changed_users)
        return len(rows)

    def daily_metrics(self, user_id: str, start: date, end: date, metric: str | None = None) -> list[orm.DailyMetric]:
        stmt = select(orm.DailyMetric).where(
            orm.DailyMetric.user_id == user_id,
            orm.DailyMetric.date.between(start, end),
        )
        if metric:
            stmt = stmt.where(orm.DailyMetric.metric == metric)
        return list(self.db.execute(stmt.order_by(orm.DailyMetric.date, orm.DailyMetric.metric)).scalars().all())

    # ---- 密集数据文件索引 ----

    def save_dense_data_files(self, files: list[DenseDataFile]) -> int:
        """Idempotently persist dense-file indexes and their decode status."""
        deduplicated = {
            (
                item.user_id,
                item.source,
                item.stream,
                item.file_id,
                (
                    _naive_utc(item.start_utc).isoformat()
                    if item.start_utc
                    else f"date:{item.date.isoformat()}" if item.date else "missing"
                ),
                item.device_id or "",
            ): item
            for item in files
            if item.file_id
        }
        if not deduplicated:
            return 0

        dialect = self.db.get_bind().dialect.name
        rows = [
            {
                "user_id": user_id,
                "source": source,
                "stream": stream,
                "file_id": file_id,
                "file_type": item.file_type,
                "date": item.date,
                "start_utc": _naive_utc(item.start_utc) if item.start_utc else None,
                "start_identity": start_identity,
                "end_utc": _naive_utc(item.end_utc) if item.end_utc else None,
                "source_scope": item.source_scope,
                "device_id": item.device_id or "",
                "parse_status": item.parse_status,
                "sample_count": item.sample_count,
            }
            for (user_id, source, stream, file_id, start_identity, device_id), item in deduplicated.items()
        ]
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            changed_users: set[str] = set()
            for offset in range(0, len(rows), 500):
                statement = dialect_insert(orm.DenseDataFile).values(rows[offset:offset + 500])
                changed = or_(
                    *[
                        getattr(orm.DenseDataFile, key).is_distinct_from(
                            getattr(statement.excluded, key)
                        )
                        for key in (
                            "file_type", "date", "start_utc", "end_utc", "source_scope",
                            "device_id", "parse_status", "sample_count",
                        )
                    ]
                )
                result = self.db.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            "user_id", "source", "stream", "file_id", "start_identity", "device_id"
                        ],
                        set_={
                            "file_type": statement.excluded.file_type,
                            "date": statement.excluded.date,
                            "start_utc": statement.excluded.start_utc,
                            "end_utc": statement.excluded.end_utc,
                            "source_scope": statement.excluded.source_scope,
                            "device_id": statement.excluded.device_id,
                            "parse_status": statement.excluded.parse_status,
                            "sample_count": statement.excluded.sample_count,
                        },
                        where=changed,
                    ).returning(orm.DenseDataFile.user_id)
                )
                changed_users.update(row[0] for row in result.fetchall())
            self.db.flush()
            self._bump_existing_input_revisions(changed_users)
            return len(rows)

        changed_users: set[str] = set()
        for values in rows:
            row = self.db.execute(select(orm.DenseDataFile).where(
                orm.DenseDataFile.user_id == values["user_id"],
                orm.DenseDataFile.source == values["source"],
                orm.DenseDataFile.stream == values["stream"],
                orm.DenseDataFile.file_id == values["file_id"],
                orm.DenseDataFile.start_identity == values["start_identity"],
                orm.DenseDataFile.device_id == values["device_id"],
            )).scalar_one_or_none()
            changed = row is None or any(
                getattr(row, key) != value for key, value in values.items()
            )
            if row is None:
                row = orm.DenseDataFile(**values)
                self.db.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            if changed:
                changed_users.add(values["user_id"])
        self.db.flush()
        self._bump_existing_input_revisions(changed_users)
        return len(rows)

    def dense_data_files(
        self, user_id: str, stream: str, start: date, end: date, limit: int = 5000
    ) -> list[orm.DenseDataFile]:
        return list(self.db.execute(
            select(orm.DenseDataFile).where(
                orm.DenseDataFile.user_id == user_id,
                orm.DenseDataFile.stream == stream,
                orm.DenseDataFile.date.between(start, end),
            ).order_by(orm.DenseDataFile.start_utc, orm.DenseDataFile.id).limit(limit)
        ).scalars().all())

    def dense_data_file_group(
        self,
        user_id: str,
        stream: str,
        file_id: str,
        source: str | None = None,
    ) -> list[orm.DenseDataFile]:
        statement = select(orm.DenseDataFile).where(
            orm.DenseDataFile.user_id == user_id,
            orm.DenseDataFile.stream == stream,
            orm.DenseDataFile.file_id == file_id,
        )
        if source is not None:
            statement = statement.where(orm.DenseDataFile.source == source)
        return list(self.db.execute(
            statement.order_by(
                orm.DenseDataFile.start_utc, orm.DenseDataFile.id
            )
        ).scalars().all())

    # ---- 单次运动 ----

    def save_workout(self, workout: Workout) -> set[date]:
        """Upsert a canonical workout and return its affected local days."""
        existing = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == workout.user_id,
                orm.Workout.source == workout.source,
                orm.Workout.workout_id == workout.workout_id,
            )
        ).scalar_one_or_none()
        affected = {
            local_day(existing.started_at)
            for existing in (existing,)
            if existing is not None and existing.started_at is not None
        }
        started_at = _naive_utc(workout.started_at) if workout.started_at else None
        if workout.started_at is not None:
            affected.add(local_day(workout.started_at))
        values = {
            "user_id": workout.user_id,
            "source": workout.source,
            "workout_id": workout.workout_id,
            "started_at": started_at,
            "vendor_source": workout.vendor_source,
            "data": workout.model_dump(mode="json", exclude_none=True),
        }

        changed = False
        dialect = self.db.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(orm.Workout).values(**values)
            changed = bool(self.db.execute(
                statement.on_conflict_do_update(
                    index_elements=["user_id", "source", "workout_id"],
                    set_={
                        "started_at": statement.excluded.started_at,
                        "vendor_source": statement.excluded.vendor_source,
                        "data": statement.excluded.data,
                    },
                    where=or_(
                        orm.Workout.started_at.is_distinct_from(statement.excluded.started_at),
                        orm.Workout.vendor_source.is_distinct_from(statement.excluded.vendor_source),
                        cast(orm.Workout.data, Text) != cast(statement.excluded.data, Text),
                    ),
                ).returning(orm.Workout.user_id)
            ).fetchall())
        elif existing is None:
            self.db.add(orm.Workout(**values))
            changed = True
        else:
            changed = (
                existing.started_at != started_at
                or existing.vendor_source != workout.vendor_source
                or existing.data != values["data"]
            )
            existing.started_at = started_at
            existing.vendor_source = workout.vendor_source
            existing.data = values["data"]
        self.db.flush()
        if changed:
            self._bump_existing_input_revisions({workout.user_id})
        return affected

    def rebuild_training_days(self, user_id: str, days: set[date]) -> int:
        """Rebuild derived training rows from canonical workouts."""
        written = 0
        revision_changed = False
        for day in sorted(days):
            start_at, _ = local_day_utc_bounds(day)
            _, end_at = local_day_utc_bounds(day)
            rows = list(self.db.execute(select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= _naive_utc(start_at),
                orm.Workout.started_at < _naive_utc(end_at),
            )).scalars().all())
            if not rows:
                result = self.db.execute(delete(orm.TrainingRecord).where(
                    orm.TrainingRecord.user_id == user_id,
                    orm.TrainingRecord.date == day,
                ))
                if result.rowcount:
                    written += 1
                    revision_changed = True
                continue
            training = TrainingRecord(
                user_id=user_id,
                source="canonical_workouts",
                date=day,
                workout_count=len(rows),
                total_duration=sum(int((row.data or {}).get("duration") or 0) for row in rows),
                total_load=sum(int((row.data or {}).get("load") or 0) for row in rows),
            )
            if self._upsert(
                orm.TrainingRecord,
                user_id,
                day,
                training.model_dump(mode="json", exclude_none=True),
            ):
                revision_changed = True
            written += 1
        self.db.flush()
        if revision_changed:
            self._bump_existing_input_revisions({user_id})
        return written

    def pending_workout_details(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        limit: int = 100,
        source: str = "zepp",
        *,
        refresh_after: datetime | None = None,
        strength_only: bool = False,
        exclude_workout_ids: set[str] | None = None,
    ) -> list[orm.Workout]:
        """Return bounded detail candidates, preserving backlog progress.

        Unsynced/old-schema rows are always preferred.  ``refresh_after`` allows
        scheduled runs to re-fetch recent strength sessions even when their
        current detail already has the current schema; the timestamp lives in the JSON
        detail metadata so no schema migration is needed.
        """
        budget = max(0, int(limit))
        if budget == 0:
            return []
        conditions = [
            orm.Workout.user_id == user_id,
            orm.Workout.source == source,
            orm.Workout.started_at >= _naive_utc(start),
            orm.Workout.started_at < _naive_utc(end),
            orm.Workout.vendor_source.is_not(None),
            orm.Workout.vendor_source != "",
            orm.Workout.workout_id != "",
        ]
        excluded = set(exclude_workout_ids or set())
        if excluded:
            conditions.append(orm.Workout.workout_id.not_in(excluded))
        if strength_only:
            training_family = orm.Workout.data["training_family"].as_string()
            workout_type = orm.Workout.data["type"].as_string()
            conditions.append(or_(
                training_family == "strength",
                func.lower(workout_type) == "strength",
            ))

        schema_version = orm.Workout.detail["schema_version"].as_string()
        fetched_at = orm.Workout.detail["fetched_at"].as_string()
        backlog = or_(
            orm.Workout.detail_synced.is_(False),
            orm.Workout.detail_synced.is_(None),
            orm.Workout.detail.is_(None),
            schema_version.is_(None),
            schema_version != WORKOUT_DETAIL_SCHEMA_VERSION,
        )
        order = (orm.Workout.started_at.desc(), orm.Workout.id.desc())
        backlog_rows = list(self.db.execute(
            select(orm.Workout).where(*conditions, backlog).order_by(*order).limit(budget)
        ).scalars().all())
        rows = list(backlog_rows)

        # A refresh is a second bounded query.  It only considers current
        # schema details with an old/missing fetched_at; fresh current details are
        # never returned merely because the caller requested a batch.
        remaining = budget - len(rows)
        if refresh_after is not None and remaining > 0:
            cutoff = _naive_utc(refresh_after)
            cutoff_iso = cutoff.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            if self.db.get_bind().dialect.name == "sqlite":
                fetched_before = func.julianday(fetched_at) < func.julianday(cutoff_iso)
            else:
                fetched_before = cast(fetched_at, DateTime) < cutoff
            refresh_rows = list(self.db.execute(
                select(orm.Workout).where(
                    *conditions,
                    orm.Workout.detail_synced.is_(True),
                    schema_version == WORKOUT_DETAIL_SCHEMA_VERSION,
                    or_(fetched_at.is_(None), fetched_before),
                ).order_by(fetched_at.asc().nulls_first(), *order).limit(remaining)
            ).scalars().all())
            known_ids = {row.id for row in rows}
            rows.extend(row for row in refresh_rows if row.id not in known_ids)

        rows.sort(key=lambda row: (
            not (
                not row.detail_synced
                or not isinstance(row.detail, dict)
                or row.detail.get("schema_version") != WORKOUT_DETAIL_SCHEMA_VERSION
            ),
            -(row.started_at.timestamp() if row.started_at else 0),
            -row.id,
        ))
        return rows[:budget]

    def save_workout_detail(
        self,
        user_id: str,
        workout_id: str,
        detail: dict,
        samples: list[WorkoutMetricSample] | None = None,
        *,
        source: str = "zepp",
        fetched_at: datetime | None = None,
    ) -> bool:
        row = self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        payload = dict(detail or {})
        fetched = fetched_at or datetime.now(timezone.utc)
        fetched = fetched if fetched.tzinfo else fetched.replace(tzinfo=timezone.utc)
        payload["fetched_at"] = fetched.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        previous_detail = dict(row.detail or {}) if isinstance(row.detail, dict) else {}
        previous_detail.pop("fetched_at", None)
        comparable_detail = dict(payload)
        comparable_detail.pop("fetched_at", None)
        detail_semantic_changed = (
            previous_detail != comparable_detail or not bool(row.detail_synced)
        )
        normalized_samples = None
        samples_changed = False
        if samples is not None:
            normalized_samples = sorted(
                (
                    _naive_utc(sample.timestamp),
                    sample.metric,
                    sample.value,
                    sample.unit,
                    sample.source_scope,
                    sample.device_id or "",
                    sample.sample_ordinal if sample.sample_ordinal is not None else 0,
                )
                for sample in samples
            )
            existing_samples = self.db.execute(select(orm.WorkoutMetricSample).where(
                orm.WorkoutMetricSample.user_id == user_id,
                orm.WorkoutMetricSample.source == source,
                orm.WorkoutMetricSample.workout_id == workout_id,
            )).scalars().all()
            previous_samples = sorted(
                (
                    item.timestamp,
                    item.metric,
                    item.value,
                    item.unit,
                    item.source_scope,
                    item.device_id or "",
                    item.sample_ordinal,
                )
                for item in existing_samples
            )
            samples_changed = previous_samples != normalized_samples

        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            # JSON1 text equality preserves object key order; an unchanged dict
            # must compare against the exact stored ordering, not the new payload.
            expected_detail = (
                comparable_detail if detail_semantic_changed else previous_detail
            )
            incoming_semantic = func.json_remove(
                cast(json.dumps(expected_detail, ensure_ascii=True), Text),
                "$.fetched_at",
            )
            current_semantic = func.json_remove(
                cast(orm.Workout.detail, Text), "$.fetched_at"
            )
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB
            incoming_semantic = cast(comparable_detail, JSONB)
            current_semantic = cast(orm.Workout.detail, JSONB).op("-")("fetched_at")
        else:
            incoming_semantic = None
            current_semantic = None

        key_conditions = (
            orm.Workout.user_id == user_id,
            orm.Workout.source == source,
            orm.Workout.workout_id == workout_id,
        )
        if dialect in ("sqlite", "postgresql"):
            semantic_difference = or_(
                orm.Workout.detail.is_(None),
                orm.Workout.detail_synced.is_not(True),
                current_semantic != incoming_semantic,
            )
            semantic_equal = and_(
                orm.Workout.detail.is_not(None),
                orm.Workout.detail_synced.is_(True),
                current_semantic == incoming_semantic,
            )
            condition = semantic_difference if detail_semantic_changed else semantic_equal
            result = self.db.execute(
                update(orm.Workout).where(*key_conditions, condition).values(
                    detail=payload,
                    detail_synced=True,
                ).returning(orm.Workout.id)
            )
            updated = bool(result.fetchall())
            if not updated and detail_semantic_changed:
                # Another writer may have installed the same semantic detail; only
                # refresh the timestamp after proving the current semantic payload matches.
                result = self.db.execute(
                    update(orm.Workout).where(*key_conditions, semantic_equal).values(
                        detail=payload,
                        detail_synced=True,
                    ).returning(orm.Workout.id)
                )
                updated = bool(result.fetchall())
            if not updated:
                raise RuntimeError("workout detail changed during conditional update")
            detail_semantic_changed = detail_semantic_changed and updated
        else:
            row.detail = payload
            row.detail_synced = True
            updated = True

        if samples is not None and samples_changed:
            self.db.execute(delete(orm.WorkoutMetricSample).where(
                orm.WorkoutMetricSample.user_id == user_id,
                orm.WorkoutMetricSample.source == source,
                orm.WorkoutMetricSample.workout_id == workout_id,
            ))
            self.db.add_all([
                orm.WorkoutMetricSample(
                    user_id=user_id,
                    source=source,
                    workout_id=workout_id,
                    timestamp=_naive_utc(sample.timestamp),
                    metric=sample.metric,
                    value=sample.value,
                    unit=sample.unit,
                    source_scope=sample.source_scope,
                    device_id=sample.device_id or "",
                    sample_ordinal=(
                        sample.sample_ordinal if sample.sample_ordinal is not None else 0
                    ),
                )
                for sample in samples
            ])
        self.db.flush()
        if detail_semantic_changed or samples_changed:
            self._bump_existing_input_revisions({user_id})
        return True

    def workout_metric_samples(
        self,
        user_id: str,
        workout_id: str,
        metric: str | None = None,
        limit: int = 250_000,
        source: str = "zepp",
    ) -> list[orm.WorkoutMetricSample]:
        query = select(orm.WorkoutMetricSample).where(
            orm.WorkoutMetricSample.user_id == user_id,
            orm.WorkoutMetricSample.source == source,
            orm.WorkoutMetricSample.workout_id == workout_id,
        )
        if metric:
            query = query.where(orm.WorkoutMetricSample.metric == metric)
        return list(self.db.execute(
            query.order_by(
                orm.WorkoutMetricSample.timestamp,
                orm.WorkoutMetricSample.metric,
            ).limit(limit)
        ).scalars().all())

    def workout_metric_samples_for_workouts(
        self,
        user_id: str,
        workout_ids: list[str] | list[tuple[str, str]],
        limit: int = 2_000_000,
        resolution_seconds: int = 5,
        source: str = "zepp",
    ) -> list[WorkoutAnalysisSample]:
        if not workout_ids:
            return []
        workout_keys = [
            (source, item) if isinstance(item, str) else item
            for item in workout_ids
        ]
        resolution_seconds = max(int(resolution_seconds), 1)
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            epoch = cast(func.strftime("%s", orm.WorkoutMetricSample.timestamp), Integer)
        elif dialect == "postgresql":
            epoch = cast(func.extract("epoch", orm.WorkoutMetricSample.timestamp), Integer)
        else:
            rows = self.workout_metric_samples_for_workouts_raw(
                user_id, workout_keys, limit
            )
            return [
                WorkoutAnalysisSample(
                    source=row.source,
                    workout_id=row.workout_id,
                    timestamp=row.timestamp,
                    metric=row.metric,
                    value=row.value,
                    unit=row.unit,
                    source_scope=row.source_scope,
                    device_id=row.device_id or None,
                )
                for row in rows
            ]
        bucket = cast(epoch / resolution_seconds, Integer)
        value = case(
            (orm.WorkoutMetricSample.metric == "distance", func.max(orm.WorkoutMetricSample.value)),
            else_=func.avg(orm.WorkoutMetricSample.value),
        ).label("value")
        statement = (
            select(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                func.min(orm.WorkoutMetricSample.timestamp).label("timestamp"),
                orm.WorkoutMetricSample.metric,
                value,
                orm.WorkoutMetricSample.unit,
                orm.WorkoutMetricSample.source_scope,
                orm.WorkoutMetricSample.device_id,
            )
            .where(
                orm.WorkoutMetricSample.user_id == user_id,
                tuple_(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                ).in_(workout_keys),
            )
            .group_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                orm.WorkoutMetricSample.metric,
                orm.WorkoutMetricSample.unit,
                orm.WorkoutMetricSample.source_scope,
                orm.WorkoutMetricSample.device_id,
                bucket,
            )
            .order_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                func.min(orm.WorkoutMetricSample.timestamp),
                orm.WorkoutMetricSample.metric,
            )
            .limit(limit)
        )
        return [
            WorkoutAnalysisSample(
                source=row.source,
                workout_id=row.workout_id,
                timestamp=row.timestamp,
                metric=row.metric,
                value=float(row.value),
                unit=row.unit,
                source_scope=row.source_scope,
                device_id=row.device_id or None,
            )
            for row in self.db.execute(statement)
        ]

    def workout_metric_samples_for_workouts_raw(
        self,
        user_id: str,
        workout_ids: list[str] | list[tuple[str, str]],
        limit: int = 2_000_000,
        source: str = "zepp",
    ) -> list[orm.WorkoutMetricSample]:
        if not workout_ids:
            return []
        workout_keys = [
            (source, item) if isinstance(item, str) else item
            for item in workout_ids
        ]
        return list(self.db.execute(
            select(orm.WorkoutMetricSample).where(
                orm.WorkoutMetricSample.user_id == user_id,
                tuple_(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                ).in_(workout_keys),
            ).order_by(
                orm.WorkoutMetricSample.source,
                orm.WorkoutMetricSample.workout_id,
                orm.WorkoutMetricSample.timestamp,
                orm.WorkoutMetricSample.metric,
            ).limit(limit)
        ).scalars().all())

    def workouts(
        self,
        user_id: str,
        start: date,
        end: date,
        limit: int = 500,
        *,
        timezone_name: str | None = None,
    ) -> list[orm.Workout]:
        start_at, _ = local_day_utc_bounds(start, timezone_name)
        _, end_at = local_day_utc_bounds(end, timezone_name)
        return list(self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.started_at >= _naive_utc(start_at),
                orm.Workout.started_at < _naive_utc(end_at),
            ).order_by(orm.Workout.started_at.desc()).limit(limit)
        ).scalars().all())

    def open_health_load_inputs(
        self,
        user_id: str,
        start: date,
        end: date,
        *,
        metric: str = "heart_rate",
        source: str | None = None,
        limit: int = 200,
        timezone_name: str | None = None,
    ):
        """Return source-qualified workouts with only the requested metric samples.

        This loader deliberately does not use the general 28-day profile loader or
        fetch any other workout metric.  ``metric`` remains explicit for query tests;
        the TRIMP algorithm always calls it with ``heart_rate``.
        """
        from vitalis.intelligence.open_health.load import (
            HeartRatePoint,
            LoadWorkout,
            LoadWorkoutBatch,
            PauseInterval,
        )

        start_at, _ = local_day_utc_bounds(start, timezone_name)
        _, end_at = local_day_utc_bounds(end, timezone_name)
        statement = select(orm.Workout).where(
            orm.Workout.user_id == user_id,
            orm.Workout.started_at >= _naive_utc(start_at),
            orm.Workout.started_at < _naive_utc(end_at),
        )
        if source is not None:
            statement = statement.where(orm.Workout.source == source)
        workouts = list(self.db.execute(
            statement.order_by(orm.Workout.started_at, orm.Workout.source, orm.Workout.workout_id).limit(limit)
        ).scalars().all())
        if not workouts:
            return []
        keys = [(row.source, row.workout_id) for row in workouts]
        max_points = 1_000_000
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            epoch = cast(func.strftime("%s", orm.WorkoutMetricSample.timestamp), Integer)
        elif dialect == "postgresql":
            epoch = cast(func.extract("epoch", orm.WorkoutMetricSample.timestamp), Integer)
        else:
            epoch = None
        if epoch is not None:
            bucket = cast(epoch / 5, Integer)
            statement = (
                select(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    func.min(orm.WorkoutMetricSample.timestamp).label("timestamp"),
                    func.avg(orm.WorkoutMetricSample.value).label("value"),
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                )
                .where(
                    orm.WorkoutMetricSample.user_id == user_id,
                    orm.WorkoutMetricSample.metric == metric,
                    tuple_(
                        orm.WorkoutMetricSample.source,
                        orm.WorkoutMetricSample.workout_id,
                    ).in_(keys),
                )
                .group_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                    bucket,
                )
                .order_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    func.min(orm.WorkoutMetricSample.timestamp),
                )
                .limit(max_points + 1)
            )
            sample_rows = list(self.db.execute(statement))
        else:
            sample_rows = list(self.db.execute(
                select(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.timestamp,
                    orm.WorkoutMetricSample.value,
                    orm.WorkoutMetricSample.unit,
                    orm.WorkoutMetricSample.source_scope,
                    orm.WorkoutMetricSample.device_id,
                ).where(
                    orm.WorkoutMetricSample.user_id == user_id,
                    orm.WorkoutMetricSample.metric == metric,
                    tuple_(
                        orm.WorkoutMetricSample.source,
                        orm.WorkoutMetricSample.workout_id,
                    ).in_(keys),
                ).order_by(
                    orm.WorkoutMetricSample.source,
                    orm.WorkoutMetricSample.workout_id,
                    orm.WorkoutMetricSample.timestamp,
                ).limit(max_points + 1)
            ))
        truncated = len(sample_rows) > max_points
        sample_rows = sample_rows[:max_points]
        samples_by_key: dict[tuple[str, str], list[HeartRatePoint]] = {}
        for row in sample_rows:
            timestamp = row.timestamp.replace(tzinfo=timezone.utc)
            samples_by_key.setdefault((row.source, row.workout_id), []).append(
                HeartRatePoint(
                    timestamp=timestamp,
                    value=float(row.value),
                    source=row.source,
                    source_scope=row.source_scope,
                    device_id=row.device_id or None,
                    unit=row.unit,
                )
            )

        def parse_datetime(value):
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return None
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return None

        output = []
        for row in workouts:
            data = row.data if isinstance(row.data, dict) else {}
            detail = row.detail if isinstance(row.detail, dict) else {}
            started = row.started_at.replace(tzinfo=timezone.utc) if row.started_at else None
            ended = parse_datetime(data.get("ended_at"))
            duration = data.get("duration")
            try:
                duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            pauses = []
            for item in (detail.get("pauses") or []):
                if not isinstance(item, dict):
                    continue
                pause_start = parse_datetime(item.get("started_at"))
                try:
                    pause_duration = float(item.get("duration_seconds") or 0)
                except (TypeError, ValueError):
                    continue
                if pause_start is not None and pause_duration > 0:
                    pauses.append(PauseInterval(pause_start, pause_duration))
            output.append(LoadWorkout(
                source=row.source,
                workout_id=row.workout_id,
                started_at=started,
                ended_at=ended,
                duration_minutes=duration,
                heart_rate=tuple(samples_by_key.get((row.source, row.workout_id), [])),
                pauses=tuple(pauses),
            ))
        return LoadWorkoutBatch(output, truncated=truncated)

    def workout(
        self, user_id: str, workout_id: str, source: str = "zepp"
    ) -> orm.Workout | None:
        return self.db.execute(
            select(orm.Workout).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
                orm.Workout.workout_id == workout_id,
            )
        ).scalar_one_or_none()

    # ---- 持久同步账本 ----

    def create_or_reuse_sync_attempt(
        self,
        user_id: str,
        source: str = "zepp",
        trigger: str = "manual",
        trigger_ref: str | None = None,
        plan_version: str = "zepp-sync-v1",
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        timezone_name: str = "UTC",
        options: dict | None = None,
        deadline_at: datetime | None = None,
        manifest: list[object] | None = None,
        attempt_id: str | None = None,
        mock_source: bool = False,
    ) -> orm.SyncAttempt:
        """Create one active attempt, or return the user's existing active attempt."""
        now = datetime.utcnow()
        if window_start is None or window_end is None:
            raise ValueError("同步尝试必须提供非空时间窗口")
        window_start = _naive_utc(window_start)
        window_end = _naive_utc(window_end)
        if window_end <= window_start:
            raise ValueError("同步尝试结束时间必须晚于开始时间")
        deadline_at = _naive_utc(deadline_at) if deadline_at else None
        options = dict(options or {})
        from vitalis.config import settings
        mock_source = bool(
            mock_source
            or options.get("mock_source") is True
            or settings.zepp_mock
        )
        # This is a transaction gate, not durable request behavior.
        options.pop("mock_source", None)
        account = self.source_account(user_id, source, for_update=True, active_only=True)
        if account is None and not mock_source:
            raise SourceIdentityConflict("数据源账号不存在或已撤销")
        if account is None:
            # Mock sources are explicit and may create a synthetic local owner.
            self.upsert_user(user_id)
        request_payload = {
            "source": source,
            "trigger": trigger,
            "trigger_ref": trigger_ref,
            "plan_version": plan_version,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "timezone": timezone_name,
            "options": options,
            "source_account_id": account.id if account is not None else None,
            "source_account_epoch": account.fence_epoch if account is not None else None,
        }
        request_key = hashlib.sha256(
            json.dumps(
                request_payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()
        api_ref = trigger == "manual" and bool(trigger_ref) and trigger_ref.startswith("api:")
        if api_ref:
            previous = self.db.execute(select(orm.SyncAttempt).where(
                orm.SyncAttempt.user_id == user_id,
                orm.SyncAttempt.source == source,
                orm.SyncAttempt.trigger == trigger,
                orm.SyncAttempt.trigger_ref == trigger_ref,
            ).with_for_update()).scalars().first()
            if previous is not None:
                if previous.request_key != request_key:
                    raise SyncIdempotencyConflict("Idempotency-Key 与此前同步请求不一致")
                return previous

        if attempt_id:
            existing = self.db.get(orm.SyncAttempt, attempt_id)
            if existing is not None:
                if existing.user_id != user_id or existing.source != source:
                    raise ValueError("同步尝试不属于当前用户或数据源")
                if (
                    account is not None
                    and existing.source_account_id != account.id
                ):
                    raise ValueError("同步尝试不属于当前数据源账号")
                self._ensure_reused_sync_chunks(existing, manifest)
                return existing

        active_statement = select(orm.SyncAttempt).where(
            orm.SyncAttempt.user_id == user_id,
            orm.SyncAttempt.source == source,
            orm.SyncAttempt.request_key == request_key,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
        )
        if account is not None:
            active_statement = active_statement.where(
                orm.SyncAttempt.source_account_id == account.id,
            )
        active = self.db.execute(
            active_statement.order_by(orm.SyncAttempt.created_at.desc()).with_for_update()
        ).scalars().first()
        if active is not None:
            self._ensure_reused_sync_chunks(active, manifest)
            return active

        values = dict(
            id=attempt_id or uuid4().hex,
            user_id=user_id,
            source=source,
            source_account_id=account.id if account is not None else None,
            source_account_epoch=(account.fence_epoch if account is not None else None),
            trigger=trigger,
            trigger_ref=trigger_ref,
            plan_version=plan_version,
            request_key=request_key,
            window_start=window_start,
            window_end=window_end,
            timezone=timezone_name,
            options=options,
            status="queued",
            deadline_at=deadline_at,
            created_at=now,
            updated_at=now,
        )
        try:
            with self.db.begin_nested():
                self.db.add(orm.SyncAttempt(**values))
                self.db.flush()
        except IntegrityError:
            if api_ref:
                previous = self.db.execute(select(orm.SyncAttempt).where(
                    orm.SyncAttempt.user_id == user_id,
                    orm.SyncAttempt.source == source,
                    orm.SyncAttempt.trigger == trigger,
                    orm.SyncAttempt.trigger_ref == trigger_ref,
                )).scalars().first()
                if previous is not None:
                    if previous.request_key != request_key:
                        raise SyncIdempotencyConflict("Idempotency-Key 与此前同步请求不一致")
                    return previous
            active = self.db.execute(
                select(orm.SyncAttempt).where(
                    orm.SyncAttempt.user_id == user_id,
                    orm.SyncAttempt.source == source,
                    orm.SyncAttempt.request_key == request_key,
                    orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                ).order_by(orm.SyncAttempt.created_at.desc())
            ).scalars().first()
            if active is None:
                raise
            self._ensure_reused_sync_chunks(active, manifest)
            return active
        attempt = self.db.get(orm.SyncAttempt, values["id"])
        assert attempt is not None
        self._ensure_sync_chunks(attempt, manifest)
        return attempt

    def create_sync_attempt(self, *args, **kwargs) -> orm.SyncAttempt:
        """Compatibility spelling for the coordinator-facing create operation."""
        return self.create_or_reuse_sync_attempt(*args, **kwargs)

    def create_or_reuse_attempt(self, *args, **kwargs) -> orm.SyncAttempt:
        return self.create_or_reuse_sync_attempt(*args, **kwargs)

    def _ensure_reused_sync_chunks(
        self, attempt: orm.SyncAttempt, manifest: list[object] | None
    ) -> None:
        # Detail-only attempts have a fixed lifetime budget; a later enqueue
        # must not append the next pending workout after the first chunk commits.
        if (attempt.options or {}).get("detail_only") is not True:
            self._ensure_sync_chunks(attempt, manifest)

    def _ensure_sync_chunks(
        self, attempt: orm.SyncAttempt, manifest: list[object] | None
    ) -> None:
        if manifest:
            values = []
            for item in manifest:
                if isinstance(item, dict):
                    get = item.get
                else:
                    get = lambda key, default=None: getattr(item, key, default)
                stable_key = get("stable_key") or get("key")
                stream = get("stream")
                if not stable_key or not stream:
                    raise ValueError("同步 chunk 必须包含 stable_key 和 stream")
                values.append({
                    "attempt_id": attempt.id,
                    "stable_key": str(stable_key),
                    "stream": str(stream),
                    "health_stream": get("health_stream"),
                    "partition": str(get("partition", "") or ""),
                    "ordinal": int(get("ordinal", 0) or 0),
                    "window_start": (
                        _naive_utc(get("window_start")) if get("window_start") else None
                    ),
                    "window_end": (
                        _naive_utc(get("window_end")) if get("window_end") else None
                    ),
                    "cursor": get("cursor"),
                    "allow_unavailable": bool(get("allow_unavailable", False)),
                    "status": "queued",
                    "fetch_status": get("fetch_status", "never"),
                    "parse_status": get("parse_status", "never"),
                    "write_status": get("write_status", "never"),
                    "stages": dict(get("stages", {}) or {}),
                })
            dialect = self.db.get_bind().dialect.name
            if dialect in ("sqlite", "postgresql"):
                if dialect == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert
                else:
                    from sqlalchemy.dialects.postgresql import insert
                statement = insert(orm.SyncChunk).values(values)
                self.db.execute(statement.on_conflict_do_nothing(
                    index_elements=["attempt_id", "stable_key"]
                ))
            else:
                for value in values:
                    exists = self.db.execute(select(orm.SyncChunk).where(
                        orm.SyncChunk.attempt_id == attempt.id,
                        orm.SyncChunk.stable_key == value["stable_key"],
                    )).scalar_one_or_none()
                    if exists is None:
                        self.db.add(orm.SyncChunk(**value))
            self.db.flush()
        attempt.chunk_count = self.db.execute(
            select(func.count(orm.SyncChunk.id)).where(orm.SyncChunk.attempt_id == attempt.id)
        ).scalar_one()
        attempt.updated_at = datetime.utcnow()
        self.db.flush()

    def sync_attempt(self, attempt_id: str, user_id: str | None = None) -> orm.SyncAttempt | None:
        row = self.db.get(orm.SyncAttempt, attempt_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    def get_sync_attempt(self, attempt_id: str, user_id: str | None = None) -> orm.SyncAttempt | None:
        return self.sync_attempt(attempt_id, user_id=user_id)

    def sync_attempts(
        self, user_id: str, source: str | None = None, statuses: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[orm.SyncAttempt]:
        statement = select(orm.SyncAttempt).where(orm.SyncAttempt.user_id == user_id)
        if source is not None:
            statement = statement.where(orm.SyncAttempt.source == source)
        if statuses:
            statement = statement.where(orm.SyncAttempt.status.in_(statuses))
        return list(self.db.execute(
            statement.order_by(orm.SyncAttempt.created_at.desc()).limit(limit)
        ).scalars().all())

    def sync_chunks(
        self, attempt_id: str, user_id: str | None = None, status: str | None = None
    ) -> list[orm.SyncChunk]:
        statement = select(orm.SyncChunk).where(orm.SyncChunk.attempt_id == attempt_id)
        if user_id is not None:
            statement = statement.join(
                orm.SyncAttempt, orm.SyncAttempt.id == orm.SyncChunk.attempt_id
            ).where(orm.SyncAttempt.user_id == user_id)
        if status is not None:
            statement = statement.where(orm.SyncChunk.status == status)
        return list(self.db.execute(
            statement.order_by(orm.SyncChunk.ordinal, orm.SyncChunk.stable_key)
        ).scalars().all())

    def get_sync_chunks(
        self, attempt_id: str, user_id: str | None = None, status: str | None = None
    ) -> list[orm.SyncChunk]:
        return self.sync_chunks(attempt_id, user_id=user_id, status=status)

    def training_history_coverage(
        self,
        user_id: str,
        start: date,
        end: date,
        as_of: datetime,
        *,
        timezone_name: str | None = None,
    ) -> dict:
        """Report conservative, attempt-proven workout-history coverage.

        Reads use stable keyset batches with explicit total budgets.  A budget
        exhaustion is visible to callers and can never be reported complete.
        Workout chunks for many attempts are fetched in grouped batches so the
        proof check does not turn into one query per attempt.
        """
        if end < start:
            raise ValueError("训练历史覆盖窗口无效")
        as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        as_of_utc = as_of_utc.astimezone(timezone.utc)
        as_of_naive = _naive_utc(as_of_utc)
        period_start = start.isoformat()
        period_end = end.isoformat()
        target_days = {
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
        }
        limitations: list[str] = []
        period_start_at, _ = local_day_utc_bounds(start, timezone_name)
        _, period_end_at = local_day_utc_bounds(end, timezone_name)
        period_start_utc = _naive_utc(period_start_at)
        period_end_utc = _naive_utc(period_end_at)
        from vitalis.adapters.zepp.client import (
            LEGACY_SPORTS, SPORTS, WORKOUT_AGGREGATE_PLAN_VERSION,
        )

        MAX_ATTEMPTS = 256
        MAX_CHUNKS = 16_384
        ATTEMPT_BATCH = 64
        CHUNK_BATCH = 512
        attempts = []
        attempt_cursor = None
        budget_exhausted = False
        while True:
            remaining = MAX_ATTEMPTS - len(attempts)
            limit = ATTEMPT_BATCH if remaining > ATTEMPT_BATCH else remaining + 1
            conditions = [
                orm.SyncAttempt.user_id == user_id,
                orm.SyncAttempt.source == "zepp",
                orm.SyncAttempt.status.in_(("succeeded", "partial", "failed")),
                orm.SyncAttempt.window_start < period_end_utc,
                orm.SyncAttempt.window_end > period_start_utc,
                orm.SyncAttempt.finished_at <= as_of_naive,
                exists().where(
                    orm.SyncChunk.attempt_id == orm.SyncAttempt.id,
                    orm.SyncChunk.stream == "workouts",
                ),
            ]
            if attempt_cursor is not None:
                finished_at, created_at, attempt_id = attempt_cursor
                conditions.append(or_(
                    orm.SyncAttempt.finished_at < finished_at,
                    (orm.SyncAttempt.finished_at == finished_at)
                    & (orm.SyncAttempt.created_at < created_at),
                    (orm.SyncAttempt.finished_at == finished_at)
                    & (orm.SyncAttempt.created_at == created_at)
                    & (orm.SyncAttempt.id < attempt_id),
                ))
            batch = list(self.db.execute(
                select(orm.SyncAttempt).where(*conditions).order_by(
                    orm.SyncAttempt.finished_at.desc(),
                    orm.SyncAttempt.created_at.desc(),
                    orm.SyncAttempt.id.desc(),
                ).limit(limit)
            ).scalars().all())
            if not batch:
                break
            if len(batch) > remaining:
                budget_exhausted = True
                batch = batch[:remaining]
            attempts.extend(batch)
            if budget_exhausted or not batch:
                break
            last = batch[-1]
            attempt_cursor = (last.finished_at, last.created_at, last.id)

        attempt_ids = [attempt.id for attempt in attempts]
        chunks_by_attempt: dict[str, list[orm.SyncChunk]] = defaultdict(list)
        chunk_cursor = None
        chunks_read = 0
        while attempt_ids:
            remaining = MAX_CHUNKS - chunks_read
            limit = CHUNK_BATCH if remaining > CHUNK_BATCH else remaining + 1
            conditions = [
                orm.SyncChunk.attempt_id.in_(attempt_ids),
                orm.SyncChunk.stream == "workouts",
            ]
            if chunk_cursor is not None:
                attempt_id, ordinal, chunk_id = chunk_cursor
                conditions.append(or_(
                    orm.SyncChunk.attempt_id > attempt_id,
                    (orm.SyncChunk.attempt_id == attempt_id)
                    & (orm.SyncChunk.ordinal > ordinal),
                    (orm.SyncChunk.attempt_id == attempt_id)
                    & (orm.SyncChunk.ordinal == ordinal)
                    & (orm.SyncChunk.id > chunk_id),
                ))
            batch = list(self.db.execute(
                select(orm.SyncChunk).where(*conditions).order_by(
                    orm.SyncChunk.attempt_id,
                    orm.SyncChunk.ordinal,
                    orm.SyncChunk.id,
                ).limit(limit)
            ).scalars().all())
            if not batch:
                break
            if len(batch) > remaining:
                budget_exhausted = True
                batch = batch[:remaining]
            for chunk in batch:
                chunks_by_attempt[chunk.attempt_id].append(chunk)
            chunks_read += len(batch)
            if budget_exhausted or not batch:
                break
            last = batch[-1]
            chunk_cursor = (last.attempt_id, last.ordinal, last.id)

        verified_days: set[date] = set()
        saw_relevant_attempt = False
        saw_partial_evidence = False
        saw_intraday_fetch = False
        last_synced_at: datetime | None = None
        as_of_local_day = local_day(as_of_utc, timezone_name)

        def verified_chunk(chunk: orm.SyncChunk) -> bool:
            return (
                chunk.status == "succeeded"
                and chunk.fetch_status == "success"
                and (chunk.parse_status, chunk.write_status) in {
                    ("success", "success"), ("empty", "not_run"),
                }
            )

        for attempt in attempts:
            attempt_chunks = chunks_by_attempt.get(attempt.id, [])
            if not attempt_chunks:
                continue
            saw_relevant_attempt = True
            required_sports = (
                SPORTS if attempt.plan_version == WORKOUT_AGGREGATE_PLAN_VERSION
                else LEGACY_SPORTS
            )
            sport_seen = {sport: False for sport in required_sports}
            sport_complete = {sport: True for sport in required_sports}
            saw_verified = False
            has_unfinished = False
            for chunk in attempt_chunks:
                if chunk.finished_at is None or chunk.finished_at > as_of_naive:
                    has_unfinished = True
                if chunk.partition in sport_seen:
                    sport_seen[chunk.partition] = True
                    if not verified_chunk(chunk):
                        sport_complete[chunk.partition] = False
                    else:
                        saw_verified = True
            if has_unfinished:
                limitations.append("attempt 存在未完成或晚于 as_of 的 workouts 分页 chunk")
                saw_partial_evidence = True
                continue
            if any(not sport_seen[sport] or not sport_complete[sport] for sport in required_sports):
                if saw_verified:
                    saw_partial_evidence = True
                continue
            window_start = max(attempt.window_start, period_start_utc)
            window_end = min(attempt.window_end, period_end_utc)
            if window_start >= window_end:
                continue
            confirmed_for_attempt = False
            for day in target_days:
                if day > as_of_local_day:
                    continue
                day_start, day_end = local_day_utc_bounds(day, timezone_name)
                if day == as_of_local_day and as_of_utc < day_end:
                    continue
                day_start_naive = _naive_utc(day_start)
                day_end_naive = _naive_utc(day_end)
                covering_chunks = [
                    chunk for chunk in attempt_chunks
                    if chunk.partition in sport_seen
                    and (chunk.window_start or attempt.window_start) <= day_start_naive
                    and (chunk.window_end or attempt.window_end) >= day_end_naive
                ]
                if {
                    chunk.partition for chunk in covering_chunks
                } != set(required_sports):
                    continue
                first_fetch_started = min(
                    chunk.started_at or attempt.created_at
                    for chunk in covering_chunks
                )
                if first_fetch_started < day_end_naive:
                    saw_intraday_fetch = True
                else:
                    verified_days.add(day)
                    confirmed_for_attempt = True
            if confirmed_for_attempt:
                finished = max(
                    (
                        chunk.finished_at for chunk in attempt_chunks
                        if chunk.finished_at is not None
                        and chunk.finished_at <= as_of_naive
                    ),
                    default=None,
                )
                if finished is not None:
                    candidate = finished.replace(tzinfo=timezone.utc)
                    if last_synced_at is None or candidate > last_synced_at:
                        last_synced_at = candidate

        if not attempts:
            limitations.append("没有截至 as_of 的 Zepp 同步 attempt 证据")
        elif not saw_relevant_attempt:
            limitations.append("匹配的 attempt 没有 workouts 分区记录")
        if budget_exhausted:
            limitations.append("训练覆盖查询达到总预算，未确认更早 attempt 或分页 chunk")
        if saw_relevant_attempt and len(verified_days) < len(target_days):
            limitations.append("至少一个日期缺少完整的运动来源或分页核验")
        if saw_intraday_fetch and len(verified_days) < len(target_days):
            limitations.append("有运动记录在当天结束前同步，不能证实当天已查全")
        if as_of_utc.date() < end:
            limitations.append("窗口末端晚于 as_of，未来日期不计入覆盖")
        elif as_of_utc.date() == end:
            _, target_end = local_day_utc_bounds(end, timezone_name)
            if as_of_utc < target_end:
                limitations.append("窗口末端当前本地日尚未结束，不计为完整覆盖")
        if budget_exhausted:
            status = "PARTIAL" if verified_days else "UNKNOWN"
        elif not verified_days:
            status = "PARTIAL" if saw_partial_evidence else "UNKNOWN"
        elif verified_days == target_days:
            status = "COMPLETE"
        else:
            status = "PARTIAL"
        return {
            "status": status,
            "period_start": period_start,
            "period_end": period_end,
            "verified_days": [day.isoformat() for day in sorted(verified_days)],
            "last_synced_at": (
                last_synced_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if last_synced_at else None
            ),
            "truncated": budget_exhausted,
            "budget_exhausted": budget_exhausted,
            "limitations": limitations,
        }

    def sync_chunk(
        self, attempt_id: str, stable_key: str, user_id: str | None = None
    ) -> orm.SyncChunk | None:
        return self.db.execute(
            select(orm.SyncChunk).join(
                orm.SyncAttempt, orm.SyncAttempt.id == orm.SyncChunk.attempt_id
            ).where(
                orm.SyncChunk.attempt_id == attempt_id,
                orm.SyncChunk.stable_key == stable_key,
                *([orm.SyncAttempt.user_id == user_id] if user_id is not None else []),
            )
        ).scalar_one_or_none()

    def claim_sync_attempt(
        self, attempt_id: str, lease_token: str, *, now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        other = orm.SyncAttempt.__table__.alias("other_sync_attempt")
        no_other_running = ~select(other.c.id).where(
            other.c.user_id == orm.SyncAttempt.user_id,
            other.c.source == orm.SyncAttempt.source,
            other.c.status == "running",
            other.c.id != orm.SyncAttempt.id,
        ).exists()
        try:
            with self.db.begin_nested():
                result = self.db.execute(update(orm.SyncAttempt).where(
                    orm.SyncAttempt.id == attempt_id,
                    no_other_running,
                    orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                    orm.SyncAttempt.cancel_requested_at.is_(None),
                    or_(
                        orm.SyncAttempt.source_account_id.is_(None),
                        exists().where(
                            orm.SourceAccount.id == orm.SyncAttempt.source_account_id,
                            orm.SourceAccount.status == "active",
                            orm.SourceAccount.fence_epoch == orm.SyncAttempt.source_account_epoch,
                        ),
                    ),
                    or_(orm.SyncAttempt.next_retry_at.is_(None), orm.SyncAttempt.next_retry_at <= now),
                    or_(orm.SyncAttempt.lease_expires_at.is_(None), orm.SyncAttempt.lease_expires_at <= now),
                ).values(
                    status="running",
                    lease_token=lease_token,
                    lease_epoch=orm.SyncAttempt.lease_epoch + 1,
                    attempt_count=orm.SyncAttempt.attempt_count + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=func.coalesce(orm.SyncAttempt.started_at, now),
                    next_retry_at=None,
                    updated_at=now,
                ))
                self.db.flush()
        except IntegrityError:
            # The partial unique running-attempt index is the final cross-worker fence.
            return False
        return bool(result.rowcount)

    def claim_attempt(self, *args, **kwargs) -> bool:
        return self.claim_sync_attempt(*args, **kwargs)

    def takeover_expired_attempt(self, *args, **kwargs) -> bool:
        return self.claim_sync_attempt(*args, **kwargs)

    def acquire_sync_attempt_lease(
        self, attempt_id: str, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> SyncLease | None:
        token = uuid4().hex
        if not self.claim_sync_attempt(
            attempt_id, token, now=now, lease_seconds=lease_seconds
        ):
            return None
        row = self.sync_attempt(attempt_id)
        assert row is not None
        from vitalis.application.sync_types import SyncLease
        return SyncLease(row.id, token, row.lease_epoch, row.lease_expires_at)

    def claim_sync_chunk(
        self, chunk_id: int, lease_token: str, *, now: datetime | None = None,
        lease_seconds: int = 60, attempt_lease_token: str | None = None,
        attempt_lease_epoch: int | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        parent = self.db.execute(select(
            orm.SyncAttempt.lease_token, orm.SyncAttempt.lease_epoch,
            orm.SyncChunk.stages,
        ).join(
            orm.SyncChunk, orm.SyncChunk.attempt_id == orm.SyncAttempt.id
        ).where(orm.SyncChunk.id == chunk_id).with_for_update()).one_or_none()
        if parent is None or parent.lease_token is None:
            return False
        if (
            attempt_lease_token is not None
            and (parent.lease_token != attempt_lease_token
                 or parent.lease_epoch != attempt_lease_epoch)
        ):
            return False
        parent_fence = _sync_parent_fence(parent.lease_token, parent.lease_epoch)
        active_attempt = select(orm.SyncAttempt.id).where(
            orm.SyncAttempt.id == orm.SyncChunk.attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.cancel_requested_at.is_(None),
            orm.SyncAttempt.lease_token == parent.lease_token,
            orm.SyncAttempt.lease_epoch == parent.lease_epoch,
            orm.SyncAttempt.lease_expires_at > now,
            exists(select(orm.User.id).where(orm.User.id == orm.SyncAttempt.user_id)),
            or_(
                orm.SyncAttempt.source_account_id.is_(None),
                exists(select(orm.SourceAccount.id).where(
                    orm.SourceAccount.id == orm.SyncAttempt.source_account_id,
                    orm.SourceAccount.status == "active",
                    orm.SourceAccount.fence_epoch == orm.SyncAttempt.source_account_epoch,
                )),
            ),
        )
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.attempt_id.in_(active_attempt),
            orm.SyncChunk.status.in_(("queued", "running", "retry_wait")),
            or_(orm.SyncChunk.next_retry_at.is_(None), orm.SyncChunk.next_retry_at <= now),
            or_(orm.SyncChunk.lease_expires_at.is_(None), orm.SyncChunk.lease_expires_at <= now),
        ).values(
            status="running",
            lease_token=lease_token,
            lease_epoch=orm.SyncChunk.lease_epoch + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=func.coalesce(orm.SyncChunk.started_at, now),
            next_retry_at=None,
            attempt_count=orm.SyncChunk.attempt_count + 1,
            stages={**(parent.stages or {}), "_attempt_lease_fence": parent_fence},
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def claim_chunk(self, *args, **kwargs) -> bool:
        return self.claim_sync_chunk(*args, **kwargs)

    def takeover_expired_chunk(self, *args, **kwargs) -> bool:
        return self.claim_sync_chunk(*args, **kwargs)

    def acquire_sync_chunk_lease(
        self, chunk_id: int, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> SyncLease | None:
        token = uuid4().hex
        if not self.claim_sync_chunk(chunk_id, token, now=now, lease_seconds=lease_seconds):
            return None
        row = self.db.get(orm.SyncChunk, chunk_id)
        assert row is not None
        from vitalis.application.sync_types import SyncLease
        return SyncLease(row.id, token, row.lease_epoch, row.lease_expires_at)

    def renew_sync_attempt_lease(
        self, attempt_id: str, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, lease_seconds: int = 60
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
            orm.SyncAttempt.lease_expires_at > now,
        ).values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def renew_attempt_lease(self, *args, **kwargs) -> bool:
        return self.renew_sync_attempt_lease(*args, **kwargs)

    def renew_sync_chunk_lease(
        self, chunk_id: int, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, lease_seconds: int = 60
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
            orm.SyncChunk.lease_expires_at > now,
        ).values(
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def renew_chunk_lease(self, *args, **kwargs) -> bool:
        return self.renew_sync_chunk_lease(*args, **kwargs)

    def release_sync_attempt_lease(
        self, attempt_id: str, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, status: str = "queued"
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def release_attempt_lease(self, *args, **kwargs) -> bool:
        return self.release_sync_attempt_lease(*args, **kwargs)

    def release_sync_chunk_lease(
        self, chunk_id: int, lease_token: str, lease_epoch: int, *,
        now: datetime | None = None, status: str = "queued"
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def release_chunk_lease(self, *args, **kwargs) -> bool:
        return self.release_sync_chunk_lease(*args, **kwargs)

    def request_sync_cancel(
        self,
        attempt_id: str,
        *,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        conditions = [
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
            orm.SyncAttempt.cancel_requested_at.is_(None),
        ]
        if user_id is not None:
            conditions.append(orm.SyncAttempt.user_id == user_id)
        result = self.db.execute(update(orm.SyncAttempt).where(*conditions).values(
            cancel_requested_at=now,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def request_cancel(self, *args, **kwargs) -> bool:
        return self.request_sync_cancel(*args, **kwargs)

    def cancel_sync_attempt(
        self,
        attempt_id: str,
        *,
        user_id: str | None = None,
        now: datetime | None = None,
        lease_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        conditions = [
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
            orm.SyncAttempt.cancel_requested_at.is_not(None),
        ]
        if user_id is not None:
            conditions.append(orm.SyncAttempt.user_id == user_id)
        if lease_token is not None:
            conditions.extend([
                orm.SyncAttempt.lease_token == lease_token,
                orm.SyncAttempt.lease_epoch == lease_epoch,
            ])
        else:
            conditions.append(or_(
                orm.SyncAttempt.status.in_(("queued", "retry_wait")),
                orm.SyncAttempt.lease_expires_at <= now,
            ))
        result = self.db.execute(update(orm.SyncAttempt).where(*conditions).values(
            status="cancelled",
            lease_token=None,
            lease_expires_at=None,
            cancelled_at=now,
            finished_at=now,
            updated_at=now,
        ))
        if result.rowcount:
            self.db.execute(update(orm.SyncChunk).where(
                orm.SyncChunk.attempt_id == attempt_id,
                orm.SyncChunk.status.in_(("queued", "retry_wait")),
            ).values(
                status="cancelled",
                lease_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            ))
        self.db.flush()
        return bool(result.rowcount)

    def _finalize_sync_chunk(
        self, chunk_id: int, lease_token: str, lease_epoch: int, status: str,
        *, now: datetime | None = None, next_retry_at: datetime | None = None,
        stages: dict | None = None, raw_records: int = 0, records_written: int = 0,
        error_kind: str | None = None, error: str | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        if status == "retry_wait" and next_retry_at is None:
            raise ValueError("重试等待 chunk 必须提供 next_retry_at")
        owner = self.db.execute(select(
            orm.SyncChunk.attempt_id, orm.SyncAttempt.user_id,
        ).join(
            orm.SyncAttempt, orm.SyncAttempt.id == orm.SyncChunk.attempt_id
        ).where(orm.SyncChunk.id == chunk_id)).one_or_none()
        if owner is None or self.db.execute(select(orm.User.id).where(
            orm.User.id == owner.user_id
        ).with_for_update()).scalar_one_or_none() is None:
            return False
        parent = self.db.execute(select(
            orm.SyncAttempt.lease_token, orm.SyncAttempt.lease_epoch,
        ).where(
            orm.SyncAttempt.id == owner.attempt_id,
            orm.SyncAttempt.user_id == owner.user_id,
        ).with_for_update()).one_or_none()
        if parent is None or parent.lease_token is None:
            return False
        parent_fence = _sync_parent_fence(parent.lease_token, parent.lease_epoch)
        saved_stages = self.db.execute(select(orm.SyncChunk.stages).where(
            orm.SyncChunk.id == chunk_id
        )).scalar_one_or_none()
        values = {
            "status": status,
            "lease_token": None,
            "lease_expires_at": None,
            "next_retry_at": _naive_utc(next_retry_at) if next_retry_at else None,
            "raw_records": raw_records,
            "records_written": records_written,
            "error_kind": error_kind,
            "error": error[:2000] if error else None,
            "finished_at": now if status in ("succeeded", "unavailable", "failed", "cancelled") else None,
            "updated_at": now,
            "stages": {
                key: value for key, value in (stages if stages is not None else saved_stages or {}).items()
                if key != "_attempt_lease_fence"
            },
        }
        if stages is not None:
            for stage_name in ("fetch_status", "parse_status", "write_status"):
                if stage_name in stages:
                    values[stage_name] = stages[stage_name]
        active_attempt = select(orm.SyncAttempt.id).where(
            orm.SyncAttempt.id == orm.SyncChunk.attempt_id,
            orm.SyncAttempt.user_id == owner.user_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.cancel_requested_at.is_(None),
            orm.SyncAttempt.lease_token == parent.lease_token,
            orm.SyncAttempt.lease_epoch == parent.lease_epoch,
            orm.SyncAttempt.lease_expires_at > now,
            exists(select(orm.User.id).where(orm.User.id == owner.user_id)),
            or_(
                orm.SyncAttempt.source_account_id.is_(None),
                exists(select(orm.SourceAccount.id).where(
                    orm.SourceAccount.id == orm.SyncAttempt.source_account_id,
                    orm.SourceAccount.status == "active",
                    orm.SourceAccount.fence_epoch == orm.SyncAttempt.source_account_epoch,
                )),
            ),
        )
        result = self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.id == chunk_id,
            orm.SyncChunk.attempt_id.in_(active_attempt),
            orm.SyncChunk.status == "running",
            orm.SyncChunk.lease_token == lease_token,
            orm.SyncChunk.lease_epoch == lease_epoch,
            orm.SyncChunk.lease_expires_at > now,
            orm.SyncChunk.stages["_attempt_lease_fence"].as_string() == parent_fence,
            *([orm.SyncChunk.allow_unavailable.is_(True)] if status == "unavailable" else []),
        ).values(**values))
        self.db.flush()
        return bool(result.rowcount)

    def finalize_sync_chunk_success(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "succeeded", **kwargs)

    def finalize_sync_chunk_unavailable(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "unavailable", **kwargs)

    def finalize_sync_chunk_retry(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "retry_wait", **kwargs)

    def finalize_sync_chunk_failure(self, *args, **kwargs) -> bool:
        return self._finalize_sync_chunk(*args, "failed", **kwargs)

    def finalize_chunk(self, chunk_id, lease_token, lease_epoch, status, **kwargs) -> bool:
        return self._finalize_sync_chunk(
            chunk_id, lease_token, lease_epoch, status, **kwargs
        )

    def _finalize_sync_attempt(
        self, attempt_id: str, lease_token: str, lease_epoch: int, status: str,
        *, now: datetime | None = None, next_retry_at: datetime | None = None,
        error_kind: str | None = None, error: str | None = None,
    ) -> bool:
        now = _naive_utc(now or datetime.now(timezone.utc))
        if status == "retry_wait" and next_retry_at is None:
            raise ValueError("重试等待 attempt 必须提供 next_retry_at")
        user_id = self.db.execute(select(orm.SyncAttempt.user_id).where(
            orm.SyncAttempt.id == attempt_id
        )).scalar_one_or_none()
        if user_id is None or self.db.execute(select(orm.User.id).where(
            orm.User.id == user_id
        ).with_for_update()).scalar_one_or_none() is None:
            return False
        if self.db.execute(select(orm.SyncAttempt.id).where(
            orm.SyncAttempt.id == attempt_id,
        ).with_for_update()).scalar_one_or_none() is None:
            return False
        aggregate = self.aggregate_sync_attempt(attempt_id)
        workout_evidence = bool(self.db.execute(select(exists().where(
            orm.SyncChunk.attempt_id == attempt_id,
            orm.SyncChunk.stream == "workouts",
        ))).scalar())
        if status == "succeeded" and (
            not aggregate.complete or aggregate.failed_chunks
        ):
            return False
        result = self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.id == attempt_id,
            orm.SyncAttempt.status == "running",
            orm.SyncAttempt.cancel_requested_at.is_(None),
            orm.SyncAttempt.lease_token == lease_token,
            orm.SyncAttempt.lease_epoch == lease_epoch,
            orm.SyncAttempt.lease_expires_at > now,
            orm.SyncAttempt.user_id == user_id,
            exists(select(orm.User.id).where(orm.User.id == user_id)),
            or_(
                orm.SyncAttempt.source_account_id.is_(None),
                exists(select(orm.SourceAccount.id).where(
                    orm.SourceAccount.id == orm.SyncAttempt.source_account_id,
                    orm.SourceAccount.status == "active",
                    orm.SourceAccount.fence_epoch == orm.SyncAttempt.source_account_epoch,
                )),
            ),
        ).values(
            status=status,
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=_naive_utc(next_retry_at) if next_retry_at else None,
            retry_count=(
                orm.SyncAttempt.retry_count + 1 if status == "retry_wait"
                else orm.SyncAttempt.retry_count
            ),
            completed_count=aggregate.completed_count,
            chunk_count=aggregate.total_chunks,
            raw_records=aggregate.raw_records,
            records_written=aggregate.records_written,
            error_kind=error_kind,
            error=error[:4000] if error else None,
            finished_at=(
                now if status in ("succeeded", "partial", "failed", "needs_reauth", "cancelled")
                else None
            ),
            updated_at=now,
        ))
        finalized = bool(result.rowcount)
        if finalized and workout_evidence and status in {
            "succeeded", "partial", "failed"
        }:
            self.bump_analysis_input_revision(user_id)
        self.db.flush()
        return finalized

    def finalize_sync_attempt_success(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "succeeded", **kwargs)

    def finalize_sync_attempt_retry(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "retry_wait", **kwargs)

    def finalize_sync_attempt_failure(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "failed", **kwargs)

    def finalize_sync_attempt_partial(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "partial", **kwargs)

    def finalize_sync_attempt_needs_reauth(self, *args, **kwargs) -> bool:
        return self._finalize_sync_attempt(*args, "needs_reauth", **kwargs)

    def finalize_attempt_success(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_success(*args, **kwargs)

    def finalize_attempt_retry(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_retry(*args, **kwargs)

    def finalize_attempt_failure(self, *args, **kwargs) -> bool:
        return self.finalize_sync_attempt_failure(*args, **kwargs)

    def finalize_attempt(self, attempt_id, lease_token, lease_epoch, status, **kwargs) -> bool:
        return self._finalize_sync_attempt(
            attempt_id, lease_token, lease_epoch, status, **kwargs
        )

    def aggregate_sync_attempt(self, attempt_id: str) -> SyncAttemptAggregate:
        from vitalis.application.sync_types import SyncAttemptAggregate

        attempt = self.db.get(orm.SyncAttempt, attempt_id)
        if attempt is None:
            raise ValueError("同步尝试不存在")
        rows = self.sync_chunks(attempt_id)
        counts = {status: 0 for status in (
            "succeeded", "unavailable", "failed", "cancelled",
            "retry_wait", "running", "queued"
        )}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return SyncAttemptAggregate(
            attempt_id=attempt.id,
            status=attempt.status,
            total_chunks=len(rows),
            succeeded_chunks=counts["succeeded"],
            unavailable_chunks=counts["unavailable"],
            failed_chunks=counts["failed"],
            cancelled_chunks=counts["cancelled"],
            retrying_chunks=counts["retry_wait"],
            running_chunks=counts["running"],
            queued_chunks=counts["queued"],
            raw_records=sum(row.raw_records or 0 for row in rows),
            records_written=sum(row.records_written or 0 for row in rows),
        )

    def sync_attempt_aggregate(self, *args, **kwargs) -> SyncAttemptAggregate:
        return self.aggregate_sync_attempt(*args, **kwargs)

    # ---- 同步数据健康 ----

    def save_sync_stream_state(
        self,
        user_id: str,
        stream: str,
        *,
        fetch_status: str,
        parse_status: str,
        write_status: str,
        fetched_at: datetime | None,
        parsed_at: datetime | None,
        written_at: datetime | None,
        raw_records: int,
        records_written: int,
        error_kind: str | None = None,
        message: str | None = None,
        source: str = "zepp",
        attempt_id: str | None = None,
    ) -> orm.SyncStreamState:
        incoming_attempt = None
        if attempt_id is not None:
            incoming_attempt = self.sync_attempt(attempt_id, user_id=user_id)
            if incoming_attempt is None or incoming_attempt.source != source:
                raise ValueError("同步投影的 attempt 不属于当前用户或数据源")
        row = self.db.execute(select(orm.SyncStreamState).where(
            orm.SyncStreamState.user_id == user_id,
            orm.SyncStreamState.source == source,
            orm.SyncStreamState.stream == stream,
        )).scalar_one_or_none()
        if row is None:
            row = orm.SyncStreamState(
                user_id=user_id, source=source, stream=stream, attempt_id=attempt_id
            )
            self.db.add(row)
        elif attempt_id is not None and row.attempt_id is not None:
            current_attempt = self.sync_attempt(row.attempt_id)
            if (
                current_attempt is not None
                and incoming_attempt is not None
                and current_attempt.created_at > incoming_attempt.created_at
            ):
                return row
        if attempt_id is not None:
            row.attempt_id = attempt_id
        row.fetch_status = fetch_status
        row.parse_status = parse_status
        row.write_status = write_status
        row.fetched_at = _naive_utc(fetched_at) if fetched_at else None
        row.parsed_at = _naive_utc(parsed_at) if parsed_at else None
        row.written_at = _naive_utc(written_at) if written_at else None
        row.last_sample_at = self.latest_stream_sample_at(
            user_id, stream, source=source
        )
        row.raw_records = raw_records
        row.records_written = records_written
        row.error_kind = error_kind
        row.message = message[:1000] if message else None
        row.updated_at = datetime.utcnow()
        self.db.flush()
        return row

    def sync_stream_states(self, user_id: str) -> list[orm.SyncStreamState]:
        return list(self.db.execute(
            select(orm.SyncStreamState).where(
                orm.SyncStreamState.user_id == user_id
            ).order_by(orm.SyncStreamState.stream)
        ).scalars().all())

    def latest_stream_sample_at(
        self, user_id: str, stream: str, source: str = "zepp"
    ) -> datetime | None:
        metric_by_stream = {
            "heart_rate": "heart_rate",
            "heart_rate/minute_endpoint": "heart_rate",
            "hrv": "hrv_sdnn",
            "wellness/hrv_rmssd": "hrv_rmssd",
            "wellness/spo2_point": "spo2",
            "wellness/spo2_odi": "spo2_odi",
            "wellness/spo2_osa": "spo2_apnea_low",
            "wellness/respiratory_rate": "respiratory_rate",
            "wellness/all_day_stress": "stress",
            "wellness/pai": "pai_daily",
            "wellness/lactate_threshold": "lactate_threshold_hr",
        }
        metric = metric_by_stream.get(stream)
        if metric in {
            "heart_rate", "hrv_sdnn", "hrv_rmssd", "spo2", "spo2_apnea_low",
            "stress",
        }:
            latest_sample = self.db.execute(
                select(func.max(orm.MetricSample.timestamp)).where(
                    orm.MetricSample.user_id == user_id,
                    orm.MetricSample.source == source,
                    orm.MetricSample.metric == metric,
                )
            ).scalar_one_or_none()
            if latest_sample is not None or metric != "stress":
                return latest_sample
        if metric:
            value = self.db.execute(select(func.max(orm.DailyMetric.date)).where(
                orm.DailyMetric.user_id == user_id,
                orm.DailyMetric.source == source,
                orm.DailyMetric.metric == metric,
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream == "workout_detail":
            return self.db.execute(select(func.max(orm.WorkoutMetricSample.timestamp)).where(
                orm.WorkoutMetricSample.user_id == user_id,
                orm.WorkoutMetricSample.source == source,
            )).scalar_one_or_none()
        if stream == "workouts":
            return self.db.execute(select(func.max(orm.Workout.started_at)).where(
                orm.Workout.user_id == user_id,
                orm.Workout.source == source,
            )).scalar_one_or_none()
        if stream == "sleep":
            value = self.db.execute(select(func.max(orm.SleepRecord.date)).where(
                orm.SleepRecord.user_id == user_id
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream == "daily_summary":
            value = self.db.execute(select(func.max(orm.DailyMetric.date)).where(
                orm.DailyMetric.user_id == user_id,
                orm.DailyMetric.source == source,
            )).scalar_one_or_none()
            return datetime.combine(value, datetime.min.time()) if value else None
        if stream in {"dense_files", "heart_rate/dense_file"}:
            return self.db.execute(select(func.max(orm.DenseDataFile.end_utc)).where(
                orm.DenseDataFile.user_id == user_id,
                orm.DenseDataFile.source == source,
            )).scalar_one_or_none()
        return None

    def replace_strength_exercises(
        self,
        user_id: str,
        workout_id: str,
        exercises: list[StrengthExerciseRecord],
        workout_source: str = "zepp",
    ) -> list[StrengthExerciseRecord]:
        current = self.strength_exercises_for_workout_keys(
            user_id, [(workout_source, workout_id)]
        ).get((workout_source, workout_id), [])
        comparable = lambda item: item.model_dump(
            mode="json", exclude={"id", "created_at"}
        )
        if [comparable(item) for item in current] == [comparable(item) for item in exercises]:
            return current

        self.db.execute(delete(orm.StrengthExercise).where(
            orm.StrengthExercise.user_id == user_id,
            orm.StrengthExercise.workout_source == workout_source,
            orm.StrengthExercise.workout_id == workout_id,
        ))
        self.db.add_all([
            orm.StrengthExercise(**item.model_dump(mode="python"))
            for item in exercises
        ])
        previous = self.db.execute(select(func.max(
            orm.StrengthCorrectionRevision.revision
        )).where(
            orm.StrengthCorrectionRevision.user_id == user_id,
            orm.StrengthCorrectionRevision.workout_source == workout_source,
            orm.StrengthCorrectionRevision.workout_id == workout_id,
        )).scalar_one() or 0
        self.db.add(orm.StrengthCorrectionRevision(
            user_id=user_id,
            workout_source=workout_source,
            workout_id=workout_id,
            revision=previous + 1,
            status="withdrawn" if not exercises else "confirmed",
            payload=[comparable(item) for item in exercises],
        ))
        self.db.flush()
        self.bump_analysis_input_revision(user_id)
        return exercises

    def strength_exercises_for_workouts(
        self, user_id: str, workout_ids: list[str], source: str = "zepp"
    ) -> dict[str, list[StrengthExerciseRecord]]:
        keyed = self.strength_exercises_for_workout_keys(
            user_id, [(source, workout_id) for workout_id in workout_ids]
        )
        return {
            workout_id: values
            for (_source, workout_id), values in keyed.items()
        }

    def strength_exercises_for_workout_keys(
        self, user_id: str, workout_keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], list[StrengthExerciseRecord]]:
        if not workout_keys:
            return {}
        rows = self.db.execute(select(orm.StrengthExercise).where(
            orm.StrengthExercise.user_id == user_id,
            tuple_(
                orm.StrengthExercise.workout_source,
                orm.StrengthExercise.workout_id,
            ).in_(workout_keys),
        ).order_by(
            orm.StrengthExercise.workout_source,
            orm.StrengthExercise.workout_id,
            orm.StrengthExercise.order,
        )).scalars().all()
        output: dict[tuple[str, str], list[StrengthExerciseRecord]] = {}
        for row in rows:
            key = (row.workout_source, row.workout_id)
            output.setdefault(key, []).append(StrengthExerciseRecord(
                id=row.id,
                user_id=row.user_id,
                workout_source=row.workout_source,
                workout_id=row.workout_id,
                order=row.order,
                exercise_name=row.exercise_name,
                exercise_id=row.exercise_id,
                session_focus=row.session_focus,
                movement_pattern=row.movement_pattern,
                movement_pattern_label=row.movement_pattern_label,
                muscle_groups=list(row.muscle_groups or []),
                muscle_group_labels=list(row.muscle_group_labels or []),
                sets=row.sets,
                repetitions=row.repetitions,
                weight_kg=row.weight_kg,
                rpe=row.rpe,
                rir=row.rir,
                rest_seconds=row.rest_seconds,
                source=row.source,
                confidence=row.confidence,
                confidence_label=row.confidence_label,
                created_at=row.created_at.replace(tzinfo=timezone.utc),
            ))
        return output

    def save_training_preferences(
        self, user_id: str, preferences: TrainingPreferenceInput
    ) -> TrainingPreferences:
        row = self.db.get(orm.TrainingPreference, user_id)
        values = preferences.model_dump(mode="python")
        changed = (
            values != TrainingPreferenceInput().model_dump(mode="python")
            if row is None else any(getattr(row, key) != value for key, value in values.items())
        )
        if row is None:
            row = orm.TrainingPreference(user_id=user_id, **values)
            self.db.add(row)
        elif changed:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = datetime.utcnow()
        self.db.flush()
        if changed:
            self.bump_analysis_input_revision(user_id)
        return self.training_preferences(user_id)

    def patch_training_preferences(
        self, user_id: str, patch: TrainingPreferencePatch
    ) -> TrainingPreferences:
        current = self.training_preferences(user_id)
        merged = current.model_dump(
            mode="python",
            exclude={
                "user_id", "primary_goal", "primary_goal_label",
                "running_required", "strength_required", "updated_at",
            },
        )
        merged.update(patch.model_dump(mode="python", exclude_unset=True))
        validated = TrainingPreferenceInput.model_validate(merged)
        row = self.db.get(orm.TrainingPreference, user_id)
        values = validated.model_dump(mode="python")
        changed = (
            values != TrainingPreferenceInput().model_dump(mode="python")
            if row is None else any(getattr(row, key) != value for key, value in values.items())
        )
        if row is None:
            row = orm.TrainingPreference(user_id=user_id, **values)
            self.db.add(row)
        elif changed:
            for field, value in values.items():
                setattr(row, field, value)
        if changed:
            row.updated_at = datetime.utcnow()
        self.db.flush()
        if changed:
            self.bump_analysis_input_revision(user_id)
        return self.training_preferences(user_id)

    def training_preferences(self, user_id: str) -> TrainingPreferences:
        row = self.db.get(orm.TrainingPreference, user_id)
        if row is None:
            return TrainingPreferences(user_id=user_id)
        return TrainingPreferences(
            user_id=row.user_id,
            weekly_running_target=row.weekly_running_target,
            weekly_strength_target=row.weekly_strength_target,
            rotation_policy=row.rotation_policy,
            treadmill_available=row.treadmill_available,
            bad_weather_running_policy=row.bad_weather_running_policy,
            available_weekdays=list(row.available_weekdays or []),
            max_session_minutes=row.max_session_minutes,
            running_experience=row.running_experience,
            strength_experience=row.strength_experience,
            equipment=list(row.equipment or []),
            pain_or_injury_status=row.pain_or_injury_status,
            pain_or_injury_notes=row.pain_or_injury_notes,
            updated_at=(
                row.updated_at.replace(tzinfo=timezone.utc) if row.updated_at else None
            ),
        )

    # ---- 用户档案 ----

    def user_profile(self, user_id: str) -> UserProfile:
        row = self.db.get(orm.UserProfile, user_id)
        if row is None:
            return UserProfile(user_id=user_id)
        return _user_profile_from_row(row)

    def get_user_profile(self, user_id: str) -> UserProfile:
        return self.user_profile(user_id)

    def patch_user_profile(
        self, user_id: str, patch: UserProfilePatch
    ) -> UserProfile:
        row = self.db.get(orm.UserProfile, user_id)
        current = _user_profile_from_row(row) if row else UserProfile(user_id=user_id)
        if patch.expected_revision != current.revision:
            raise ProfileRevisionConflict(patch.expected_revision, current.revision)

        values = patch.model_dump(
            mode="python", exclude={"expected_revision"}, exclude_unset=True
        )
        changed_fields = [
            field for field, value in values.items()
            if _profile_field_value(getattr(current, field)) != value
        ]
        if not changed_fields:
            return current

        revision = current.revision + 1
        now = datetime.now(timezone.utc)
        naive_now = _naive_utc(now)
        payload = current.model_dump(
            mode="json", exclude={"schema_version", "user_id", "revision"}
        )
        for field in changed_fields:
            value = values[field]
            if value is None:
                payload.pop(field, None)
                continue
            payload[field] = ProfileField(
                value=value,
                source=ProfileSource.USER_CONFIRMED,
                confidence=ConfidenceBand.HIGH,
                revision=revision,
                updated_at=now,
            ).model_dump(mode="json")

        revision_row = orm.UserProfileRevision(
            user_id=user_id,
            revision=revision,
            changed_fields=changed_fields,
            payload=payload,
            created_at=naive_now,
        )
        if row is None:
            try:
                with self.db.begin_nested():
                    self.db.add(orm.UserProfile(
                        user_id=user_id,
                        payload=payload,
                        revision=revision,
                        schema_version="1.0",
                        updated_at=naive_now,
                    ))
                    self.db.add(revision_row)
                    self.db.flush()
            except IntegrityError as exc:
                actual = self.db.get(orm.UserProfile, user_id)
                raise ProfileRevisionConflict(
                    patch.expected_revision,
                    actual.revision if actual is not None else revision,
                ) from exc
        else:
            result = self.db.execute(
                update(orm.UserProfile)
                .where(
                    orm.UserProfile.user_id == user_id,
                    orm.UserProfile.revision == patch.expected_revision,
                )
                .values(
                    payload=payload,
                    revision=revision,
                    schema_version="1.0",
                    updated_at=naive_now,
                )
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                self.db.expire(row)
                actual = self.db.get(orm.UserProfile, user_id)
                raise ProfileRevisionConflict(
                    patch.expected_revision,
                    actual.revision if actual is not None else revision,
                )
            self.db.add(revision_row)
            self.db.flush()
        updated = self.db.get(orm.UserProfile, user_id)
        assert updated is not None
        self.db.refresh(updated)
        self.bump_analysis_input_revision(user_id)
        return _user_profile_from_row(updated)

    def patch_profile(self, user_id: str, patch: UserProfilePatch) -> UserProfile:
        return self.patch_user_profile(user_id, patch)

    # ---- 健康智能事件 ----

    def active_health_events(self, user_id: str) -> list[HealthEvent]:
        rows = self.db.execute(select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id,
            orm.HealthEventRecord.lifecycle != "RESOLVED",
        )).scalars().all()
        return [_event_from_row(row) for row in rows]

    def health_events_as_of(self, user_id: str, as_of: date) -> list[HealthEvent]:
        """Return event state known by a historical analysis date."""
        events = self.db.execute(select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id,
        )).scalars().all()
        output: list[HealthEvent] = []
        for row in events:
            event = _event_from_row(row)
            evaluated = event.last_evaluated_date or event.end_date
            if evaluated <= as_of:
                if event.lifecycle is not EventLifecycle.RESOLVED:
                    output.append(event)
                continue
            snapshot = self.db.execute(
                select(orm.AnalysisSnapshot)
                .join(orm.AnalysisRun, orm.AnalysisSnapshot.analysis_run_id == orm.AnalysisRun.id)
                .where(
                    orm.AnalysisSnapshot.user_id == user_id,
                    orm.AnalysisRun.user_id == user_id,
                    orm.AnalysisSnapshot.profile_type == "daily",
                    orm.AnalysisRun.target_date <= as_of,
                    orm.AnalysisRun.status == "SUCCEEDED",
                ).order_by(
                    orm.AnalysisRun.target_date.desc(),
                    orm.AnalysisRun.completed_at.desc(),
                    orm.AnalysisRun.id.desc(),
                )
            ).scalars().first()
            if snapshot is not None:
                for payload in snapshot.payload.get("events", []):
                    if payload.get("id") == event.id:
                        historical = HealthEvent.model_validate(payload)
                        if historical.lifecycle is not EventLifecycle.RESOLVED:
                            output.append(historical)
                        break
        return output

    def save_health_event(self, user_id: str, event: HealthEvent) -> HealthEvent:
        row = self.db.get(orm.HealthEventRecord, event.id)
        if row is None:
            row = orm.HealthEventRecord(id=event.id, user_id=user_id)
            self.db.add(row)
        if row.user_id != user_id:
            raise ValueError("健康事件 ID 与用户不匹配")
        payload = event.model_dump(mode="json")
        row.event_type = event.type
        row.metric = event.metric
        row.start_date = event.start_date
        row.end_date = event.end_date
        row.lifecycle = event.lifecycle.value
        row.last_observed_date = event.last_observed_date
        row.last_evaluated_date = event.last_evaluated_date or event.end_date
        row.resolved_at = event.resolved_at
        payload["acknowledged"] = row.acknowledged_at is not None
        payload["acknowledged_at"] = (
            row.acknowledged_at.isoformat() if row.acknowledged_at else None
        )
        row.payload = payload
        self.db.flush()
        return _event_from_row(row)

    def save_event_observation(
        self, observation: HealthEventObservation
    ) -> HealthEventObservation:
        row = orm.HealthEventObservation(
            **observation.model_dump(mode="python")
        )
        row.previous_lifecycle = (
            observation.previous_lifecycle.value
            if observation.previous_lifecycle else None
        )
        row.lifecycle = observation.lifecycle.value
        row.created_at = _naive_utc(observation.created_at)
        self.db.add(row)
        self.db.flush()
        return observation

    def event_observations(
        self, user_id: str, event_id: str
    ) -> list[HealthEventObservation]:
        rows = self.db.execute(select(orm.HealthEventObservation).where(
            orm.HealthEventObservation.user_id == user_id,
            orm.HealthEventObservation.event_id == event_id,
        ).order_by(orm.HealthEventObservation.date, orm.HealthEventObservation.created_at)).scalars().all()
        return [HealthEventObservation.model_validate(row, from_attributes=True) for row in rows]

    def event_observations_range(
        self, user_id: str, start: date, end: date
    ) -> list[HealthEventObservation]:
        rows = self.db.execute(select(orm.HealthEventObservation).where(
            orm.HealthEventObservation.user_id == user_id,
            orm.HealthEventObservation.date.between(start, end),
        ).order_by(orm.HealthEventObservation.date.desc(), orm.HealthEventObservation.created_at.desc())).scalars().all()
        return [HealthEventObservation.model_validate(row, from_attributes=True) for row in rows]

    def health_events(
        self,
        user_id: str,
        start: date,
        end: date,
        event_type: str | None = None,
    ) -> list[HealthEvent]:
        statement = select(orm.HealthEventRecord).where(
            orm.HealthEventRecord.user_id == user_id,
            or_(
                (
                    (orm.HealthEventRecord.end_date >= start)
                    & (orm.HealthEventRecord.start_date <= end)
                ),
                orm.HealthEventRecord.resolved_at.between(start, end),
            ),
        )
        if event_type:
            statement = statement.where(orm.HealthEventRecord.event_type == event_type)
        rows = self.db.execute(
            statement.order_by(orm.HealthEventRecord.end_date.desc(), orm.HealthEventRecord.id)
        ).scalars().all()
        output = []
        for row in rows:
            output.append(_event_from_row(row))
        return output

    def health_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        row = self.db.get(orm.HealthEventRecord, event_id)
        if row is None or row.user_id != user_id:
            return None
        return _event_from_row(row)

    def acknowledge_health_event(self, user_id: str, event_id: str) -> HealthEvent | None:
        row = self.db.get(orm.HealthEventRecord, event_id)
        if row is None or row.user_id != user_id:
            return None
        if row.acknowledged_at is None:
            row.acknowledged_at = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = dict(row.payload or {})
        payload["acknowledged"] = True
        payload["acknowledged_at"] = row.acknowledged_at.isoformat()
        row.payload = payload
        self.db.flush()
        return _event_from_row(row)

    # ---- 分析运行、不可变快照与主观反馈 ----

    def create_analysis_run(self, run) -> orm.AnalysisRun:
        row = orm.AnalysisRun(
            id=run.id,
            user_id=run.user_id,
            target_date=run.target_date,
            status=run.status.value,
            started_at=_naive_utc(run.started_at),
            completed_at=_naive_utc(run.completed_at) if run.completed_at else None,
            intelligence_version=run.intelligence_version,
            decision_policy_version=run.decision_policy_version,
            evidence_version=run.evidence_version,
            error=run.error,
            profile_revision_used=run.profile_revision_used,
            input_revision_used=run.input_revision_used,
            config_digest=run.config_digest,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def complete_analysis_run(self, run_id: str, status: str, error: str | None = None):
        row = self.db.get(orm.AnalysisRun, run_id)
        if row is None:
            raise ValueError("分析运行不存在")
        row.status = status
        row.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.error = error[:1024] if error else None
        self.db.flush()
        return row

    def analysis_run(self, user_id: str, run_id: str):
        row = self.db.get(orm.AnalysisRun, run_id)
        return row if row is not None and row.user_id == user_id else None

    def analysis_runs(self, user_id: str, start: date, end: date):
        return list(self.db.execute(select(orm.AnalysisRun).where(
            orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.target_date.between(start, end),
        ).order_by(orm.AnalysisRun.target_date.desc(), orm.AnalysisRun.started_at.desc())).scalars().all())

    def save_analysis_snapshot(
        self,
        analysis_run_id: str,
        user_id: str,
        profile_type: str,
        period_start: date,
        period_end: date,
        schema_version: str,
        intelligence_version: str,
        decision_policy_version: str,
        evidence_version: str,
        payload: dict,
    ) -> orm.AnalysisSnapshot:
        row = orm.AnalysisSnapshot(
            id=uuid4().hex,
            analysis_run_id=analysis_run_id,
            user_id=user_id,
            profile_type=profile_type,
            period_start=period_start,
            period_end=period_end,
            intelligence_version=intelligence_version,
            decision_policy_version=decision_policy_version,
            evidence_version=evidence_version,
            schema_version=schema_version,
            payload=payload,
        )
        self.db.add(row)
        generated_at = payload.get("generated_at")
        if isinstance(generated_at, str):
            parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            row.generated_at = _naive_utc(parsed)
        self.db.flush()
        return row

    @staticmethod
    def _current_snapshot_conditions(user_id: str, profile_type: str):
        conditions = [
            orm.AnalysisSnapshot.user_id == user_id,
            orm.AnalysisSnapshot.profile_type == profile_type,
            orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.status == "SUCCEEDED",
            orm.AnalysisRun.profile_revision_used == func.coalesce(orm.UserProfile.revision, 0),
            orm.AnalysisRun.input_revision_used == orm.User.analysis_input_revision,
            orm.AnalysisRun.config_digest == _current_analysis_config_digest(),
            orm.AnalysisSnapshot.intelligence_version == INTELLIGENCE_VERSION,
            orm.AnalysisSnapshot.decision_policy_version == DECISION_POLICY_VERSION,
            orm.AnalysisSnapshot.evidence_version == EVIDENCE_VERSION,
        ]
        schema = _CURRENT_SNAPSHOT_SCHEMAS.get(profile_type)
        if schema is not None:
            conditions.append(orm.AnalysisSnapshot.schema_version == schema)
        return conditions

    @staticmethod
    def _current_snapshot_query(user_id: str):
        return (
            select(orm.AnalysisSnapshot)
            .join(orm.AnalysisRun, orm.AnalysisSnapshot.analysis_run_id == orm.AnalysisRun.id)
            .join(orm.User, orm.User.id == orm.AnalysisRun.user_id)
            .outerjoin(orm.UserProfile, and_(
                orm.UserProfile.user_id == user_id,
                orm.UserProfile.user_id == orm.AnalysisRun.user_id,
            ))
        )

    def latest_analysis_snapshot(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
    ) -> orm.AnalysisSnapshot | None:
        return self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, profile_type),
                orm.AnalysisSnapshot.period_end == period_end,
            ).order_by(
                orm.AnalysisRun.completed_at.desc().nulls_last(),
                orm.AnalysisRun.id.desc(),
                orm.AnalysisSnapshot.id.desc(),
            )
        ).scalars().first()

    def analysis_snapshot_for_run(
        self, user_id: str, profile_type: str, period_end: date, run_id: str
    ) -> orm.AnalysisSnapshot | None:
        return self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, profile_type),
                orm.AnalysisSnapshot.period_end == period_end,
                orm.AnalysisSnapshot.analysis_run_id == run_id,
            ).order_by(orm.AnalysisSnapshot.id.desc())
        ).scalars().first()

    def latest_analysis_snapshot_for_target(
        self,
        user_id: str,
        profile_type: str,
        target_date: date,
    ) -> orm.AnalysisSnapshot | None:
        """Read a period snapshot produced by the requested analysis target."""
        return self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, profile_type),
                orm.AnalysisRun.target_date == target_date,
                orm.AnalysisSnapshot.period_end <= target_date,
            ).order_by(
                orm.AnalysisSnapshot.period_end.desc(),
                orm.AnalysisRun.completed_at.desc().nulls_last(),
                orm.AnalysisRun.id.desc(),
                orm.AnalysisSnapshot.id.desc(),
            )
        ).scalars().first()

    def latest_analysis_snapshot_on_or_before(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
    ) -> orm.AnalysisSnapshot | None:
        return self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, profile_type),
                orm.AnalysisSnapshot.period_end <= period_end,
            ).order_by(
                orm.AnalysisSnapshot.period_end.desc(),
                orm.AnalysisRun.completed_at.desc().nulls_last(),
                orm.AnalysisRun.id.desc(),
                orm.AnalysisSnapshot.id.desc(),
            )
        ).scalars().first()

    def analysis_snapshot_for_run_on_or_before(
        self,
        user_id: str,
        profile_type: str,
        period_end: date,
        run_id: str,
    ) -> orm.AnalysisSnapshot | None:
        """Load a component from one run when its period ends before the target."""
        return self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, profile_type),
                orm.AnalysisSnapshot.period_end <= period_end,
                orm.AnalysisSnapshot.analysis_run_id == run_id,
            ).order_by(orm.AnalysisSnapshot.period_end.desc(), orm.AnalysisSnapshot.id.desc())
        ).scalars().first()

    def save_recommendation(
        self, recommendation: RecommendationInstance
    ) -> RecommendationInstance:
        row = orm.RecommendationInstance(
            id=recommendation.id,
            analysis_run_id=recommendation.analysis_run_id,
            user_id=recommendation.user_id,
            date=recommendation.date,
            decision=recommendation.decision.model_dump(mode="json"),
            linked_workout_source=recommendation.linked_workout_source,
            linked_workout_id=recommendation.linked_workout_id,
            completion_status=recommendation.completion_status.value,
            created_at=_naive_utc(recommendation.created_at),
            completed_at=(
                _naive_utc(recommendation.completed_at)
                if recommendation.completed_at else None
            ),
        )
        self.db.add(row)
        self.db.flush()
        return recommendation

    def recommendation(
        self, user_id: str, recommendation_id: str
    ) -> RecommendationInstance | None:
        row = self.db.get(orm.RecommendationInstance, recommendation_id)
        if row is None or row.user_id != user_id:
            return None
        return _recommendation_from_row(row)

    def recommendations(
        self, user_id: str, start: date, end: date
    ) -> list[RecommendationInstance]:
        rows = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.date.between(start, end),
        ).order_by(orm.RecommendationInstance.date.desc(), orm.RecommendationInstance.created_at.desc())).scalars().all()
        return [_recommendation_from_row(row) for row in rows]

    def recommendations_for_workouts(
        self, user_id: str, workout_ids: list[str], source: str = "zepp"
    ) -> dict[str, str]:
        keyed = self.recommendations_for_workout_keys(
            user_id, [(source, workout_id) for workout_id in workout_ids]
        )
        return {
            workout_id: recommendation_id
            for (_source, workout_id), recommendation_id in keyed.items()
        }

    def recommendations_for_workout_keys(
        self, user_id: str, workout_keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        if not workout_keys:
            return {}
        rows = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            tuple_(
                orm.RecommendationInstance.linked_workout_source,
                orm.RecommendationInstance.linked_workout_id,
            ).in_(workout_keys),
        )).scalars().all()
        return {
            (row.linked_workout_source, row.linked_workout_id): row.id
            for row in rows
            if row.linked_workout_source is not None
            and row.linked_workout_id is not None
        }

    def link_recommendation(
        self,
        user_id: str,
        recommendation_id: str,
        workout_id: str,
        workout_source: str = "zepp",
    ) -> RecommendationInstance:
        row = self.db.get(orm.RecommendationInstance, recommendation_id)
        if row is None or row.user_id != user_id:
            raise ValueError("训练建议不存在或不属于当前用户")
        if self.workout(user_id, workout_id, source=workout_source) is None:
            raise ValueError("指定训练不存在或不属于当前用户")
        existing = self.db.execute(select(orm.RecommendationInstance).where(
            orm.RecommendationInstance.user_id == user_id,
            orm.RecommendationInstance.linked_workout_source == workout_source,
            orm.RecommendationInstance.linked_workout_id == workout_id,
            orm.RecommendationInstance.id != recommendation_id,
        )).scalar_one_or_none()
        if existing is not None:
            raise ValueError("该训练已关联其他训练建议")
        if row.linked_workout_id and (
            row.linked_workout_source != workout_source
            or row.linked_workout_id != workout_id
        ):
            raise ValueError("训练建议已关联其他训练")
        completed_at = row.completed_at or datetime.now(timezone.utc).replace(tzinfo=None)
        changed = (
            row.linked_workout_source != workout_source
            or row.linked_workout_id != workout_id
            or row.completion_status != RecommendationStatus.COMPLETED.value
            or row.completed_at != completed_at
        )
        row.linked_workout_source = workout_source
        row.linked_workout_id = workout_id
        row.completion_status = RecommendationStatus.COMPLETED.value
        row.completed_at = completed_at
        self.db.flush()
        if changed:
            self._bump_existing_input_revisions({user_id})
        return _recommendation_from_row(row)

    def analysis_snapshots(
        self,
        user_id: str,
        profile_type: str,
        start: date,
        end: date,
    ) -> list[orm.AnalysisSnapshot]:
        conditions = [
            orm.AnalysisSnapshot.user_id == user_id,
            orm.AnalysisSnapshot.profile_type == profile_type,
            orm.AnalysisSnapshot.period_end.between(start, end),
            orm.AnalysisSnapshot.intelligence_version == INTELLIGENCE_VERSION,
            orm.AnalysisSnapshot.decision_policy_version == DECISION_POLICY_VERSION,
            orm.AnalysisSnapshot.evidence_version == EVIDENCE_VERSION,
        ]
        schema = _CURRENT_SNAPSHOT_SCHEMAS.get(profile_type)
        if schema is not None:
            conditions.append(orm.AnalysisSnapshot.schema_version == schema)
        return list(self.db.execute(
            select(orm.AnalysisSnapshot).where(*conditions).order_by(
                orm.AnalysisSnapshot.period_end, orm.AnalysisSnapshot.generated_at
            )
        ).scalars().all())

    def save_subjective_feedback(self, feedback: SubjectiveFeedback) -> SubjectiveFeedback:
        row = orm.SubjectiveFeedback(**feedback.model_dump(mode="python"))
        row.created_at = _naive_utc(feedback.created_at)
        self.db.add(row)
        self.db.flush()
        self.bump_analysis_input_revision(feedback.user_id)
        return feedback

    def create_or_reuse_feedback_request(
        self,
        user_id: str,
        key: str,
        request_hash: str,
        create_feedback: Callable[[], SubjectiveFeedback],
    ) -> SubjectiveFeedback:
        """Claim a user-scoped key before writing feedback in the caller's transaction."""
        dialect = self.db.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise NotImplementedError("feedback idempotency requires SQLite or PostgreSQL")

        statement = insert(orm.FeedbackRequest).values(
            user_id=user_id, idempotency_key=key, request_hash=request_hash,
            feedback_id=None,
        )
        claimed = self.db.execute(statement.on_conflict_do_nothing(
            index_elements=["user_id", "idempotency_key"]
        )).rowcount
        if claimed:
            feedback = create_feedback()
            if feedback.user_id != user_id:
                raise ValueError("反馈不属于当前用户")
            self.db.execute(update(orm.FeedbackRequest).where(
                orm.FeedbackRequest.user_id == user_id,
                orm.FeedbackRequest.idempotency_key == key,
            ).values(feedback_id=feedback.id))
            return feedback

        previous = self.db.execute(select(orm.FeedbackRequest).where(
            orm.FeedbackRequest.user_id == user_id,
            orm.FeedbackRequest.idempotency_key == key,
        )).scalar_one_or_none()
        if previous is None or previous.feedback_id is None:
            raise RuntimeError("feedback request has no committed feedback")
        if previous.request_hash != request_hash:
            raise FeedbackIdempotencyConflict("Idempotency-Key 与此前反馈请求不一致")
        row = self.db.get(orm.SubjectiveFeedback, previous.feedback_id)
        if row is None or row.user_id != user_id:
            raise RuntimeError("feedback request has no user-owned feedback")
        feedback = SubjectiveFeedback.model_validate(row, from_attributes=True)
        feedback.created_at = row.created_at.replace(tzinfo=timezone.utc)
        return feedback

    def subjective_feedback(
        self,
        user_id: str,
        start: date,
        end: date,
    ) -> list[SubjectiveFeedback]:
        rows = self.db.execute(
            select(orm.SubjectiveFeedback).where(
                orm.SubjectiveFeedback.user_id == user_id,
                orm.SubjectiveFeedback.date.between(start, end),
            ).order_by(orm.SubjectiveFeedback.date, orm.SubjectiveFeedback.created_at)
        ).scalars().all()
        return [SubjectiveFeedback.model_validate(row, from_attributes=True) for row in rows]

    # ---- OAuth 令牌 ----

    def _cancel_source_account_attempts(
        self, account_id: str, *, now: datetime, reason: str,
        message: str | None = None,
    ) -> None:
        """Fence queued/running work before a source credential generation changes."""
        attempt_ids = select(orm.SyncAttempt.id).where(
            orm.SyncAttempt.source_account_id == account_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
        )
        self.db.execute(update(orm.SyncChunk).where(
            orm.SyncChunk.attempt_id.in_(attempt_ids),
            orm.SyncChunk.status.in_(("queued", "running", "retry_wait")),
        ).values(
            status="cancelled",
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=None,
            finished_at=now,
            error_kind=reason,
            error=(message or "来源凭据已更新，旧同步尝试已终止")[:2000],
            updated_at=now,
        ))
        self.db.execute(update(orm.SyncAttempt).where(
            orm.SyncAttempt.source_account_id == account_id,
            orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
        ).values(
            status="cancelled",
            lease_token=None,
            lease_expires_at=None,
            next_retry_at=None,
            cancel_requested_at=func.coalesce(
                orm.SyncAttempt.cancel_requested_at, now
            ),
            cancelled_at=now,
            finished_at=now,
            error_kind=reason,
            error=(message or "来源凭据已更新，旧同步尝试已终止")[:2000],
            updated_at=now,
        ))

    def save_token(
        self, token: AuthToken, *, allow_create_user: bool = True,
        allow_reactivate: bool = True,
        expected_account_id: str | None | object = _EXPECTED_UNSET,
        expected_fence_epoch: int | None | object = _EXPECTED_UNSET,
    ) -> bool:
        """Encrypt and save a credential under its authoritative SourceAccount.

        A changed credential is a new source-account generation.  Existing work
        is cancelled while holding the user-first lock, and its epoch no longer
        matches the generation captured by old workers.
        """
        vendor_id = token.source_user_id.strip() if isinstance(token.source_user_id, str) else ""
        if not vendor_id:
            raise SourceIdentityConflict("Zepp 凭据必须包含厂商用户 id")
        # Resolve pending authenticated-user inserts before taking the user lock.
        self.db.flush()
        user = self.db.execute(select(orm.User).where(
            orm.User.id == token.user_id,
        ).with_for_update()).scalar_one_or_none()
        if user is None:
            if not allow_create_user:
                # Pairing/OAuth callers must never recreate an owner deleted in a
                # concurrent transaction.
                raise SourceIdentityConflict("本地用户不存在")
            user = orm.User(id=token.user_id)
            self.db.add(user)
            self.db.flush()
        existing_account = self.source_account(
            token.user_id, token.source, for_update=True
        )
        if expected_account_id is not _EXPECTED_UNSET:
            if existing_account is None or existing_account.id != expected_account_id:
                if expected_account_id is None and existing_account is None:
                    pass
                else:
                    raise SourceIdentityConflict("数据源账号在验证期间发生变化")
            elif expected_fence_epoch is not _EXPECTED_UNSET and (
                existing_account.status != "active"
                or int(existing_account.fence_epoch) != int(expected_fence_epoch or 0)
            ):
                raise SourceIdentityConflict("数据源账号在验证期间已撤销或更新")
        account_was_new = existing_account is None
        was_reactivated = bool(
            existing_account is not None and existing_account.status != "active"
        )
        account = self.ensure_source_account(
            token.user_id, token.source, vendor_id,
            allow_reactivate=allow_reactivate,
        )
        row = self.db.execute(select(orm.AuthToken).where(
            orm.AuthToken.source_account_id == account.id,
        ).with_for_update()).scalar_one_or_none()

        from vitalis.adapters.credentials import decrypt_token, encrypt_token

        expires_at = _naive_utc(token.expires_at) if token.expires_at else None
        previous = None
        if row is not None:
            previous = (
                decrypt_token(row.access_token),
                decrypt_token(row.refresh_token),
                row.expires_at,
                row.scope,
                row.region_host,
            )
        current = (
            token.access_token,
            token.refresh_token,
            expires_at,
            token.scope,
            token.region_host,
        )
        credential_changed = row is None or previous != current
        if credential_changed and not account_was_new and not was_reactivated:
            account.fence_epoch = int(account.fence_epoch or 0) + 1
            self._cancel_source_account_attempts(
                account.id, now=datetime.utcnow(), reason="source_credential_refreshed"
            )
        if row is None:
            row = orm.AuthToken(source_account_id=account.id)
            self.db.add(row)

        row.access_token = encrypt_token(token.access_token)
        row.refresh_token = encrypt_token(token.refresh_token)
        row.expires_at = expires_at
        row.scope = token.scope
        row.region_host = token.region_host
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise SourceIdentityConflict("该厂商账号已绑定到其他本地用户") from exc
        return credential_changed

    def get_token(self, user_id: str, source: str = "zepp") -> AuthToken | None:
        row = self.db.execute(
            select(orm.AuthToken).join(
                orm.SourceAccount, orm.SourceAccount.id == orm.AuthToken.source_account_id
            ).where(
                orm.SourceAccount.user_id == user_id,
                orm.SourceAccount.source == source,
                orm.SourceAccount.status == "active",
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        from vitalis.adapters.credentials import decrypt_token

        account = row.source_account
        return AuthToken(
            user_id=account.user_id,
            source=account.source,
            access_token=decrypt_token(row.access_token),
            refresh_token=decrypt_token(row.refresh_token),
            expires_at=row.expires_at,
            scope=row.scope,
            region_host=row.region_host or "",
            # This is a transient client value; no duplicate ORM column exists.
            source_user_id=account.vendor_id,
        )

    def revoke_source_account(
        self,
        user_id: str,
        source: str = "zepp",
        *,
        reason: str = "source_account_revoked",
        now: datetime | None = None,
    ) -> bool:
        """Revoke one vendor account while retaining the local user and facts."""
        now = _naive_utc(now or datetime.now(timezone.utc))
        owner = self.db.execute(select(orm.User).where(
            orm.User.id == user_id
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            return False
        account = self.source_account(user_id, source, for_update=True)
        if account is None:
            return False
        account.status = "revoked"
        account.revoked_at = now
        account.fence_epoch = int(account.fence_epoch or 0) + 1
        account.updated_at = now
        self.bump_analysis_input_revision(user_id)

        # Credentials and all source-bound capability links become unusable in
        # this transaction; historical observations are intentionally untouched.
        self.db.execute(delete(orm.AuthToken).where(
            orm.AuthToken.source_account_id == account.id
        ))
        self.db.execute(update(orm.ZeppBrowserLink).where(
            orm.ZeppBrowserLink.user_id == user_id,
            orm.ZeppBrowserLink.revoked_at.is_(None),
        ).values(
            status="needs_login",
            message=reason[:512],
            revoked_at=now,
        ))
        self.db.execute(delete(orm.OAuthState).where(
            orm.OAuthState.user_id == user_id,
            orm.OAuthState.source == source,
        ))
        # A revoke invalidates every still-live pairing code as well as the
        # long-lived browser/device links. Terminal workers must not revive one.
        self.db.execute(update(orm.ZeppPairingSession).where(
            orm.ZeppPairingSession.user_id == user_id,
            orm.ZeppPairingSession.status.in_(
                ("waiting", "processing", "failed", "connected")
            ),
            orm.ZeppPairingSession.expires_at > now,
        ).values(
            status="revoked",
            message=reason[:512],
            consumed_at=func.coalesce(orm.ZeppPairingSession.consumed_at, now),
            processing_started_at=None,
            processing_token=None,
        ))
        self._cancel_source_account_attempts(
            account.id,
            now=now,
            reason="source_account_revoked",
            message=reason,
        )

        self.db.flush()
        return True

    def source_account_status(self, user_id: str, source: str = "zepp") -> dict | None:
        account = self.source_account(user_id, source)
        if account is None:
            return None
        return {
            "id": account.id,
            "user_id": account.user_id,
            "source": account.source,
            "vendor_id": account.vendor_id,
            "status": account.status,
            "fence_epoch": account.fence_epoch,
            "revoked_at": account.revoked_at,
        }

    def save_oauth_state(
        self, state: str, user_id: str, source: str = "zepp",
        *, expires_at: datetime | None = None,
    ) -> None:
        if expires_at is None:
            from vitalis.config import settings
            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=settings.oauth_state_ttl_minutes
            )
        self.db.merge(orm.OAuthState(
            id=state,
            user_id=user_id,
            source=source,
            expires_at=_naive_utc(expires_at),
        ))
        self.db.flush()

    def oauth_state_exists(self, state: str, *, now: datetime | None = None) -> bool:
        current = _naive_utc(now or datetime.now(timezone.utc))
        return self.db.execute(select(orm.OAuthState.id).where(
            orm.OAuthState.id == state,
            orm.OAuthState.expires_at > current,
        )).scalar_one_or_none() is not None

    def consume_oauth_state(
        self, state: str, *, now: datetime | None = None
    ) -> str | None:
        """Atomically consume a live one-time state and return its user id."""
        current = _naive_utc(now or datetime.now(timezone.utc))
        result = self.db.execute(
            delete(orm.OAuthState).where(
                orm.OAuthState.id == state,
                orm.OAuthState.expires_at > current,
            ).returning(orm.OAuthState.user_id)
        )
        row = result.first()
        self.db.flush()
        return row[0] if row is not None else None

    # ---- Zepp 浏览器扩展配对 ----

    def create_pairing_session(self, pairing_id: str, user_id: str, expires_at: datetime, sync_days: int = 30) -> orm.ZeppPairingSession:
        # Pairing creation is only reached after API authentication; the
        # submission path separately locks and validates the owner before save.
        self.upsert_user(user_id)
        row = orm.ZeppPairingSession(
            id=pairing_id,
            user_id=user_id,
            expires_at=_naive_utc(expires_at),
            sync_days=sync_days,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def pairing_session(self, pairing_id: str) -> orm.ZeppPairingSession | None:
        return self.db.get(orm.ZeppPairingSession, pairing_id)

    def claim_pairing_session(
        self, pairing_id: str, processing_lease_seconds: int = 120,
        *, rate_limit_attempts: int = 12, rate_window_seconds: int = 60,
        now: datetime | None = None,
    ) -> str | None:
        owner_id = self.db.execute(select(orm.ZeppPairingSession.user_id).where(
            orm.ZeppPairingSession.id == pairing_id,
        )).scalar_one_or_none()
        if owner_id is None:
            return None
        try:
            self.claim_source_account(owner_id, "zepp")
        except SourceIdentityConflict:
            return None
        now = _naive_utc(now or datetime.now(timezone.utc))
        reclaim_before = now - timedelta(seconds=max(1, processing_lease_seconds))
        rate_cutoff = now - timedelta(seconds=rate_window_seconds)
        window_expired = or_(
            orm.ZeppPairingSession.rate_window_started_at.is_(None),
            orm.ZeppPairingSession.rate_window_started_at <= rate_cutoff,
        )
        claim_token = uuid4().hex
        result = self.db.execute(
            update(orm.ZeppPairingSession).where(
                orm.ZeppPairingSession.id == pairing_id,
                orm.ZeppPairingSession.user_id == owner_id,
                or_(
                    orm.ZeppPairingSession.status.in_(("waiting", "failed")),
                    (
                        (orm.ZeppPairingSession.status == "processing")
                        & (orm.ZeppPairingSession.processing_started_at <= reclaim_before)
                    ),
                ),
                orm.ZeppPairingSession.expires_at > now,
                or_(window_expired, orm.ZeppPairingSession.rate_window_attempts < rate_limit_attempts),
            ).values(
                status="processing", message="正在验证 Zepp 凭据",
                processing_started_at=now,
                processing_token=claim_token,
                processing_epoch=orm.ZeppPairingSession.processing_epoch + 1,
                rate_window_started_at=case(
                    (window_expired, now),
                    else_=orm.ZeppPairingSession.rate_window_started_at,
                ),
                rate_window_attempts=case(
                    (window_expired, 1),
                    else_=orm.ZeppPairingSession.rate_window_attempts + 1,
                ),
            )
        )
        self.db.flush()
        return claim_token if result.rowcount else None

    def pairing_claim(
        self, pairing_id: str, processing_token: str
    ) -> PairingClaim | None:
        """Return the current processing lease plus its source fencing snapshot."""
        row = self.db.execute(select(orm.ZeppPairingSession).where(
            orm.ZeppPairingSession.id == pairing_id,
            orm.ZeppPairingSession.status == "processing",
            orm.ZeppPairingSession.processing_token == processing_token,
        )).scalar_one_or_none()
        if row is None:
            return None
        source_claim = self.claim_source_account(row.user_id, "zepp")
        return PairingClaim(
            pairing_id=row.id,
            user_id=row.user_id,
            sync_days=row.sync_days,
            processing_token=processing_token,
            processing_epoch=int(row.processing_epoch),
            source_claim=source_claim,
        )

    def pairing_retry_after(
        self, pairing_id: str, rate_limit_attempts: int, rate_window_seconds: int,
        *, now: datetime | None = None,
    ) -> int | None:
        row = self.pairing_session(pairing_id)
        if row is None or row.rate_window_started_at is None:
            return None
        remaining = (
            row.rate_window_started_at + timedelta(seconds=rate_window_seconds)
            - _naive_utc(now or datetime.now(timezone.utc))
        ).total_seconds()
        if row.rate_window_attempts >= rate_limit_attempts and remaining > 0:
            return max(1, int(remaining + 0.999999))
        return None

    def lock_pairing_claim(
        self, pairing_id: str, user_id: str, processing_token: str,
        *, processing_epoch: int | None = None,
    ) -> bool:
        # Match delete_for_user's user-first lock order; both locks survive until commit.
        owner = self.db.execute(update(orm.User).where(
            orm.User.id == user_id,
        ).values(name=orm.User.name))
        if not owner.rowcount:
            return False
        now = datetime.utcnow()
        result = self.db.execute(update(orm.ZeppPairingSession).where(
            orm.ZeppPairingSession.id == pairing_id,
            orm.ZeppPairingSession.user_id == user_id,
            orm.ZeppPairingSession.status == "processing",
            orm.ZeppPairingSession.processing_token == processing_token,
            *(
                [orm.ZeppPairingSession.processing_epoch == processing_epoch]
                if processing_epoch is not None else []
            ),
            orm.ZeppPairingSession.expires_at > now,
        ).values(processing_token=processing_token))
        return bool(result.rowcount)

    def finish_pairing_session(
        self, pairing_id: str, processing_token: str,
        message: str = "已连接", sync_attempt_id: str | None = None,
    ) -> bool:
        now = datetime.utcnow()
        conditions = [
            orm.ZeppPairingSession.id == pairing_id,
            orm.ZeppPairingSession.status == "processing",
            orm.ZeppPairingSession.processing_token == processing_token,
            orm.ZeppPairingSession.expires_at > now,
        ]
        result = self.db.execute(
            update(orm.ZeppPairingSession).where(*conditions).values(
                status="connected",
                message=message[:512],
                consumed_at=now,
                processing_started_at=None,
                processing_token=None,
                sync_attempt_id=sync_attempt_id,
            )
        )
        self.db.flush()
        return bool(result.rowcount)

    def fail_pairing_session(
        self, pairing_id: str, processing_token: str, message: str
    ) -> bool:
        conditions = [
            orm.ZeppPairingSession.id == pairing_id,
            orm.ZeppPairingSession.status == "processing",
            orm.ZeppPairingSession.processing_token == processing_token,
            orm.ZeppPairingSession.expires_at > datetime.utcnow(),
        ]
        result = self.db.execute(update(orm.ZeppPairingSession).where(*conditions).values(
            status="failed",
            message=message[:512],
            processing_started_at=None,
            processing_token=None,
        ))
        self.db.flush()
        return bool(result.rowcount)

    # ---- Zepp 浏览器持续连接 ----

    def create_browser_link(
        self, token_digest: str, user_id: str, sync_attempt_id: str | None = None
    ) -> orm.ZeppBrowserLink:
        self.upsert_user(user_id)
        row = orm.ZeppBrowserLink(
            token_digest=token_digest,
            user_id=user_id,
            status="connected",
            message="浏览器已连接",
            last_verified_at=datetime.utcnow(),
            sync_attempt_id=sync_attempt_id,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def browser_link(self, token_digest: str) -> orm.ZeppBrowserLink | None:
        return self.db.get(orm.ZeppBrowserLink, token_digest)

    def claim_browser_link(self, token_digest: str) -> BrowserLinkClaim | None:
        """Lock owner/source first, then link, before browser I/O."""
        link = self.db.execute(select(orm.ZeppBrowserLink).where(
            orm.ZeppBrowserLink.token_digest == token_digest,
            orm.ZeppBrowserLink.revoked_at.is_(None),
        )).scalar_one_or_none()
        if link is None:
            return None
        source_claim = self.claim_source_account(link.user_id, "zepp")
        locked_link = self.db.execute(select(orm.ZeppBrowserLink).where(
            orm.ZeppBrowserLink.token_digest == token_digest,
            orm.ZeppBrowserLink.user_id == link.user_id,
            orm.ZeppBrowserLink.revoked_at.is_(None),
        ).with_for_update()).scalar_one_or_none()
        if locked_link is None:
            return None
        return BrowserLinkClaim(
            token_digest=token_digest,
            user_id=link.user_id,
            source_claim=source_claim,
        )

    def lock_browser_link(self, claim: BrowserLinkClaim) -> bool:
        """Reacquire owner/link/source locks and reject stale or revoked claims."""
        owner = self.db.execute(select(orm.User).where(
            orm.User.id == claim.user_id,
        ).with_for_update()).scalar_one_or_none()
        if owner is None:
            return False
        link = self.db.execute(select(orm.ZeppBrowserLink).where(
            orm.ZeppBrowserLink.token_digest == claim.token_digest,
            orm.ZeppBrowserLink.user_id == claim.user_id,
            orm.ZeppBrowserLink.revoked_at.is_(None),
        ).with_for_update()).scalar_one_or_none()
        return link is not None and self.source_claim_current(claim.source_claim)

    def latest_browser_link(self, user_id: str) -> orm.ZeppBrowserLink | None:
        return self.db.execute(
            select(orm.ZeppBrowserLink).where(
                orm.ZeppBrowserLink.user_id == user_id,
                orm.ZeppBrowserLink.revoked_at.is_(None),
            ).order_by(orm.ZeppBrowserLink.created_at.desc()).limit(1)
        ).scalar_one_or_none()

    def mark_browser_link_verified(self, token_digest: str, message: str = "登录状态有效") -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            now = datetime.utcnow()
            row.status = "connected"
            row.message = message[:512]
            row.last_seen_at = now
            row.last_verified_at = now
            self.db.flush()

    def mark_browser_link_reauth(self, token_digest: str, message: str) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            row.status = "needs_login"
            row.message = message[:512]
            row.last_seen_at = datetime.utcnow()
            self.db.flush()

    def mark_user_browser_links_reauth(self, user_id: str, message: str) -> None:
        rows = self.db.execute(
            select(orm.ZeppBrowserLink).where(
                orm.ZeppBrowserLink.user_id == user_id,
                orm.ZeppBrowserLink.revoked_at.is_(None),
            )
        ).scalars().all()
        now = datetime.utcnow()
        for row in rows:
            row.status = "needs_login"
            row.message = message[:512]
            row.last_seen_at = now
        self.db.flush()

    def mark_browser_link_synced(
        self, token_digest: str, message: str, sync_attempt_id: str | None = None
    ) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None:
            if row.status != "needs_login":
                row.status = "connected"
                row.message = message[:512]
            row.last_sync_at = datetime.utcnow()
            if sync_attempt_id is not None:
                row.sync_attempt_id = sync_attempt_id
            self.db.flush()

    def mark_browser_link_sync_failed(self, token_digest: str, message: str) -> None:
        row = self.browser_link(token_digest)
        if row and row.revoked_at is None and row.status != "needs_login":
            row.status = "connected"
            row.message = message[:512]
            self.db.flush()

    def attach_browser_link_attempt(
        self, token_digest: str, sync_attempt_id: str | None
    ) -> None:
        row = self.browser_link(token_digest)
        if row is not None and row.revoked_at is None:
            row.sync_attempt_id = sync_attempt_id
            self.db.flush()

    # ---- durable notification delivery ----

    _SAFE_NOTIFICATION_ERRORS = {
        "lease_expired_uncertain",
        "snapshot_unavailable",
        "delivery_disabled",
        "stale_report_date",
        "stale_plan_expired",
        "sleep_incomplete",
        "stored_data_incomplete",
        "unusable_sync_state",
        "transport_ambiguous",
        "transport_rejected",
        "render_failed",
    }

    def enqueue_notification_delivery(
        self, user_id: str, analysis_run_id: str, period: str, target_date: date
    ) -> orm.NotificationDelivery:
        if period not in {"morning", "evening"}:
            raise ValueError("notification period must be morning or evening")
        run = self.db.execute(select(orm.AnalysisRun).where(
            orm.AnalysisRun.id == analysis_run_id,
            orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.target_date == target_date,
            orm.AnalysisRun.status == "SUCCEEDED",
        )).scalar_one_or_none()
        if run is None:
            raise ValueError("notification delivery requires a succeeded analysis run")
        existing = self.db.execute(select(orm.NotificationDelivery).where(
            orm.NotificationDelivery.user_id == user_id,
            orm.NotificationDelivery.target_date == target_date,
            orm.NotificationDelivery.period == period,
        )).scalar_one_or_none()
        if existing is not None:
            if existing.analysis_run_id != analysis_run_id:
                result = self.db.execute(update(orm.NotificationDelivery).where(
                    orm.NotificationDelivery.id == existing.id,
                    orm.NotificationDelivery.analysis_run_id == existing.analysis_run_id,
                    or_(
                        orm.NotificationDelivery.status.in_(("pending", "failed")),
                        and_(
                            orm.NotificationDelivery.status == "deferred",
                            orm.NotificationDelivery.last_error.in_((
                                "delivery_disabled", "snapshot_unavailable",
                                "sleep_incomplete", "stored_data_incomplete",
                            )),
                        ),
                    ),
                ).values(
                    analysis_run_id=analysis_run_id,
                    status="pending",
                    attempt_count=0,
                    lease_token=None,
                    lease_expires_at=None,
                    next_attempt_at=None,
                    last_error=None,
                    updated_at=datetime.utcnow(),
                ))
                if result.rowcount:
                    self.db.refresh(existing)
            return existing
        row = orm.NotificationDelivery(
            id=hashlib.sha256(
                f"{user_id}:{target_date.isoformat()}:{period}".encode("utf-8")
            ).hexdigest(),
            user_id=user_id,
            analysis_run_id=analysis_run_id,
            period=period,
            target_date=target_date,
            status="pending",
        )
        dialect = self.db.get_bind().dialect.name
        if dialect in {"sqlite", "postgresql"}:
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as dialect_insert
            else:
                from sqlalchemy.dialects.postgresql import insert as dialect_insert
            statement = dialect_insert(orm.NotificationDelivery).values(
                id=row.id,
                user_id=row.user_id,
                analysis_run_id=row.analysis_run_id,
                period=row.period,
                target_date=row.target_date,
                status=row.status,
            ).on_conflict_do_nothing(
                index_elements=["user_id", "target_date", "period"]
            )
            self.db.execute(statement)
            return self.db.execute(select(orm.NotificationDelivery).where(
                orm.NotificationDelivery.user_id == user_id,
                orm.NotificationDelivery.target_date == target_date,
                orm.NotificationDelivery.period == period,
            )).scalar_one()
        self.db.add(row)
        self.db.flush()
        return row

    def rearm_unavailable_notification_deliveries(
        self, user_id: str, analysis_run_id: str, target_date: date
    ) -> int:
        """Rearm only existing, definitely unsent intents after a successful run."""
        run = self.db.execute(select(orm.AnalysisRun.id).where(
            orm.AnalysisRun.id == analysis_run_id,
            orm.AnalysisRun.user_id == user_id,
            orm.AnalysisRun.target_date == target_date,
            orm.AnalysisRun.status == "SUCCEEDED",
        )).scalar_one_or_none()
        if run is None:
            raise ValueError("notification rearm requires a succeeded analysis run")
        result = self.db.execute(update(orm.NotificationDelivery).where(
            orm.NotificationDelivery.user_id == user_id,
            orm.NotificationDelivery.target_date == target_date,
            orm.NotificationDelivery.status == "deferred",
            orm.NotificationDelivery.last_error == "snapshot_unavailable",
        ).values(
            analysis_run_id=analysis_run_id,
            status="pending",
            attempt_count=0,
            last_error=None,
            next_attempt_at=None,
            lease_token=None,
            lease_expires_at=None,
            updated_at=datetime.utcnow(),
        ))
        self.db.flush()
        return result.rowcount

    def notification_delivery(
        self, delivery_id: str, user_id: str
    ) -> orm.NotificationDelivery | None:
        """Return a delivery row only within its owning user's scope."""
        row = self.db.get(orm.NotificationDelivery, delivery_id)
        return row if row is not None and row.user_id == user_id else None

    def notification_deliveries(
        self, user_id: str, *, limit: int = 100
    ) -> list[orm.NotificationDelivery]:
        return list(self.db.execute(
            select(orm.NotificationDelivery).where(
                orm.NotificationDelivery.user_id == user_id
            ).order_by(
                orm.NotificationDelivery.target_date.desc(),
                orm.NotificationDelivery.period,
                orm.NotificationDelivery.created_at.desc(),
            ).limit(max(1, min(limit, 1000)))
        ).scalars().all())

    def notification_delivery_snapshot(
        self, delivery_id: str, user_id: str, analysis_run_id: str,
        lease_token: str,
    ) -> orm.AnalysisSnapshot | None:
        """Prepare the newest eligible daily snapshot under the delivery lease."""
        now = _naive_utc(datetime.now(timezone.utc))
        delivery = self.db.execute(select(orm.NotificationDelivery).where(
            orm.NotificationDelivery.id == delivery_id,
            orm.NotificationDelivery.user_id == user_id,
            orm.NotificationDelivery.analysis_run_id == analysis_run_id,
            orm.NotificationDelivery.status == "running",
            orm.NotificationDelivery.lease_token == lease_token,
            orm.NotificationDelivery.lease_expires_at > now,
        )).scalar_one_or_none()
        if delivery is None:
            return None
        latest_run_id = self.db.execute(
            select(orm.AnalysisRun.id).where(
                orm.AnalysisRun.user_id == user_id,
                orm.AnalysisRun.target_date == delivery.target_date,
                orm.AnalysisRun.status == "SUCCEEDED",
            ).order_by(
                orm.AnalysisRun.completed_at.desc(), orm.AnalysisRun.id.desc()
            ).limit(1)
        ).scalar_one_or_none()
        if latest_run_id is None:
            return None
        snapshot = self.db.execute(
            self._current_snapshot_query(user_id).where(
                *self._current_snapshot_conditions(user_id, "daily"),
                orm.AnalysisSnapshot.analysis_run_id == latest_run_id,
                orm.AnalysisSnapshot.period_end == delivery.target_date,
            )
        ).scalars().first()
        if snapshot is None:
            return None
        if latest_run_id != analysis_run_id:
            result = self.db.execute(update(orm.NotificationDelivery).where(
                orm.NotificationDelivery.id == delivery_id,
                orm.NotificationDelivery.user_id == user_id,
                orm.NotificationDelivery.status == "running",
                orm.NotificationDelivery.analysis_run_id == analysis_run_id,
                orm.NotificationDelivery.lease_token == lease_token,
                orm.NotificationDelivery.lease_expires_at > now,
            ).values(analysis_run_id=latest_run_id, updated_at=now))
            if not result.rowcount:
                return None
            self.db.flush()
        return snapshot

    def claim_notification_delivery(
        self, *, lease_seconds: int = 300, now: datetime | None = None,
        max_attempts: int = 3,
    ) -> orm.NotificationDelivery | None:
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("notification lease and retry limits must be positive")
        current = _naive_utc(now or datetime.now(timezone.utc))
        # A lost worker cannot safely retry a remote notification: the remote may
        # have accepted it before the process died. Convert expired leases to an
        # explicit uncertain state before claiming new, definitely pending work.
        self.db.execute(update(orm.NotificationDelivery).where(
            orm.NotificationDelivery.status == "running",
            orm.NotificationDelivery.lease_token.is_not(None),
            orm.NotificationDelivery.lease_expires_at.is_not(None),
            orm.NotificationDelivery.lease_expires_at <= current,
        ).values(
            status="uncertain",
            last_error="lease_expired_uncertain",
            lease_token=None,
            lease_expires_at=None,
            updated_at=current,
        ))
        candidate = self.db.execute(select(orm.NotificationDelivery.id).where(
            orm.NotificationDelivery.status.in_(("pending", "failed")),
            or_(
                orm.NotificationDelivery.next_attempt_at.is_(None),
                orm.NotificationDelivery.next_attempt_at <= current,
            ),
            orm.NotificationDelivery.attempt_count < max_attempts,
        ).order_by(orm.NotificationDelivery.created_at).limit(1)).scalar_one_or_none()
        if candidate is None:
            self.db.flush()
            return None
        token = uuid4().hex
        result = self.db.execute(update(orm.NotificationDelivery).where(
            orm.NotificationDelivery.id == candidate,
            orm.NotificationDelivery.status.in_(("pending", "failed")),
            or_(
                orm.NotificationDelivery.next_attempt_at.is_(None),
                orm.NotificationDelivery.next_attempt_at <= current,
            ),
            orm.NotificationDelivery.attempt_count < max_attempts,
        ).values(
            status="running",
            lease_token=token,
            lease_expires_at=current + timedelta(seconds=max(1, lease_seconds)),
            attempt_count=orm.NotificationDelivery.attempt_count + 1,
            updated_at=current,
        ))
        self.db.flush()
        if not result.rowcount:
            return None
        return self.db.get(orm.NotificationDelivery, candidate)

    def complete_notification_delivery(
        self, delivery_id: str, lease_token: str, status: str,
        *, error: str | None = None, next_attempt_at: datetime | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed", "uncertain", "deferred"}:
            raise ValueError("invalid notification delivery status")
        safe_error = error if error in self._SAFE_NOTIFICATION_ERRORS else (
            "delivery_failed" if error else None
        )
        now = _naive_utc(datetime.now(timezone.utc))
        if status == "deferred" and safe_error == "snapshot_unavailable":
            owner_id = self.db.execute(select(orm.NotificationDelivery.user_id).where(
                orm.NotificationDelivery.id == delivery_id,
            )).scalar_one_or_none()
            if owner_id is None:
                return False
            owner = self.db.execute(update(orm.User).where(
                orm.User.id == owner_id,
            ).values(analysis_input_revision=orm.User.analysis_input_revision))
            if not owner.rowcount:
                return False
            now = _naive_utc(datetime.now(timezone.utc))
            delivery = self.db.execute(select(orm.NotificationDelivery).where(
                orm.NotificationDelivery.id == delivery_id,
                orm.NotificationDelivery.status == "running",
                orm.NotificationDelivery.lease_token == lease_token,
                orm.NotificationDelivery.lease_expires_at > now,
            )).scalar_one_or_none()
            if delivery is not None:
                snapshot = self.notification_delivery_snapshot(
                    delivery.id, delivery.user_id, delivery.analysis_run_id,
                    lease_token,
                )
                if snapshot is not None:
                    result = self.db.execute(update(orm.NotificationDelivery).where(
                        orm.NotificationDelivery.id == delivery_id,
                        orm.NotificationDelivery.status == "running",
                        orm.NotificationDelivery.analysis_run_id == snapshot.analysis_run_id,
                        orm.NotificationDelivery.lease_token == lease_token,
                        orm.NotificationDelivery.lease_expires_at > _naive_utc(datetime.now(timezone.utc)),
                    ).values(
                        status="pending",
                        attempt_count=0,
                        last_error=None,
                        next_attempt_at=None,
                        lease_token=None,
                        lease_expires_at=None,
                        updated_at=now,
                    ))
                    if result.rowcount:
                        self.db.flush()
                        return True
        result = self.db.execute(update(orm.NotificationDelivery).where(
            orm.NotificationDelivery.id == delivery_id,
            orm.NotificationDelivery.status == "running",
            orm.NotificationDelivery.lease_token == lease_token,
            orm.NotificationDelivery.lease_expires_at > now,
        ).values(
            status=status,
            last_error=safe_error,
            next_attempt_at=_naive_utc(next_attempt_at) if next_attempt_at else None,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
        ))
        self.db.flush()
        return bool(result.rowcount)

    def delete_for_user(self, user_id: str) -> None:
        self.db.execute(select(orm.User.id).where(
            orm.User.id == user_id
        ).with_for_update()).scalar_one_or_none()
        for model in (
            orm.Device, orm.SleepRecord, orm.ActivityRecord, orm.TrainingRecord,
            orm.MetricSample, orm.DailyMetric, orm.DenseDataFile,
            orm.WorkoutMetricSample, orm.StrengthExercise, orm.StrengthCorrectionRevision, orm.Workout,
            orm.TrainingPreference,
            orm.UserProfileRevision, orm.UserProfile,
            orm.HealthEventObservation, orm.HealthEventRecord, orm.AnalysisSnapshot,
            orm.RecommendationInstance, orm.AnalysisJob,
            orm.NotificationDelivery, orm.AnalysisRun,
            orm.FeedbackRequest, orm.SubjectiveFeedback,
            orm.SyncStreamState,
            orm.ZeppBrowserLink, orm.ZeppPairingSession,
            orm.AccessToken, orm.AuthToken, orm.OAuthState, orm.SourceAccount,
        ):
            self.db.execute(delete(model).where(model.user_id == user_id))
        attempt_ids = select(orm.SyncAttempt.id).where(orm.SyncAttempt.user_id == user_id)
        self.db.execute(delete(orm.SyncChunk).where(orm.SyncChunk.attempt_id.in_(attempt_ids)))
        self.db.execute(delete(orm.SyncAttempt).where(orm.SyncAttempt.user_id == user_id))
        self.db.execute(delete(orm.User).where(orm.User.id == user_id))


def _profile_field_value(field: ProfileField | None):
    return field.value if field is not None else None


def _user_profile_from_row(row: orm.UserProfile) -> UserProfile:
    payload = dict(row.payload or {})
    values = {"user_id": row.user_id, "revision": row.revision}
    for field in ("sex", "confirmed_hrmax_bpm", "sleep_target_minutes"):
        raw = payload.get(field)
        if raw is not None:
            values[field] = ProfileField.model_validate(raw)
        else:
            values[field] = None
    return UserProfile.model_validate(values)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sync_parent_fence(token: str, epoch: int) -> str:
    return hashlib.sha256(f"{epoch}:{token}".encode("utf-8")).hexdigest()


def _recommendation_from_row(row: orm.RecommendationInstance) -> RecommendationInstance:
    return RecommendationInstance(
        id=row.id,
        analysis_run_id=row.analysis_run_id,
        user_id=row.user_id,
        date=row.date,
        decision=row.decision,
        linked_workout_source=row.linked_workout_source,
        linked_workout_id=row.linked_workout_id,
        completion_status=RecommendationStatus(row.completion_status),
        created_at=row.created_at.replace(tzinfo=timezone.utc),
        completed_at=(
            row.completed_at.replace(tzinfo=timezone.utc) if row.completed_at else None
        ),
    )


def _event_from_row(row: orm.HealthEventRecord) -> HealthEvent:
    payload = dict(row.payload or {})
    payload.update({
        "lifecycle": row.lifecycle,
        "last_observed_date": row.last_observed_date,
        "last_evaluated_date": row.last_evaluated_date,
        "resolved_at": row.resolved_at,
        "acknowledged": row.acknowledged_at is not None,
        "acknowledged_at": (
            row.acknowledged_at.replace(tzinfo=timezone.utc)
            if row.acknowledged_at else None
        ),
    })
    return HealthEvent.model_validate(payload)
