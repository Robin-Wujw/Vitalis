"""Persist sanitized source metadata at the same boundary as its raw journal."""
from datetime import datetime, timezone

from .models import SourceSnapshot


def save_source_snapshot(repo, user_id: str, kind: str, data: dict, *, fetched_at=None) -> int:
    from .source_journal import SourceJournalRecord

    journal_id = repo.db.info.get("active_source_record_id")
    journal = repo.db.get(SourceJournalRecord, journal_id) if journal_id else None
    if journal_id and (journal is None or journal.user_id != user_id or journal.source != "zepp" or journal.stream != kind):
        raise ValueError("source snapshot journal scope mismatch")
    stamp = fetched_at or (journal.fetched_at if journal is not None else datetime.now(timezone.utc))
    if stamp.tzinfo:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    key = (user_id, "zepp", kind)
    row = repo.db.get(SourceSnapshot, key)
    if row and row.fetched_at > stamp:
        return 0
    values = dict(data=data, fetched_at=stamp, source_record_id=journal_id)
    if row:
        for name, value in values.items():
            setattr(row, name, value)
    else:
        repo.db.add(SourceSnapshot(user_id=user_id, source="zepp", kind=kind, **values))
    repo.db.flush()
    return 1
