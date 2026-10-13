"""Assemble application jobs and the verified external source adapter."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import partial
from typing import Callable
from zoneinfo import ZoneInfo

from vitalis.adapters.persistence import HealthRepository, database, session_scope
from vitalis.adapters.persistence.health_reader import SqlHealthReader
from vitalis.adapters.persistence.intelligence_store import SqlIntelligenceStore
from vitalis.adapters.persistence.analysis_jobs import SqlAnalysisJobRepository
from vitalis.application.aggregation import RangeSummaryQuery
from vitalis.application.health_query import HealthQuery
from vitalis.application.product_tracking import ProductTrackingService
from vitalis.adapters.persistence.product_tracking import SqlProductTrackingStore
from vitalis.application.connector import HealthConnector
from vitalis.application.jobs import configure_analysis_jobs as bind_analysis_jobs
from vitalis.application.ports import (
    AnalysisJobRepository,
    AnalysisRunner,
    JobClaim,
    RangeSummaryReader,
)
from vitalis.application.connection import ConnectionService
from vitalis.application.source_accounts import SourceAccountService
from vitalis.application.sync import SyncService
from vitalis.application.sync_jobs import SyncJobService
from vitalis.application.intelligence_service import (
    IntelligenceAction,
    IntelligenceCommand,
    IntelligenceQuery,
)
from vitalis.config import settings
from vitalis.domain import ActivityRecord, NormalizedDaily, SleepRecord, TrainingRecord
from vitalis.adapters.zepp.catalog import load_catalog
from vitalis.time import local_today


def _intelligence_uow():
    return SqlIntelligenceStore(database.UnitOfWork).uow()


def get_intelligence_command(
    *, today_factory: Callable[[], date] | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> IntelligenceCommand:
    return IntelligenceCommand(
        _intelligence_uow,
        timezone_name=settings.timezone,
        catalog_revision=load_catalog().catalog_revision,
        today_factory=today_factory or local_today,
        now_factory=now_factory or (lambda: datetime.now(timezone.utc)),
        product_context_factory=lambda user_id, day, as_of: get_product_tracking_service(
            today_factory=today_factory or local_today,
            now_factory=now_factory or (lambda: datetime.now(timezone.utc)),
        ).analysis_context(user_id, day, as_of=as_of),
    )


def get_shadow_batch(
    *, now_factory: Callable[[], datetime] | None = None,
):
    from vitalis.application.shadow_batch import ShadowBatch

    return ShadowBatch(
        _intelligence_uow,
        timezone_name=settings.timezone,
        now_factory=now_factory or (lambda: datetime.now(timezone.utc)),
    )


def get_feedback_link_service():
    from vitalis.application.feedback_links import FeedbackLinkService

    return FeedbackLinkService(
        _intelligence_uow,
        save_feedback=get_intelligence_action()._save_feedback,
        now_factory=lambda: datetime.now(timezone.utc),
    )


def get_intelligence_query() -> IntelligenceQuery:
    return IntelligenceQuery(_intelligence_uow, today_factory=local_today)


def get_intelligence_action() -> IntelligenceAction:
    return IntelligenceAction(_intelligence_uow, today_factory=local_today)


def get_product_tracking_service(
    *, today_factory: Callable[[], date] | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> ProductTrackingService:
    return ProductTrackingService(
        SqlProductTrackingStore(database.SessionLocal, timezone_name=settings.timezone),
        today_factory=today_factory or local_today,
        now_factory=now_factory,
        timezone_name=settings.timezone,
    )


def _run_analysis(claim: JobClaim, *, repository: AnalysisJobRepository) -> str:
    return get_intelligence_command().analyze(
        claim.user_id, claim.target_date, job_claim=claim, job_repository=repository,
    ).run.id


def configure_analysis_jobs(
    *, repository: AnalysisJobRepository | None = None, runner: AnalysisRunner | None = None,
) -> None:
    """Connect the HTTP and worker entry points to the same durable queue."""
    store = repository if repository is not None else SqlAnalysisJobRepository(database.SessionLocal)
    bind_analysis_jobs(
        store,
        runner if runner is not None else partial(_run_analysis, repository=store),
    )


class _SqlRangeSummaryReader(RangeSummaryReader):
    """Compose the SQL repository into the application range-summary port."""

    def load_daily_records(
        self, user_id: str, start: date, end: date
    ) -> list[NormalizedDaily]:
        with session_scope() as db:
            repository = HealthRepository(db)
            sleeps = {
                date.fromisoformat(record["date"]): record
                for record in repository.sleep_range(user_id, start, end)
            }
            activities = {
                date.fromisoformat(record["date"]): record
                for record in repository.activity_range(user_id, start, end)
            }
            training = {
                date.fromisoformat(record["date"]): record
                for record in repository.training_range(user_id, start, end)
            }

        records: list[NormalizedDaily] = []
        day = start
        while day <= end:
            records.append(
                NormalizedDaily(
                    user_id=user_id,
                    date=day,
                    sleep=(
                        SleepRecord.model_validate(sleeps[day])
                        if day in sleeps
                        else None
                    ),
                    activity=(
                        ActivityRecord.model_validate(activities[day])
                        if day in activities
                        else None
                    ),
                    training=(
                        TrainingRecord.model_validate(training[day])
                        if day in training
                        else None
                    ),
                )
            )
            day += timedelta(days=1)
        return records


def _next_auto_sync() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    candidate = now.replace(
        hour=settings.sync_cron_hour,
        minute=settings.sync_cron_minute,
        second=0,
        microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def get_health_query() -> HealthQuery:
    """Compose the stored raw-health query with the SQL reader adapter."""
    return HealthQuery(
        SqlHealthReader(database.SessionLocal),
        timezone_name=settings.timezone,
        next_auto_sync_factory=_next_auto_sync,
    )


def list_delivery_alerts(user_id: str, *, limit: int = 100, status: str | None = None) -> list[dict]:
    """Return sanitized internal alerts through the composition root."""
    with database.session_scope() as db:
        rows = HealthRepository(db).delivery_alerts(user_id, limit=limit, status=status)
        return [
            {
                "alert_id": row.id,
                "event_key": row.event_key,
                "category": row.category,
                "reason": row.reason,
                "status": row.status,
                "period": row.period,
                "target_date": row.target_date,
                "observed_at": row.observed_at,
                "acknowledged_at": row.acknowledged_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "external_channel": "unconfigured",
            }
            for row in rows
        ]


def get_range_summary() -> RangeSummaryQuery:
    """Return the range-summary use case with the production SQL reader."""
    return RangeSummaryQuery(_SqlRangeSummaryReader())


def get_source_account_service() -> SourceAccountService:
    """Compose source-account management with the SQL UnitOfWork adapter."""
    return SourceAccountService(database.UnitOfWork)


def get_connection_service(*, provider=None) -> ConnectionService:
    """Compose the grouped credential/pairing workflow and vendor ports."""
    connector = provider or get_connector("zepp")

    def create_attempt(
        user_id: str,
        *,
        days: int,
        trigger: str,
        trigger_ref: str | None = None,
        repository=None,
    ):
        create = getattr(connector, "create_attempt", None)
        if create is None:
            return None
        return create(
            user_id,
            days=days,
            trigger=trigger,
            trigger_ref=trigger_ref,
            repository=repository,
        )

    return ConnectionService(
        database.UnitOfWork,
        connector,
        sync_creator=create_attempt,
        pairing_ttl_minutes=settings.pairing_ttl_minutes,
        pairing_processing_lease_seconds=settings.pairing_processing_lease_seconds,
        pairing_rate_limit_attempts=settings.pairing_rate_limit_attempts,
        pairing_rate_limit_window_seconds=settings.pairing_rate_limit_window_seconds,
    )


def get_sync_job_service(*, adapter=None) -> SyncJobService:
    """Compose the current durable sync-job use case and source adapter."""
    from vitalis.adapters.zepp.sync_coordinator import ZeppSyncJobAdapter

    return SyncJobService(
        adapter or ZeppSyncJobAdapter(connector_factory=get_connector)
    )


def get_demo_sync() -> SyncService:
    """Compose the synchronous synthetic demo with current SQL persistence."""
    return SyncService(
        get_connector("zepp", mock=True),
        database.UnitOfWork,
    )


def get_connector(source: str, **kwargs) -> HealthConnector:
    if source != "zepp":
        raise KeyError(f"unknown source: {source}")
    from vitalis.adapters.zepp import ZeppConnector

    return ZeppConnector(**kwargs)


def get_zepp_login_adapter():
    """Compose the official-web login adapter outside the API route layer."""
    from vitalis.adapters.zepp.login import ZeppLoginAdapter

    return ZeppLoginAdapter()


def available_sources() -> list[str]:
    return ["zepp"]
