"""Independent nap observations, including days with no main-sleep record."""
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class SleepSupplement(Base):
    __tablename__ = "sleep_supplements"
    __table_args__ = (Index("ix_sleep_supplement_user_day", "user_id", "day", "fetched_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    naps: Mapped[list] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


def save_naps(repo, user_id: str, day: date, naps: list, *, source: str = "zepp",
              fetched_at: datetime | None = None) -> int:
    """Append changed source revisions and invalidate their analysis window."""
    payload = [item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item) for item in naps]
    previous = repo.db.execute(select(SleepSupplement).where(
        SleepSupplement.user_id == user_id, SleepSupplement.day == day, SleepSupplement.source == source,
    ).order_by(SleepSupplement.fetched_at.desc(), SleepSupplement.id.desc()).limit(1)).scalar_one_or_none()
    if previous and previous.naps == payload:
        return 0
    stamp = fetched_at or datetime.now(timezone.utc)
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    repo.db.add(SleepSupplement(id=uuid4().hex, user_id=user_id, day=day, source=source,
        naps=payload, fetched_at=stamp.replace(tzinfo=None),
        source_record_id=repo.db.info.get("active_source_record_id")))
    repo.db.flush()
    repo._bump_existing_input_revisions({user_id})
    repo.enqueue_analysis_job(user_id, day, event_type="source_sync", source=source,
        affected_dates={day}, affected_streams={"sleep", "nap_minutes"}, reason="nap observations changed")
    return 1


def read_naps(db, user_id: str, start: date, end: date, as_of: datetime) -> dict[date, list[dict]]:
    cutoff = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)
    rows = db.execute(select(SleepSupplement).where(
        SleepSupplement.user_id == user_id, SleepSupplement.day.between(start,end),
        SleepSupplement.fetched_at <= cutoff.replace(tzinfo=None),
    ).order_by(SleepSupplement.fetched_at, SleepSupplement.id)).scalars()
    return {row.day: list(row.naps) for row in rows}
