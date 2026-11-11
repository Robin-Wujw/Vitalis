"""Explicit raw source reads, distinct from typed health interpretation."""
from .ports import HealthReader


class VendorQuery:
    def __init__(self, reader: HealthReader):
        self._reader = reader

    def records(self, user_id, stream, start, end, limit=50):
        if not 0 <= (end - start).days < 730 or not 1 <= limit <= 100:
            raise ValueError("invalid raw record range or limit")
        rows = self._reader.raw_source_records(user_id, stream, start, end, limit + 1)
        return {"state": "ready" if rows else "no_data", "records": rows[:limit],
                "truncated": len(rows) > limit, "qualification": "raw_unverified"}

    def record(self, user_id, record_id):
        return self._reader.raw_source_record(user_id, record_id)

    def job_result(self, user_id, job_id):
        return self._reader.raw_job_result(user_id, job_id)
