"""Durable, resumable Zepp synchronization coordinator.

The coordinator deliberately keeps network work outside SQLAlchemy transactions.  A
chunk is the unit of retry, fencing, and commit; completed chunks are never rolled
back when a later page fails.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import logging
import os
import random as random_module
from threading import Event, Thread
import time
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlalchemy import or_, select, update

from vitalis.connectors.zepp.client import SPORTS, ZeppAPIClient, ZeppAuthError
from vitalis.connectors.zepp.dense_hr import decode_sec_hr_archive
from vitalis.connectors.zepp.fetcher import (
    CHUNK_DAYS,
    DAY_MILLISECONDS,
    HEART_RATE_PAGE_LIMIT,
    DataFetcher,
    FetchWindow,
    FetchedRecord,
    RawRecord,
    _heart_rate_cursor,
    _heart_rate_items,
)
from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.connectors.zepp.sync_manager import StreamReport, SyncManager, SyncReport
from vitalis.models import DenseDataFile, User
from vitalis.storage import HealthRepository
from vitalis.storage.database import SessionLocal
from vitalis.storage import models as orm
from vitalis.storage.sync_types import SyncLease
from vitalis.time import local_day_utc_bounds


PLAN_VERSION = "zepp-sync-v4"
MAX_CHUNK_ATTEMPTS = 5
CHUNK_LEASE_SECONDS = 120
ATTEMPT_LEASE_SECONDS = 300
MAX_DETAIL_CHUNKS = 4
DETAIL_REFRESH_DAYS = 28
MAX_DENSE_ARCHIVE_CHUNKS = 1

log = logging.getLogger("vitalis.sync")


class SyncCancelled(ZeppAuthError):
    def __init__(self, message: str = "同步已取消"):
        super().__init__(message, kind="cancelled")


class SyncDeadlineExceeded(ZeppAuthError):
    def __init__(self, message: str = "同步达到时间预算"):
        super().__init__(message, kind="timeout")


class StaleSyncLease(RuntimeError):
    """A worker lost its fencing lease before the finalize transaction."""


@dataclass(frozen=True)
class SyncControl:
    """Clock- and cancellation-aware request budget.

    ``deadline_at`` is the durable wall-clock deadline.  ``monotonic_clock`` is
    used for a local request slice so a wall-clock adjustment cannot extend a
    request indefinitely.
    """

    deadline_at: datetime | None = None
    cancel_check: Callable[[], bool] | None = None
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    monotonic_clock: Callable[[], float] = time.monotonic
    slice_seconds: float = 30.0

    @staticmethod
    def budget_for_days(days: int) -> int:
        """Return a non-decreasing budget for a local-date window."""
        days = max(1, int(days))
        return max(90, min(45 + days * 3, 20 * 60))

    @classmethod
    def budget_for_root_chunks(cls, root_chunks: int) -> int:
        """Return a monotonic budget when the planner grows its root set."""
        return cls.budget_for_days(max(1, int(root_chunks)))

    budget_for_window = budget_for_days

    @classmethod
    def for_window(
        cls,
        window: FetchWindow,
        *,
        deadline_at: datetime | None = None,
        cancel_check: Callable[[], bool] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        slice_seconds: float = 30.0,
    ) -> "SyncControl":
        days = max(1, (date.fromisoformat(window.end_day()) - date.fromisoformat(window.start_day())).days + 1)
        wall = wall_clock or (lambda: datetime.now(timezone.utc))
        if deadline_at is None:
            deadline_at = wall() + timedelta(seconds=cls.budget_for_days(days))
        return cls(
            deadline_at=deadline_at,
            cancel_check=cancel_check,
            wall_clock=wall,
            monotonic_clock=monotonic_clock or time.monotonic,
            slice_seconds=slice_seconds,
        )

    def remaining_wall_seconds(self) -> float | None:
        if self.deadline_at is None:
            return None
        now = self.wall_clock()
        deadline = self.deadline_at
        if deadline.tzinfo is None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
        return (deadline - now).total_seconds()

    def check(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise SyncCancelled()
        remaining = self.remaining_wall_seconds()
        if remaining is not None and remaining <= 0:
            raise SyncDeadlineExceeded()

    def monotonic_remaining(self, started_at: float) -> float:
        return max(0.0, self.slice_seconds - (self.monotonic_clock() - started_at))

    def request_timeout(self, maximum: float = 30.0, started_at: float | None = None) -> float:
        self.check()
        remaining = self.remaining_wall_seconds()
        timeout = min(float(maximum), float(self.slice_seconds))
        if started_at is not None:
            timeout = min(timeout, self.monotonic_remaining(started_at))
        if remaining is not None:
            timeout = min(timeout, max(0.001, remaining))
        return max(0.001, timeout)

    # Friendly aliases used by callers/tests.
    remaining_seconds = remaining_wall_seconds
    assert_available = check


@dataclass
class _ChunkResult:
    record: FetchedRecord | None = None
    raw_records: int = 0
    next_cursor: Any | None = None
    archive: bytes | None = None
    decoded: Any | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _ClaimedChunk:
    lease: SyncLease
    chunk: dict[str, Any]


def stable_chunk_key(
    stream: str,
    partition: str,
    start: datetime | None,
    end: datetime | None,
    cursor: Any | None,
) -> str:
    """Build the v1 stable key without depending on Python object repr."""
    def value(item: Any) -> str:
        if isinstance(item, datetime):
            normalized = item.replace(tzinfo=timezone.utc) if item.tzinfo is None else item
            return normalized.astimezone(timezone.utc).isoformat()
        if item is None:
            return ""
        return str(item)

    material = "|".join(("v1", stream, partition, value(start), value(end), value(cursor)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ZeppSyncCoordinator:
    """Plan and execute durable Zepp sync attempts."""

    def __init__(
        self,
        connector: Any | None = None,
        *,
        connector_factory: Callable[..., Any] | None = None,
        session_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        random: Callable[[], float] | random_module.Random | None = None,
        random_fn: Callable[[], float] | None = None,
        lease_seconds: int = CHUNK_LEASE_SECONDS,
        attempt_lease_seconds: int = ATTEMPT_LEASE_SECONDS,
    ):
        self.connector = connector
        self.connector_factory = connector_factory
        self.session_factory = session_factory or SessionLocal
        selected_clock = wall_clock or clock
        if selected_clock is not None and not callable(selected_clock) and hasattr(selected_clock, "now"):
            selected_clock = selected_clock.now
        self.wall_clock = selected_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or time.monotonic
        if random_fn is not None:
            self.random_fn = random_fn
        elif callable(random):
            self.random_fn = random
        elif random is not None and hasattr(random, "random"):
            self.random_fn = random.random
        else:
            self.random_fn = random_module.random
        self.lease_seconds = max(1, int(lease_seconds))
        self.attempt_lease_seconds = max(1, int(attempt_lease_seconds))

    @contextmanager
    def _session(self) -> Iterator[Any]:
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _naive(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def _window(self, window: FetchWindow | tuple[datetime, datetime] | None, days: int | None) -> FetchWindow:
        if window is not None:
            if isinstance(window, FetchWindow):
                return window
            if isinstance(window, tuple) and len(window) == 2:
                return FetchWindow(start=window[0], end=window[1])
            raise ZeppAuthError("同步窗口格式无效", kind="invalid_request")
        if days is None:
            raise ZeppAuthError("同步窗口不能为空", kind="invalid_request")
        if not 1 <= int(days) <= 730:
            raise ZeppAuthError("同步天数必须在 1..730 之间", kind="invalid_request")
        from vitalis.time import local_today

        end_day = local_today()
        start_day = end_day - timedelta(days=int(days) - 1)
        return FetchWindow.local_dates(start_day, end_day)

    @staticmethod
    def _local_windows(window: FetchWindow) -> list[FetchWindow]:
        start_day = date.fromisoformat(window.start_day())
        end_day = date.fromisoformat(window.end_day())
        out: list[FetchWindow] = []
        cursor = start_day
        while cursor <= end_day:
            last = min(end_day, cursor + timedelta(days=CHUNK_DAYS - 1))
            start, _ = local_day_utc_bounds(cursor)
            _, end = local_day_utc_bounds(last)
            out.append(FetchWindow(start=start, end=end))
            cursor = last + timedelta(days=1)
        return out

    @staticmethod
    def _spec(
        stream: str,
        partition: str,
        window: FetchWindow,
        *,
        cursor: Any = None,
        health_stream: str | None = None,
        ordinal: int = 0,
        allow_unavailable: bool = False,
        operation: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stable_key": stable_chunk_key(stream, partition, window.start, window.end, cursor),
            "stream": stream,
            "health_stream": health_stream,
            "partition": partition,
            "ordinal": ordinal,
            "window_start": window.start,
            "window_end": window.end,
            "cursor": cursor,
            "allow_unavailable": allow_unavailable,
            "stages": {
                "operation": operation,
                "params": params,
                "fetch_status": "never",
                "parse_status": "never",
                "write_status": "never",
            },
        }

    def _manifest(self, window: FetchWindow, options: dict[str, Any]) -> list[dict[str, Any]]:
        chunks = self._local_windows(window)
        manifest: list[dict[str, Any]] = []
        ordinal = 0

        for item in chunks:
            start_ts, end_ts = int(item.start.timestamp()), int(item.end.timestamp())
            start_ms, end_ms = int(item.start.timestamp() * 1000), int(item.end.timestamp() * 1000)
            manifest.append(self._spec(
                "heart_rate", "minute", item, cursor=start_ts,
                health_stream="heart_rate/minute_endpoint", ordinal=ordinal,
                allow_unavailable=False, operation="fetch_heart_rate",
                params={"cursor": start_ts, "end": end_ts, "limit": HEART_RATE_PAGE_LIMIT, "hr_type": 2},
            )); ordinal += 1
            manifest.append(self._spec(
                "sleep", "sleep", item, health_stream="sleep", ordinal=ordinal,
                allow_unavailable=True, operation="fetch_band_data",
                params={"from_date": item.start_day(), "to_date": item.end_day(), "query_type": "detail"},
            )); ordinal += 1
            manifest.append(self._spec(
                "hrv", "hrv", item, health_stream="hrv", ordinal=ordinal,
                allow_unavailable=True, operation="fetch_hrv",
                params={"start_date": item.start_day(), "end_date": item.end_day()},
            )); ordinal += 1
            manifest.append(self._spec(
                "dense_files", "dense_index", item, health_stream="heart_rate/dense_file", ordinal=ordinal,
                allow_unavailable=True, operation="fetch_file_info_events",
                params={"event_type": "second_heart_rate", "sub_type": "real_data", "from_ms": start_ms, "to_ms": end_ms, "limit": 2000},
            )); ordinal += 1

            daily_specs = (
                ("DailyHealth/summary", "DailyHealth", "summary", "daily_summary"),
                ("Charge/real_data", "Charge", "real_data", "daily_summary/charge_real_data"),
                ("readiness/watch_score", "readiness", "watch_score", "daily_summary/readiness"),
            )
            for partition, event_type, sub_type, health in daily_specs:
                manifest.append(self._spec(
                    "daily_summary", partition, item, health_stream=health, ordinal=ordinal,
                    allow_unavailable=partition != "DailyHealth/summary", operation="fetch_events",
                    params={"event_type": event_type, "sub_type": sub_type, "from_ms": start_ms, "to_ms": end_ms, "limit": 2000, "reverse": True},
                )); ordinal += 1

            wellness_specs = (
                ("all_day_stress", "user", "all_day_stress", None, "wellness/all_day_stress"),
                ("respiratory_rate", "v2", "RespiratoryRate", "real_data", "wellness/respiratory_rate"),
                ("hrv_rmssd", "v2", "HRVRMSSD", "real_data", "wellness/hrv_rmssd"),
                ("lactate_threshold", "v2", "LactateThreshold", "summary", "wellness/lactate_threshold"),
                ("spo2", "user", "blood_oxygen", None, "wellness/spo2_point"),
                ("pai", "user", "PaiHealthInfo", None, "wellness/pai"),
            )
            for label, surface, event_type, sub_type, health in wellness_specs:
                request_start_ms = (
                    start_ms - DAY_MILLISECONDS
                    if label == "all_day_stress" else start_ms
                )
                request_end_ms = (
                    end_ms + DAY_MILLISECONDS
                    if label == "all_day_stress" else end_ms
                )
                manifest.append(self._spec(
                    "wellness", label, item, health_stream=health, ordinal=ordinal,
                    allow_unavailable=True, operation="fetch_wellness",
                    params={
                        "label": label,
                        "surface": surface,
                        "event_type": event_type,
                        "sub_type": sub_type,
                        "from_ms": request_start_ms,
                        "to_ms": request_end_ms,
                    },
                )); ordinal += 1
            for subtype, health in (("odi", "wellness/spo2_odi"), ("osa_event", "wellness/spo2_osa")):
                manifest.append(self._spec(
                    "wellness", f"spo2/{subtype}", item, health_stream=health, ordinal=ordinal,
                    allow_unavailable=True, operation="fetch_user_events_date_string",
                    params={"event_type": "blood_oxygen", "sub_type": subtype, "from_iso": item.start.isoformat().replace("+00:00", "Z"), "to_iso": item.end.isoformat().replace("+00:00", "Z")},
                )); ordinal += 1

        for statistic in ("SPORT_LOAD", "VO2_MAX"):
            manifest.append(self._spec(
                "daily_summary", f"watch/{statistic}", window,
                health_stream=f"daily_summary/{statistic.lower()}", ordinal=ordinal,
                allow_unavailable=True, operation="fetch_watch_statistics",
                params={"statistic": statistic, "start_day": window.start_day(), "end_day": window.end_day(), "limit": 900, "reverse": True},
            )); ordinal += 1

        start_ts, end_ts = int(window.start.timestamp()), int(window.end.timestamp())
        for sport in SPORTS:
            manifest.append(self._spec(
                "workouts", sport, window, cursor=end_ts,
                health_stream="workouts", ordinal=ordinal, allow_unavailable=True,
                operation="fetch_sport_history",
                params={"sport": sport, "start_track_id": start_ts, "stop_track_id": end_ts, "need_sub_data": 1},
            )); ordinal += 1
        manifest.append(self._spec(
            "devices", "inventory", window, health_stream="devices", ordinal=ordinal,
            allow_unavailable=True, operation="fetch_devices", params={},
        ))
        return manifest

    def create_attempt(
        self,
        user_id: str,
        window: FetchWindow | None = None,
        days: int | None = None,
        trigger: str = "manual",
        options: dict[str, Any] | None = None,
        *,
        timezone_name: str = "UTC",
        trigger_ref: str | None = None,
        deadline_at: datetime | None = None,
    ) -> orm.SyncAttempt:
        window = self._window(window, days)
        options = dict(options or {})
        manifest = self._manifest(window, options)
        with self._session() as db:
            repo = HealthRepository(db)
            return repo.create_or_reuse_sync_attempt(
                user_id,
                source="zepp",
                trigger=trigger,
                trigger_ref=trigger_ref,
                plan_version=PLAN_VERSION,
                window_start=window.start,
                window_end=window.end,
                timezone_name=timezone_name,
                options=options,
                deadline_at=deadline_at,
                manifest=manifest,
            )

    def _attempt_snapshot(self, attempt_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            row = HealthRepository(db).sync_attempt(attempt_id)
            if row is None:
                return None
            return {
                "id": row.id, "user_id": row.user_id, "status": row.status,
                "trigger": row.trigger, "created_at": row.created_at,
                "window_start": row.window_start, "window_end": row.window_end,
                "deadline_at": row.deadline_at, "options": dict(row.options or {}),
                "cancel_requested_at": row.cancel_requested_at,
                "lease_token": row.lease_token, "lease_epoch": row.lease_epoch,
            }

    def _cancel_requested(self, attempt_id: str) -> bool:
        with self._session() as db:
            row = HealthRepository(db).sync_attempt(attempt_id)
            return bool(row and row.cancel_requested_at is not None)

    def _control(self, attempt: dict[str, Any]) -> SyncControl:
        return SyncControl(
            deadline_at=attempt["deadline_at"],
            cancel_check=lambda: self._cancel_requested(attempt["id"]),
            wall_clock=self.wall_clock,
            monotonic_clock=self.monotonic_clock,
            slice_seconds=30.0,
        )

    def _connector_for(self, user_id: str) -> tuple[Any, bool]:
        if self.connector is not None:
            if hasattr(self.connector, "fetch_heart_rate"):
                return self.connector, False
            if hasattr(self.connector, "_client_for"):
                with self._session() as db:
                    return self.connector._client_for(
                        HealthRepository(db), User(id=user_id)
                    ), True
            return self.connector, False
        if self.connector_factory is not None:
            factory = self.connector_factory
            try:
                value = factory(user_id)
            except TypeError:
                value = factory()
            return value.connector if isinstance(value, DataFetcher) else value, True
        with self._session() as db:
            auth = HealthRepository(db).get_token(user_id, "zepp")
        if auth is None:
            raise ZeppAuthError("缺少 Zepp 凭据", kind="auth", needs_reauth=True)
        client = ZeppAPIClient(
            app_token=auth.access_token,
            user_id=auth.source_user_id or "me",
            region_host=auth.region_host or "api-mifitcn.zepp.com",
        )
        return client, True

    @staticmethod
    def _set_client_timeout(connector: Any, timeout: float) -> None:
        setter = getattr(connector, "set_timeout", None)
        if setter is not None:
            setter(timeout)

    def _fetch_chunk(self, chunk: dict[str, Any], control: SyncControl, connector: Any) -> _ChunkResult:
        control.check()
        operation = chunk["stages"].get("operation")
        params = dict(chunk["stages"].get("params") or {})
        stream = chunk["stream"]
        start = chunk["window_start"]
        end = chunk["window_end"]

        def call(method: str, *args: Any) -> Any:
            control.check()
            self._set_client_timeout(connector, control.request_timeout())
            return getattr(connector, method)(*args)

        try:
            if operation == "fetch_devices":
                payload = call("fetch_devices")
                return _ChunkResult(FetchedRecord(RawRecord(
                    "devices", "device_inventory", start, end, payload
                )), 1)
            if operation == "fetch_heart_rate":
                payload = call("fetch_heart_rate", int(chunk["cursor"] or params["cursor"]), params["end"], HEART_RATE_PAGE_LIMIT, 2)
                items = _heart_rate_items(payload)
                nxt = None
                if len(items) >= HEART_RATE_PAGE_LIMIT:
                    candidate = _heart_rate_cursor(items)
                    if candidate is not None and candidate > int(chunk["cursor"] or 0) and candidate < params["end"]:
                        nxt = candidate
                return _ChunkResult(FetchedRecord(RawRecord(
                    "heart_rate", f"heart_rate:{int(start.timestamp())}:{int(end.timestamp())}", start, end, payload
                )), len(items), nxt)
            if operation == "fetch_band_data":
                payload = call("fetch_band_data", params["from_date"], params["to_date"], "detail", 8, 0)
                return _ChunkResult(FetchedRecord(RawRecord("sleep", f"band_data:detail:{params['from_date']}:{params['to_date']}", start, end, payload)), 1)
            if operation == "fetch_hrv":
                payload = call("fetch_hrv", params["start_date"], params["end_date"])
                return _ChunkResult(FetchedRecord(RawRecord("hrv", f"events:hrv_sdnn:{params['start_date']}:{params['end_date']}", start, end, payload)), 1)
            if operation == "fetch_file_info_events":
                payload = call("fetch_file_info_events", "second_heart_rate", "real_data", params["from_ms"], params["to_ms"], 2000)
                return _ChunkResult(FetchedRecord(RawRecord("dense_files", f"file_info:second_heart_rate:{start.date()}:{(end - timedelta(microseconds=1)).date()}", start, end, payload, "indexed")), 1)
            if operation == "fetch_events":
                payload = call("fetch_events", params["event_type"], params["sub_type"], params["from_ms"], params["to_ms"], params.get("limit", 2000), params.get("reverse", True))
                key = f"events:{params['event_type']}:{params['sub_type']}:{params['from_ms']}:{params['to_ms']}"
                return _ChunkResult(FetchedRecord(RawRecord("daily_summary", key, start, end, payload)), 1)
            if operation == "fetch_watch_statistics":
                payload = call("fetch_watch_statistics", params["statistic"], params["start_day"], params["end_day"], params.get("limit", 900), params.get("reverse", True))
                key = f"WatchSportStatistics:{params['statistic']}:{params['start_day']}:{params['end_day']}"
                return _ChunkResult(FetchedRecord(RawRecord("daily_summary", key, start, end, payload)), 1)
            if operation == "fetch_wellness":
                if params["surface"] == "user":
                    payload = call("fetch_user_events", params["event_type"], params["sub_type"], params["from_ms"], params["to_ms"], 1000, True)
                else:
                    payload = call("fetch_events", params["event_type"], params["sub_type"] or "real_data", params["from_ms"], params["to_ms"], 1000, True)
                key = f"wellness:{params['label']}:{params['surface']}:{start.date()}:{(end - timedelta(microseconds=1)).date()}"
                return _ChunkResult(FetchedRecord(RawRecord("wellness", key, start, end, payload, "unverified")), 1)
            if operation == "fetch_user_events_date_string":
                payload = call("fetch_user_events_date_string", params["event_type"], params["sub_type"], params["from_iso"], params["to_iso"])
                key = f"wellness:spo2:user_day:{start.date()}:{(end - timedelta(microseconds=1)).date()}:{params['sub_type']}"
                return _ChunkResult(FetchedRecord(RawRecord("wellness", key, start, end, payload, "unverified")), 1)
            if operation == "fetch_sport_history":
                cursor = int(chunk["cursor"] or params["stop_track_id"])
                payload = call("fetch_sport_history", params["sport"], params["start_track_id"], cursor, 1)
                data = payload.get("data") or {} if isinstance(payload, dict) else {}
                nxt = data.get("next")
                next_cursor = None
                if nxt is not None and int(nxt) > params["start_track_id"] and int(nxt) < cursor:
                    next_cursor = int(nxt)
                key = f"sport_history:{params['sport']}:{params['start_track_id']}:{cursor}"
                return _ChunkResult(FetchedRecord(RawRecord("workouts", key, start, end, payload)), len(data.get("items") or []) if isinstance(data, dict) else 0, next_cursor)
            if operation == "fetch_workout_detail":
                payload = call("fetch_sport_detail", params["workout_id"], params["source"])
                key = f"workout_detail:{params['workout_id']}:{params['source']}"
                return _ChunkResult(FetchedRecord(RawRecord("workout_detail", key, start, end, payload)), 1)
            if operation == "fetch_dense_archive":
                archive = call("download_dense_file", params["file_type"], params["file_id"])
                return _ChunkResult(archive=archive, raw_records=1)
            raise ZeppAuthError(f"未知同步操作: {operation}", kind="invalid_request")
        except ZeppAuthError:
            raise
        except Exception as exc:
            raise ZeppAuthError(str(exc), kind="unknown") from exc

    @contextmanager
    def _lease_heartbeat(
        self,
        attempt_id: str,
        attempt_token: str,
        attempt_epoch: int,
        claim: _ClaimedChunk,
    ) -> Iterator[None]:
        """Renew both fencing leases while network/decode/write work is in flight."""
        stop = Event()
        lost = Event()
        interval = max(0.25, min(self.lease_seconds, self.attempt_lease_seconds) / 3)

        def renew() -> None:
            while not stop.wait(interval):
                try:
                    with self._session() as db:
                        repo = HealthRepository(db)
                        attempt_ok = repo.renew_sync_attempt_lease(
                            attempt_id, attempt_token, attempt_epoch,
                            now=self.wall_clock(), lease_seconds=self.attempt_lease_seconds,
                        )
                        chunk_ok = repo.renew_sync_chunk_lease(
                            claim.lease.entity_id, claim.lease.token, claim.lease.epoch,
                            now=self.wall_clock(), lease_seconds=self.lease_seconds,
                        )
                        if attempt_ok and chunk_ok:
                            continue
                        row = db.get(orm.SyncChunk, claim.lease.entity_id)
                        if row is not None and row.status != "running":
                            return
                        lost.set()
                        return
                except Exception:
                    # A concurrent write may briefly lock SQLite. Fencing at finalize
                    # remains authoritative if the next heartbeat cannot recover.
                    continue

        thread = Thread(target=renew, name=f"zepp-sync-lease-{claim.lease.entity_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=interval + 1)
        if lost.is_set():
            raise StaleSyncLease("sync lease ownership was lost during chunk execution")

    def _claim_attempt(self, attempt_id: str) -> tuple[dict[str, Any], str, int] | None:
        token = uuid4().hex
        now = self.wall_clock()
        with self._session() as db:
            repo = HealthRepository(db)
            if not repo.claim_sync_attempt(attempt_id, token, now=now, lease_seconds=self.attempt_lease_seconds):
                return None
            row = repo.sync_attempt(attempt_id)
            assert row is not None
            return ({"id": row.id, "user_id": row.user_id, "status": row.status,
                     "trigger": row.trigger, "created_at": row.created_at,
                     "window_start": row.window_start, "window_end": row.window_end,
                     "deadline_at": row.deadline_at, "options": dict(row.options or {}),
                     "cancel_requested_at": row.cancel_requested_at}, token, row.lease_epoch)

    def _claim_chunk(
        self, attempt_id: str, attempt_token: str, attempt_epoch: int
    ) -> _ClaimedChunk | None:
        now = self.wall_clock()
        naive_now = self._naive(now)
        with self._session() as db:
            repo = HealthRepository(db)
            rows = db.execute(select(orm.SyncChunk).where(
                orm.SyncChunk.attempt_id == attempt_id,
                orm.SyncChunk.status.in_(("queued", "retry_wait", "running")),
                or_(orm.SyncChunk.next_retry_at.is_(None), orm.SyncChunk.next_retry_at <= naive_now),
                or_(orm.SyncChunk.lease_expires_at.is_(None), orm.SyncChunk.lease_expires_at <= naive_now),
            ).order_by(orm.SyncChunk.ordinal, orm.SyncChunk.id).limit(8)).scalars().all()
            for row in rows:
                token = uuid4().hex
                if not repo.claim_sync_chunk(
                    row.id, token, now=now, lease_seconds=self.lease_seconds,
                    attempt_lease_token=attempt_token, attempt_lease_epoch=attempt_epoch,
                ):
                    continue
                claimed = repo.sync_chunk(attempt_id, row.stable_key)
                assert claimed is not None
                return _ClaimedChunk(
                    SyncLease(claimed.id, token, claimed.lease_epoch, claimed.lease_expires_at),
                    {"id": claimed.id, "attempt_id": attempt_id, "stable_key": claimed.stable_key,
                     "stream": claimed.stream, "health_stream": claimed.health_stream,
                     "partition": claimed.partition, "ordinal": claimed.ordinal,
                     "window_start": claimed.window_start.replace(tzinfo=timezone.utc) if claimed.window_start else None,
                     "window_end": claimed.window_end.replace(tzinfo=timezone.utc) if claimed.window_end else None,
                     "cursor": claimed.cursor, "allow_unavailable": claimed.allow_unavailable,
                     "stages": dict(claimed.stages or {}), "attempt_count": claimed.attempt_count,
                     "raw_records": claimed.raw_records, "records_written": claimed.records_written},
                )
        return None

    def _backoff(self, attempt_count: int) -> datetime:
        base = min(1800.0, 30.0 * (2 ** max(0, attempt_count - 1)))
        delay = min(1800.0, base + max(0.0, min(1.0, float(self.random_fn()))) * base)
        return self.wall_clock() + timedelta(seconds=delay)

    @staticmethod
    def _failure_kind(exc: Exception) -> str:
        if isinstance(exc, ZeppAuthError):
            return exc.kind
        if exc.__class__.__name__ == "AuthRequired":
            return "auth"
        return "unknown"

    def _dynamic_specs(self, repo: HealthRepository, attempt: dict[str, Any], chunk: dict[str, Any], result: _ChunkResult) -> list[dict[str, Any]]:
        if result.record is None or chunk["stream"] != "workouts" or result.next_cursor is not None:
            return []
        existing = repo.sync_chunks(attempt["id"])
        known = {row.partition for row in existing if row.stream == "workout_detail"}
        specs: list[dict[str, Any]] = []
        remaining = MAX_DETAIL_CHUNKS - len(known)
        if remaining <= 0:
            return specs

        detail_window = FetchWindow(chunk["window_start"], chunk["window_end"])
        strength_only = False
        refresh_after = None
        if attempt.get("trigger") in {"manual", "morning", "evening"}:
            # The daily health request stays at 1/2 days.  Only the bounded
            # detail refresh looks back 28 days, so old app detail caches are
            # refreshed without expanding every vendor request.  Manual is the
            # real /health/sync entry point used by the Hermes CLI; it must not
            # be relabeled as a scheduled trigger because that would deliver a
            # duplicate report from the terminal callback.
            end_day = date.fromisoformat(detail_window.end_day())
            start_day = end_day - timedelta(days=DETAIL_REFRESH_DAYS - 1)
            detail_window = FetchWindow.local_dates(start_day, end_day)
            refresh_after = attempt.get("created_at")

        known_workout_ids = {
            partition.split(":", 1)[1]
            for partition in known
            if partition.startswith("zepp:")
        }
        pending = repo.pending_workout_details(
            attempt["user_id"],
            detail_window.start,
            detail_window.end,
            limit=remaining,
            source="zepp",
            refresh_after=refresh_after,
            strength_only=strength_only,
            exclude_workout_ids=known_workout_ids,
        )
        for workout in pending:
            if not workout.workout_id or not workout.vendor_source:
                continue
            part = f"zepp:{workout.workout_id}"
            if part in known:
                continue
            params = {
                "workout_id": workout.workout_id,
                "source": workout.vendor_source,
            }
            specs.append(self._spec(
                "workout_detail", part,
                detail_window,
                health_stream="workout_detail",
                ordinal=100000 + len(existing) + len(specs),
                operation="fetch_workout_detail",
                params=params,
            ))
            known.add(part)
        return specs

    def _archive_spec(self, repo: HealthRepository, attempt: dict[str, Any], chunk: dict[str, Any], result: _ChunkResult) -> list[dict[str, Any]]:
        if chunk["stream"] != "dense_files" or not result.record or not attempt["options"].get("decode_dense_files", False):
            return []
        if sum(1 for row in repo.sync_chunks(attempt["id"]) if row.stream == "dense_archive") >= MAX_DENSE_ARCHIVE_CHUNKS:
            return []
        files = ZeppParser().parse_dense_file_index(result.record.raw.payload, "second_heart_rate")
        grouped: dict[tuple[str, str], list[DenseDataFile]] = {}
        for item in files:
            if item.file_id and item.file_type:
                grouped.setdefault((item.file_type, item.file_id), []).append(item)
        ordered = sorted(
            grouped.items(),
            key=lambda entry: max(
                (item.start_utc or datetime.min.replace(tzinfo=timezone.utc))
                for item in entry[1]
            ),
            reverse=True,
        )
        for (file_type, file_id), indexed_files in ordered:
            existing = repo.dense_data_file_group(
                attempt["user_id"], "second_heart_rate", file_id, source="zepp"
            )
            handled = {
                (row.start_utc, row.device_id or "")
                for row in existing if row.parse_status in {"decoded", "no_data"}
            }
            indexed = {
                (self._naive(item.start_utc), item.device_id or "")
                for item in indexed_files
            }
            if indexed and indexed <= handled:
                continue
            params = {"file_type": file_type, "file_id": file_id}
            return [self._spec(
                "dense_archive", f"{file_type}:{file_id}",
                FetchWindow(attempt["window_start"], attempt["window_end"]), cursor=file_id,
                health_stream="heart_rate/dense_archive", ordinal=200000,
                operation="fetch_dense_archive", params=params,
            )]
        return []

    def _finalize_success(self, attempt: dict[str, Any], claim: _ClaimedChunk, result: _ChunkResult, connector: Any) -> bool:
        chunk = claim.chunk
        now = self.wall_clock()
        user = User(id=attempt["user_id"])
        report: StreamReport | None = None
        dense_written = 0
        if result.decoded is not None:
            decoded = result.decoded
            with self._session() as db:
                repo = HealthRepository(db)
                dense_written = repo.save_dense_data_files(decoded.files) + repo.save_metric_samples(decoded.samples)
                stages = {**chunk["stages"], "fetch_status": "success", "parse_status": "success", "write_status": "success"}
                if not repo.finalize_chunk(claim.lease.entity_id, claim.lease.token, claim.lease.epoch, "succeeded", now=now, stages=stages, raw_records=result.raw_records, records_written=dense_written):
                    raise StaleSyncLease("dense archive chunk lease expired before finalize")
                repo.save_sync_stream_state(attempt["user_id"], chunk["health_stream"] or "heart_rate/dense_archive", fetch_status="success", parse_status="success", write_status="success", fetched_at=now, parsed_at=now, written_at=now, raw_records=result.raw_records, records_written=dense_written, attempt_id=attempt["id"])
            return True
        if result.record is not None and chunk["stream"] == "devices":
            devices = ZeppParser().parse_devices(result.record.raw.payload)
            with self._session() as db:
                repo = HealthRepository(db)
                for device in devices:
                    device.user_id = attempt["user_id"]
                    repo.upsert_device(device)
                stages = {
                    **chunk["stages"], "fetch_status": "success",
                    "parse_status": "success", "write_status": "success",
                }
                if not repo.finalize_chunk(
                    claim.lease.entity_id, claim.lease.token, claim.lease.epoch,
                    "succeeded", now=now, stages=stages, raw_records=1,
                    records_written=len(devices),
                ):
                    raise StaleSyncLease("device chunk lease expired before finalize")
                repo.save_sync_stream_state(
                    attempt["user_id"], "devices", fetch_status="success",
                    parse_status="success", write_status="success", fetched_at=now,
                    parsed_at=now, written_at=now, raw_records=1,
                    records_written=len(devices), attempt_id=attempt["id"],
                )
            return True
        if result.record is not None:
            manager = SyncManager(DataFetcher(connector), dense_archive_budget=0)
            with self._session() as db:
                repo = HealthRepository(db)
                report = manager._persist_record(result.record, repo, user)
                stage_status = {
                    **chunk["stages"], "fetch_status": "success",
                    "parse_status": report.parse_status or ("success" if report.status == "success" else "failed"),
                    "write_status": report.write_status or ("success" if report.status == "success" else "not_run"),
                    "capability": report.capability,
                }
                final_status = "succeeded"
                error_kind = report.error_kind
                error = report.message
                if report.status == "failed" or report.error_kind == "unrecognized_payload":
                    final_status = "failed"
                elif report.status == "unavailable":
                    final_status = "unavailable"
                if final_status == "unavailable" and not chunk["allow_unavailable"]:
                    final_status = "failed"
                ok = repo.finalize_chunk(claim.lease.entity_id, claim.lease.token, claim.lease.epoch, final_status, now=now, stages=stage_status, raw_records=max(result.raw_records, report.raw_records), records_written=report.records_written, error_kind=error_kind, error=error)
                if not ok:
                    raise StaleSyncLease("chunk lease expired before finalize")
                repo.save_sync_stream_state(
                    attempt["user_id"], chunk["health_stream"] or chunk["stream"],
                    fetch_status="unavailable" if final_status == "unavailable" else "success",
                    parse_status=stage_status["parse_status"], write_status=stage_status["write_status"],
                    fetched_at=report.fetched_at or now, parsed_at=report.parsed_at, written_at=report.written_at,
                    raw_records=max(result.raw_records, report.raw_records), records_written=report.records_written,
                    error_kind=error_kind, message=error, attempt_id=attempt["id"],
                )
                if final_status == "succeeded" and result.next_cursor is not None:
                    params = dict(chunk["stages"].get("params") or {})
                    params["cursor"] = result.next_cursor
                    successor = self._spec(
                        chunk["stream"], chunk["partition"], FetchWindow(chunk["window_start"], chunk["window_end"]),
                        cursor=result.next_cursor, health_stream=chunk["health_stream"], ordinal=chunk["ordinal"] + 1,
                        allow_unavailable=chunk["allow_unavailable"], operation=chunk["stages"].get("operation", ""), params=params,
                    )
                    repo._ensure_sync_chunks(repo.sync_attempt(attempt["id"]), [successor])
                if final_status == "succeeded":
                    for spec in self._dynamic_specs(repo, attempt, chunk, result) + self._archive_spec(repo, attempt, chunk, result):
                        repo._ensure_sync_chunks(repo.sync_attempt(attempt["id"]), [spec])
                return True
        return False

    def _finalize_error(self, attempt: dict[str, Any], claim: _ClaimedChunk, exc: Exception) -> str:
        kind = self._failure_kind(exc)
        chunk = claim.chunk
        now = self.wall_clock()
        if chunk["stream"] == "devices" and kind != "auth":
            status, retry_at = "unavailable", None
        elif kind in {"network", "service", "timeout"} and claim.chunk["attempt_count"] < MAX_CHUNK_ATTEMPTS:
            status = "retry_wait"
            retry_at = self._backoff(claim.chunk["attempt_count"])
        elif kind == "not_available" and chunk["allow_unavailable"]:
            status, retry_at = "unavailable", None
        else:
            status, retry_at = "failed", None
        stages = {
            **chunk["stages"],
            "fetch_status": "unavailable" if status == "unavailable" else "failed",
            "parse_status": "not_run",
            "write_status": "not_run",
        }
        with self._session() as db:
            repo = HealthRepository(db)
            finalized = repo.finalize_chunk(claim.lease.entity_id, claim.lease.token, claim.lease.epoch, status, now=now, next_retry_at=retry_at, stages=stages, error_kind=kind, error=str(exc))
            if finalized:
                repo.save_sync_stream_state(
                    attempt["user_id"], chunk["health_stream"] or chunk["stream"],
                    fetch_status="unavailable" if status == "unavailable" else "failed",
                    parse_status="not_run", write_status="not_run", fetched_at=now,
                    parsed_at=None, written_at=None, raw_records=0, records_written=0,
                    error_kind=kind, message=str(exc), attempt_id=attempt["id"],
                )
        return status

    def _run_chunk(
        self,
        attempt: dict[str, Any],
        claim: _ClaimedChunk,
        control: SyncControl,
        connector: Any,
        attempt_token: str,
        attempt_epoch: int,
    ) -> str:
        try:
            with self._lease_heartbeat(
                attempt["id"], attempt_token, attempt_epoch, claim
            ):
                control.check()
                result = self._fetch_chunk(claim.chunk, control, connector)
                control.check()
                if claim.chunk["stream"] == "dense_archive" and result.archive is not None:
                    # Fetching is outside a transaction; decoding is also completed before write commit.
                    params = claim.chunk["stages"].get("params") or {}
                    with self._session() as db:
                        indexed = HealthRepository(db).dense_data_file_group(attempt["user_id"], "second_heart_rate", params.get("file_id", ""), source="zepp")
                    files = [DenseDataFile(
                        user_id=attempt["user_id"], source="zepp", stream=row.stream, file_id=row.file_id,
                        file_type=row.file_type, date=row.date, start_utc=row.start_utc.replace(tzinfo=timezone.utc) if row.start_utc else None,
                        end_utc=row.end_utc.replace(tzinfo=timezone.utc) if row.end_utc else None,
                        source_scope=row.source_scope, device_id=row.device_id, parse_status=row.parse_status, sample_count=row.sample_count,
                    ) for row in indexed]
                    result.decoded = decode_sec_hr_archive(result.archive, files)
                    for sample in result.decoded.samples:
                        sample.user_id = attempt["user_id"]
                    for item in result.decoded.files:
                        item.user_id = attempt["user_id"]
                if not self._finalize_success(attempt, claim, result, connector):
                    return "stale"
            return "succeeded"
        except Exception as exc:
            if isinstance(exc, StaleSyncLease):
                return "stale"
            if isinstance(exc, SyncCancelled):
                with self._session() as db:
                    repo = HealthRepository(db)
                    repo.finalize_chunk(claim.lease.entity_id, claim.lease.token, claim.lease.epoch, "cancelled", now=self.wall_clock(), stages={**claim.chunk["stages"], "fetch_status": "cancelled", "parse_status": "not_run", "write_status": "not_run"}, error_kind="cancelled", error=str(exc))
                return "cancelled"
            return self._finalize_error(attempt, claim, exc)

    def _finish_attempt(self, attempt: dict[str, Any], token: str, epoch: int, *, timeout: bool = False) -> str:
        now = self.wall_clock()
        with self._session() as db:
            repo = HealthRepository(db)
            row = repo.sync_attempt(attempt["id"])
            if row is None:
                return "missing"
            if (
                row.status != "running" or row.lease_token != token
                or row.lease_epoch != epoch
            ):
                return "stale"
            chunks = repo.sync_chunks(row.id)
            aggregate = repo.aggregate_sync_attempt(row.id)
            if row.cancel_requested_at is not None:
                return "cancelled" if repo.cancel_sync_attempt(
                    row.id, now=now, lease_token=token, lease_epoch=epoch
                ) else "stale"
            if any(item.status == "failed" for item in chunks):
                auth = any(item.error_kind == "auth" for item in chunks)
                status = "needs_reauth" if auth else "failed"
                ok = repo.finalize_attempt(
                    row.id, token, epoch, status, now=now,
                    error_kind="auth" if auth else next(
                        (item.error_kind for item in chunks if item.error_kind), "unknown"
                    ),
                    error=next((item.error for item in chunks if item.error), None),
                )
                return status if ok else "stale"
            if any(item.status == "retry_wait" for item in chunks):
                next_at = min(
                    item.next_retry_at for item in chunks
                    if item.status == "retry_wait" and item.next_retry_at
                )
                ok = repo.finalize_attempt(
                    row.id, token, epoch, "retry_wait", now=now,
                    next_retry_at=next_at,
                    error_kind=next(
                        (item.error_kind for item in chunks if item.status == "retry_wait"),
                        "network",
                    ),
                    error="等待重试",
                )
                return "retry_wait" if ok else "stale"
            if timeout:
                terminal_status = "partial" if aggregate.completed_count else "failed"
                ok = repo.finalize_attempt(
                    row.id, token, epoch, terminal_status, now=now,
                    error_kind="timeout",
                    error="同步达到 hard deadline；已保留完成的 chunk",
                )
                if not ok:
                    return "stale"
                db.execute(update(orm.SyncChunk).where(
                    orm.SyncChunk.attempt_id == row.id,
                    orm.SyncChunk.status.in_(("queued", "retry_wait")),
                ).values(
                    status="cancelled", next_retry_at=None,
                    finished_at=self._naive(now), error_kind="timeout",
                    error="同步达到 hard deadline，未执行的 chunk 已终止",
                    updated_at=self._naive(now),
                ))
                return terminal_status
            if any(item.status in ("queued", "running") for item in chunks):
                return "queued" if repo.release_attempt_lease(
                    row.id, token, epoch, now=now, status="queued"
                ) else "stale"

            stream_statuses: dict[str, set[str]] = {}
            for item in chunks:
                stream_statuses.setdefault(
                    item.health_stream or item.stream, set()
                ).add(item.status)
            if any({"succeeded", "unavailable"} <= statuses for statuses in stream_statuses.values()):
                ok = repo.finalize_attempt(
                    row.id, token, epoch, "partial", now=now,
                    error_kind="partial_coverage",
                    error="同一数据流同时包含成功与不可用 chunk",
                )
                return "partial" if ok else "stale"
            return "succeeded" if repo.finalize_attempt(
                row.id, token, epoch, "succeeded", now=now
            ) else "stale"

    def run_attempt(self, attempt_id: str, *, max_chunks: int | None = None) -> SyncReport:
        claimed = self._claim_attempt(attempt_id)
        if claimed is None:
            return self.report(attempt_id)
        attempt, token, epoch = claimed
        control = self._control(attempt)
        processed = 0
        terminal: str | None = None
        connector = None
        owned_connector = False
        try:
            while max_chunks is None or processed < max_chunks:
                control.check()
                with self._session() as db:
                    renewed = HealthRepository(db).renew_sync_attempt_lease(
                        attempt_id, token, epoch, now=self.wall_clock(),
                        lease_seconds=self.attempt_lease_seconds,
                    )
                if not renewed:
                    terminal = "stale"
                    break
                claim = self._claim_chunk(attempt_id, token, epoch)
                if claim is None:
                    break
                if connector is None:
                    try:
                        connector, owned_connector = self._connector_for(attempt["user_id"])
                    except Exception as exc:
                        terminal = self._finalize_error(attempt, claim, exc)
                        processed += 1
                        break
                outcome = self._run_chunk(
                    attempt, claim, control, connector, token, epoch
                )
                processed += 1
                if outcome == "stale":
                    terminal = "stale"
                    break
            if terminal != "stale":
                try:
                    terminal = self._finish_attempt(attempt, token, epoch)
                except (SyncCancelled, SyncDeadlineExceeded):
                    terminal = self._finish_attempt(attempt, token, epoch, timeout=True)
        except SyncCancelled:
            terminal = self._finish_attempt(attempt, token, epoch)
        except SyncDeadlineExceeded:
            terminal = self._finish_attempt(attempt, token, epoch, timeout=True)
        finally:
            if owned_connector and connector is not None:
                close = getattr(connector, "close", None)
                if close is not None:
                    close()
        if terminal in {"succeeded", "partial", "failed", "needs_reauth", "cancelled"}:
            self._apply_terminal_side_effect(attempt_id, terminal)
        return self.report(attempt_id)

    def drain_once(self, attempt_id: str | None = None) -> SyncReport | None:
        with self._session() as db:
            if attempt_id is None:
                now = self._naive(self.wall_clock())
                repo = HealthRepository(db)
                cancelled = db.execute(select(orm.SyncAttempt.id).where(
                    orm.SyncAttempt.source == "zepp",
                    orm.SyncAttempt.status == "running",
                    orm.SyncAttempt.cancel_requested_at.is_not(None),
                    orm.SyncAttempt.lease_expires_at <= now,
                ).limit(8)).scalars().all()
                for cancelled_id in cancelled:
                    repo.cancel_sync_attempt(cancelled_id, now=now)

                other = orm.SyncAttempt.__table__.alias("other_due_attempt")
                no_other_running = ~select(other.c.id).where(
                    other.c.user_id == orm.SyncAttempt.user_id,
                    other.c.source == orm.SyncAttempt.source,
                    other.c.status == "running",
                    other.c.id != orm.SyncAttempt.id,
                ).exists()
                row = db.execute(select(orm.SyncAttempt).where(
                    orm.SyncAttempt.source == "zepp",
                    orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                    orm.SyncAttempt.cancel_requested_at.is_(None),
                    or_(orm.SyncAttempt.next_retry_at.is_(None), orm.SyncAttempt.next_retry_at <= now),
                    or_(orm.SyncAttempt.lease_expires_at.is_(None), orm.SyncAttempt.lease_expires_at <= now),
                    no_other_running,
                ).order_by(orm.SyncAttempt.updated_at, orm.SyncAttempt.created_at).limit(1)).scalar_one_or_none()
                if row is None:
                    return None
                attempt_id = row.id
        return self.run_attempt(attempt_id, max_chunks=1)

    def recover_due(self, user_id: str | None = None) -> list[SyncReport]:
        now = self._naive(self.wall_clock())
        with self._session() as db:
            conditions = [
                orm.SyncAttempt.source == "zepp",
                orm.SyncAttempt.status.in_(("queued", "running", "retry_wait")),
                orm.SyncAttempt.cancel_requested_at.is_(None),
                or_(orm.SyncAttempt.next_retry_at.is_(None), orm.SyncAttempt.next_retry_at <= now),
                or_(orm.SyncAttempt.lease_expires_at.is_(None), orm.SyncAttempt.lease_expires_at <= now),
            ]
            if user_id is not None:
                conditions.append(orm.SyncAttempt.user_id == user_id)
            rows = list(db.execute(select(orm.SyncAttempt).where(*conditions).order_by(
                orm.SyncAttempt.updated_at, orm.SyncAttempt.created_at
            )).scalars().all())
        return [self.run_attempt(row.id) for row in rows]

    def _apply_terminal_side_effect(self, attempt_id: str, status: str) -> None:
        """Project a fenced terminal transition onto links and scheduled actions."""
        trigger = ""
        user_id = ""
        with self._session() as db:
            repo = HealthRepository(db)
            attempt = repo.sync_attempt(attempt_id)
            if attempt is None:
                return
            trigger = attempt.trigger
            user_id = attempt.user_id
            ref = attempt.trigger_ref or ""
            pairing_id = ref
            link_digest = None
            if "|" in ref:
                pairing_id, link_digest = ref.split("|", 1)
            elif trigger == "link_refresh" and ref:
                link_digest = ref
            if trigger.startswith("pairing") and pairing_id:
                pairing = repo.pairing_session(pairing_id)
                if pairing is not None:
                    if status == "succeeded":
                        pairing.message = f"Zepp 已连接，首次同步写入 {attempt.records_written} 条记录"
                    elif status == "needs_reauth":
                        pairing.message = "Zepp 登录已失效，请重新登录"
                    elif status == "cancelled":
                        pairing.message = "首次同步已取消"
                    else:
                        pairing.message = "Zepp 已连接，但首次同步不完整，将稍后重试"
                    pairing.sync_attempt_id = attempt_id
            if link_digest is None:
                link = repo.latest_browser_link(attempt.user_id)
                link_digest = link.token_digest if link else None
            if link_digest:
                if status == "succeeded":
                    repo.mark_browser_link_synced(
                        link_digest,
                        f"同步完成，写入 {attempt.records_written} 条记录",
                        attempt_id,
                    )
                elif status == "needs_reauth":
                    repo.mark_browser_link_reauth(link_digest, "Zepp 登录已失效，请重新登录")
                elif status in {"partial", "failed", "cancelled"}:
                    repo.mark_browser_link_sync_failed(link_digest, "数据同步不完整，将稍后重试")

        if status not in {"succeeded", "partial"} or trigger not in {"nightly", "morning", "evening"}:
            return
        try:
            from vitalis.intelligence.service import IntelligenceCommand

            result = IntelligenceCommand().analyze(user_id)
            if trigger in {"morning", "evening"}:
                from vitalis.services.daily_push import deliver_daily_report

                daily = (
                    result.daily.model_dump(mode="json")
                    if hasattr(result.daily, "model_dump") else result.daily
                )
                deliver_daily_report(
                    user_id,
                    os.getenv("PUSHPLUS_TOKEN", ""),
                    daily,
                    period=trigger,
                )
        except Exception:
            log.exception(
                "scheduled post-sync action failed: trigger=%s user=%s attempt=%s",
                trigger, user_id, attempt_id,
            )

    def request_cancel(self, attempt_id: str) -> bool:
        now = self.wall_clock()
        with self._session() as db:
            repo = HealthRepository(db)
            changed = repo.request_sync_cancel(attempt_id, now=now)
            repo.cancel_sync_attempt(attempt_id, now=now)
            return changed

    def public_status(self, attempt_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """Return a user-scoped projection with no lease tokens or raw errors."""
        state = self.status(attempt_id)
        if state is None or (user_id is not None and state["attempt"]["user_id"] != user_id):
            return None
        attempt = {key: value for key, value in state["attempt"].items() if key not in {"trigger_ref", "error", "error_kind"}}
        chunks = [
            {key: value for key, value in row.items() if key not in {"error", "error_kind", "stages"}}
            for row in state["chunks"]
        ]
        return {"attempt": attempt, "progress": state["progress"], "chunks": chunks}

    def status(self, attempt_id: str) -> dict[str, Any] | None:
        with self._session() as db:
            repo = HealthRepository(db)
            attempt = repo.sync_attempt(attempt_id)
            if attempt is None:
                return None
            aggregate = repo.aggregate_sync_attempt(attempt_id)
            chunks = [
                {"id": row.id, "stable_key": row.stable_key, "stream": row.stream, "health_stream": row.health_stream,
                 "partition": row.partition, "ordinal": row.ordinal, "status": row.status,
                 "fetch_status": row.fetch_status, "parse_status": row.parse_status, "write_status": row.write_status,
                 "stages": dict(row.stages or {}), "attempt_count": row.attempt_count,
                 "raw_records": row.raw_records, "records_written": row.records_written,
                 "next_retry_at": row.next_retry_at, "error_kind": row.error_kind, "error": row.error}
                for row in repo.sync_chunks(attempt_id)
            ]
            return {"attempt": {"id": attempt.id, "user_id": attempt.user_id, "status": attempt.status, "trigger": attempt.trigger, "trigger_ref": attempt.trigger_ref, "attempt_count": attempt.attempt_count, "retry_count": attempt.retry_count, "chunk_count": attempt.chunk_count, "completed_count": attempt.completed_count, "raw_records": attempt.raw_records, "records_written": attempt.records_written, "deadline_at": attempt.deadline_at, "next_retry_at": attempt.next_retry_at, "error_kind": attempt.error_kind, "error": attempt.error}, "progress": {**aggregate.as_dict(), "retry": aggregate.retrying_chunks, "retry_count": attempt.retry_count, "next_retry_at": attempt.next_retry_at}, "chunks": chunks}

    def report(self, attempt_id: str) -> SyncReport:
        state = self.status(attempt_id)
        if state is None:
            return SyncReport(False, message="同步尝试不存在")
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in state["chunks"]:
            groups.setdefault(row["health_stream"] or row["stream"], []).append(row)
        streams: list[StreamReport] = []
        for stream, rows in sorted(groups.items()):
            failed = next((row for row in rows if row["status"] == "failed"), None)
            unavailable = all(row["status"] == "unavailable" for row in rows)
            status = "failed" if failed else "unavailable" if unavailable else "success" if all(row["status"] in ("succeeded", "unavailable") for row in rows) else "unverified"
            terminal = all(
                row["status"] in ("succeeded", "unavailable") for row in rows
            )
            fetch_status = (
                "failed" if failed else "unavailable" if unavailable
                else "success" if terminal else "partial"
            )
            capabilities = {
                (row.get("stages") or {}).get("capability")
                for row in rows
                if (row.get("stages") or {}).get("capability")
            }
            streams.append(StreamReport(
                stream=stream, status=status,
                records_written=sum(row["records_written"] or 0 for row in rows),
                raw_records=sum(row["raw_records"] or 0 for row in rows),
                capability=(
                    "unavailable" if status == "unavailable"
                    else "unverified" if "unverified" in capabilities
                    else "verified"
                ),
                needs_reauth=bool(failed and failed["error_kind"] == "auth"),
                message=failed["error"] if failed else None,
                diagnostic_stream=stream,
                fetch_status=fetch_status,
                parse_status=(
                    "failed" if failed else SyncManager._aggregate_stage([
                        row["parse_status"] for row in rows
                    ])
                ),
                write_status=(
                    "failed" if failed else SyncManager._aggregate_stage([
                        row["write_status"] for row in rows
                    ])
                ),
                error_kind=failed["error_kind"] if failed else None,
            ))
        success = state["attempt"]["status"] == "succeeded"
        return SyncReport(success, streams=streams, records_written=sum(item.records_written for item in streams), message=state["attempt"].get("error"), progress=state["progress"])

    get_status = status
    get_report = report
    cancel = request_cancel
    recover_due_attempts = recover_due
    trigger = create_attempt
    kick = run_attempt


# Short aliases make the control surface easy to discover from service imports.
Coordinator = ZeppSyncCoordinator
SyncCoordinator = ZeppSyncCoordinator

__all__ = [
    "Coordinator", "SyncControl", "SyncCoordinator", "ZeppSyncCoordinator",
    "stable_chunk_key", "MAX_CHUNK_ATTEMPTS", "MAX_DETAIL_CHUNKS",
]
