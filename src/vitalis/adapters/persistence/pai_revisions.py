"""Whole PAI upload revisions, scoped to local day and vendor stream identity."""
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from . import models as orm

PAI_METRICS = frozenset({"pai_daily", "pai_total", "device_max_hr", "device_resting_hr",
    *(f"pai_{zone}_zone{suffix}" for zone in ("low", "medium", "high")
      for suffix in ("", "_minutes", "_lower_limit"))})


class PaiRevision(Base):
    __tablename__ = "pai_revisions"
    __table_args__ = (UniqueConstraint("user_id", "source", "day", "source_scope", "device_id",
                                      name="uq_pai_revision_stream"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    day: Mapped[date] = mapped_column(Date)
    source_scope: Mapped[str] = mapped_column(String(24))
    device_id: Mapped[str] = mapped_column(String(128), default="")
    revision_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def _naive(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def replace_pai(repo, user_id: str, revisions: list[dict], metrics: list) -> int:
    """Atomically replace accepted whole uploads; stale pages cannot revive fields."""
    accepted, affected = set(), set()
    for value in revisions:
        day, scope, device = value["day"], value["source_scope"], value.get("device_id") or ""
        revision = _naive(value.get("revision_at"))
        previous = repo.db.execute(select(PaiRevision).where(
            PaiRevision.user_id == user_id, PaiRevision.source == "zepp", PaiRevision.day == day,
            PaiRevision.source_scope == scope, PaiRevision.device_id == device,
        ).with_for_update()).scalar_one_or_none()
        if previous is not None and (revision is None or previous.revision_at is not None and revision <= previous.revision_at):
            continue
        repo.db.execute(delete(orm.DailyMetric).where(
            orm.DailyMetric.user_id == user_id, orm.DailyMetric.source == "zepp", orm.DailyMetric.date == day,
            orm.DailyMetric.source_scope == scope, orm.DailyMetric.device_id == device,
            orm.DailyMetric.metric.in_(PAI_METRICS),
        ))
        if previous is None:
            previous = PaiRevision(user_id=user_id, source="zepp", day=day, source_scope=scope, device_id=device)
            repo.db.add(previous)
        previous.revision_at = revision
        previous.observed_at = _naive(value.get("observed_at"))
        accepted.add((day, scope, device))
        affected.add(day)
    repo.db.flush()
    selected = [metric for metric in metrics if (metric.date, metric.source_scope, metric.device_id or "") in accepted]
    written = repo.save_daily_metrics(selected)
    if affected:
        # Normal metric writes already advance revision; withdrawals need it too.
        if not selected:
            repo.bump_analysis_input_revision(user_id)
        repo.enqueue_analysis_job(user_id, max(affected), event_type="source_sync", source="zepp",
            affected_dates=affected, affected_streams={"wellness/pai"}, reason="PAI upload revision replaced")
    return written + len(accepted)
