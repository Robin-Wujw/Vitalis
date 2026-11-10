"""Bind JSON facts to an owner-checked source journal without rewriting age."""

from datetime import timezone


def bind_record_evidence(repo, record, previous: dict | None, *, stream: str) -> dict:
    data = record.model_dump(mode="json", exclude_none=True)
    metadata = ("fetched_at", "source_record_id")
    def core(value):
        if isinstance(value, dict):
            return {key: core(item) for key, item in value.items() if key not in metadata}
        if isinstance(value, list):
            return [core(item) for item in value]
        return value
    previous = previous or {}
    for key in metadata:
        data.pop(key, None)
    if previous and core(previous) == core(data):
        for key in metadata:
            if previous.get(key) is not None:
                data[key] = previous[key]
    journal = repo._active_chart_journal()
    if journal is not None and journal.user_id == record.user_id and journal.source == record.source and journal.stream == stream:
        if data.get("fetched_at") is None:
            stamp = journal.fetched_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            data["fetched_at"] = stamp.astimezone(timezone.utc).isoformat()
            data["source_record_id"] = journal.id
    return data
