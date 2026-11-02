"""Whole-revision fencing for vendor readiness, including withdrawn components."""
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from vitalis.domain.features import DAILY_FEATURE_METRICS
from vitalis.time import local_day, local_day_utc_bounds

from .database import Base
from . import models as orm

READINESS_METRICS = frozenset({"readiness", "physical_readiness", "mental_readiness", "hrv_readiness",
    "rhr_readiness", "skin_temp_readiness", "ahi_readiness", "afib_readiness", "skin_temp_delta",
    "skin_temp_baseline_delta", "sleep_hrv", "sleep_rhr", "hrv_baseline", "rhr_baseline"})


class ReadinessRevision(Base):
    __tablename__ = "readiness_revisions"
    __table_args__ = (UniqueConstraint("user_id","source","day","source_scope","device_id", name="uq_readiness_revision_stream"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    source_scope: Mapped[str] = mapped_column(String(24))
    device_id: Mapped[str] = mapped_column(String(128), default="")
    revision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime)


def _naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def replace_readiness(repo, user_id: str, revisions: list[dict], metrics: list, samples: list, *, timezone_name: str) -> int:
    """Apply one complete newest revision; do not resurrect withdrawn readings."""
    accepted = set()
    changed_days = set()
    for value in revisions:
        day, scope, device = value["day"], value["source_scope"], value.get("device_id") or ""
        revision = _naive(value["revision_at"]) if value.get("revision_at") else None
        previous = repo.db.execute(select(ReadinessRevision).where(
            ReadinessRevision.user_id==user_id,ReadinessRevision.source=="zepp",ReadinessRevision.day==day,
            ReadinessRevision.source_scope==scope,ReadinessRevision.device_id==device,
        )).scalar_one_or_none()
        if previous and previous.revision_at is not None and (revision is None or revision <= previous.revision_at):
            continue
        if previous is not None and revision is None:
            # A second unversioned response cannot prove it is newer.
            continue
        left,right=local_day_utc_bounds(day,timezone_name)
        query=repo.db.execute(delete(orm.DailyMetric).where(orm.DailyMetric.user_id==user_id,
            orm.DailyMetric.source=="zepp",orm.DailyMetric.date==day,orm.DailyMetric.source_scope==scope,
            orm.DailyMetric.device_id==device,orm.DailyMetric.metric.in_(READINESS_METRICS)))
        sample_query=repo.db.execute(delete(orm.MetricSample).where(orm.MetricSample.user_id==user_id,
            orm.MetricSample.source=="zepp",orm.MetricSample.source_scope==scope,orm.MetricSample.device_id==device,
            orm.MetricSample.metric.in_(READINESS_METRICS),orm.MetricSample.timestamp>=_naive(left),orm.MetricSample.timestamp<_naive(right)))
        if query.rowcount or sample_query.rowcount:
            changed_days.add(day)
        if previous is None:
            previous=ReadinessRevision(user_id=user_id,source="zepp",day=day,source_scope=scope,device_id=device,
                observed_at=_naive(value["observed_at"]))
            repo.db.add(previous)
        previous.revision_at=revision
        previous.observed_at=_naive(value["observed_at"])
        accepted.add((day,scope,device))
    repo.db.flush()
    filtered_metrics=[m for m in metrics if (m.date,m.source_scope,m.device_id or "") in accepted]
    filtered_samples=[s for s in samples if (local_day(s.timestamp,timezone_name),s.source_scope,s.device_id or "") in accepted]
    written=repo.save_daily_metrics(filtered_metrics)+repo.save_metric_samples(filtered_samples)
    # A revision containing only sentinel/null fields still changes analysis.
    for day in changed_days:
        for metric in READINESS_METRICS & set(DAILY_FEATURE_METRICS):
            repo.refresh_daily_features(user_id,metric,day,timezone_name=timezone_name)
    if changed_days:
        repo.bump_analysis_input_revision(user_id)
        repo.enqueue_analysis_job(user_id,max(changed_days),event_type="source_sync",source="zepp",
            affected_dates=changed_days,affected_streams=READINESS_METRICS,reason="readiness revision replaced")
    return written
