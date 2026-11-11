"""Read-only workout queries composed around deterministic statistics."""
from datetime import date

from vitalis.intelligence.workout_statistics import GROUPS, METRICS, matches, workout_statistics

from .ports import HealthReader


MAX_WORKOUTS = 10000


class WorkoutStatistics:
    def __init__(self, reader: HealthReader, *, detail_catalog=None):
        self._reader = reader
        self._detail_catalog = detail_catalog or {}

    def _read(self, user_id, start, end, timezone_name, filters):
        if start > end:
            raise ValueError("from must not be after to")
        rows = self._reader.workout_statistics_workouts(user_id, start, end, MAX_WORKOUTS + 1,
                                                       timezone_name=timezone_name)
        selected = [row for row in rows[:MAX_WORKOUTS] if matches({**row.data, "source": row.source}, **filters)]
        return selected, len(rows) > MAX_WORKOUTS

    def statistics(self, user_id: str, *, start: date, end: date, timezone_name: str,
                   group_by="month", metric=None, sport=None, family=None, workout_type=None, source=None):
        rows, truncated = self._read(user_id, start, end, timezone_name,
            dict(sport=sport, family=family, workout_type=workout_type, source=source))
        data = [{**row.data, "source": row.source, "workout_id": row.workout_id,
                 "strength_sets": (row.detail or {}).get("strength_sets", [])} for row in rows]
        return {"user_id": user_id, "filters": {"sport": sport, "family": family, "type": workout_type, "source": source},
                **workout_statistics(data, start=start, end=end, timezone_name=timezone_name,
                                     group_by=group_by, metric=metric, truncated=truncated)}

    def list_workouts(self, user_id: str, *, start: date, end: date, timezone_name: str,
                      limit=50, offset=0, sport=None, family=None, workout_type=None, source=None,
                      include_strength_details=False):
        if not 1 <= limit <= 500 or offset < 0 or include_strength_details and limit > 20:
            raise ValueError("invalid workout page size")
        rows, truncated = self._read(user_id, start, end, timezone_name,
            dict(sport=sport, family=family, workout_type=workout_type, source=source))
        page = rows[offset:offset + limit]
        from .health_query import _strength_details

        result = {"user_id": user_id, "state": "partial" if truncated else "ready" if rows else "no_data",
                  "range": {"from": start.isoformat(), "to": end.isoformat(), "timezone": timezone_name, "day_semantics": "activity_day"},
                  "total": len(rows), "offset": offset, "returned": len(page),
                  "coverage": {"scope": "saved_records_only", "truncated": truncated,
                               "vendor_history_complete": None, "read_limit": MAX_WORKOUTS},
                  "sports_seen": sorted({str(row.data.get("sport_mode") or "unknown") for row in rows}),
                  "workouts": [{**row.data, "source": row.source, "workout_id": row.workout_id,
                      "detail_available": row.detail_synced,
                      **({"strength_details": _strength_details(row)} if include_strength_details else {})} for row in page]}
        if offset + limit < len(rows):
            result["next_offset"] = offset + limit
        return result

    def detail(self, user_id: str, workout_id: str, source: str, *, sections=None):
        selected = set(sections if sections is not None else ("summary", "laps", "splits"))
        if not selected <= {"summary", "laps", "splits", "streams", "gps"}:
            raise ValueError("unknown workout section")
        saved = self._reader.workout_detail(user_id, workout_id, source, include_samples="streams" in selected)
        if saved is None:
            return None
        row, samples = saved
        detail = row.detail or {}
        result = {"user_id": user_id, "workout_id": row.workout_id, "source": row.source,
                  "state": "ready" if row.detail_synced else "detail_missing",
                  "detail_available": row.detail_synced, "available_fields": detail.get("available_fields", []),
                  "fetched_at": detail.get("fetched_at"), "schema_version": detail.get("schema_version"),
                  "observed_at": row.data.get("started_at"), "day_semantics": "activity_day",
                  "pauses": detail.get("pauses", [])}
        if "summary" in selected:
            result["summary"] = {**row.data, "source": row.source, "workout_id": row.workout_id}
        if "laps" in selected:
            result.update(laps=detail.get("laps", []), strength_sets=detail.get("strength_sets", []), swimming=detail.get("swimming", {}))
        if "splits" in selected:
            result["splits"] = detail.get("splits", {})
        if "gps" in selected:
            result["gps"] = detail.get("gps", {})
        if "streams" in selected:
            result["streams"] = {"vendor_streams": detail.get("vendor_streams", {}),
                "samples": [{"timestamp": sample.timestamp.isoformat(), "metric": sample.metric,
                    "value": sample.value, "unit": sample.unit, "source_scope": sample.source_scope,
                    "device_id": sample.device_id} for sample in samples]}
        return result

    def catalog(self):
        return {"metric_catalog": {name: {"unit": unit, "aggregation": method} for name, (unit, method) in METRICS.items()},
                "group_by": sorted(GROUPS), "sections": ["summary", "laps", "splits", "streams", "gps"],
                "coverage_scope": "saved_records_only", "day_semantics": "activity_day", **self._detail_catalog}
