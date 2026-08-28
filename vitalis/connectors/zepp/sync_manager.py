"""Zepp 同步管理器：对齐 ZeppBridge sync/mod.rs。

负责按顺序拉取 8 条数据流、逐流报告、超时控制、可取消。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.exc import SQLAlchemyError

from vitalis.connectors.zepp.client import ZeppAuthError
from vitalis.connectors.zepp.fetcher import DataFetcher, FetchWindow, FetchedRecord, _payload_items
from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.models import ActivityRecord, DailyHealth, SleepRecord, TrainingRecord, User
from vitalis.storage import HealthRepository
from vitalis.time import local_day


_user_sync_locks_guard = threading.Lock()
_user_sync_locks: dict[str, threading.Lock] = {}


def _sync_lock_for_user(user_id: str) -> threading.Lock:
    with _user_sync_locks_guard:
        return _user_sync_locks.setdefault(user_id, threading.Lock())


@dataclass
class StreamReport:
    stream: str
    status: str  # success / failed / unavailable / unverified
    records_written: int = 0
    raw_records: int = 0
    capability: str = "verified"
    needs_reauth: bool = False
    message: str | None = None


@dataclass
class SyncReport:
    success: bool
    streams: list[StreamReport] = field(default_factory=list)
    records_written: int = 0
    message: str | None = None


@dataclass
class SyncProgress:
    stream: str
    current: int
    total: int
    message: str


class SyncManager:
    """同步管理器（同步版本，适配 httpx 同步客户端）。"""

    def __init__(self, fetcher: DataFetcher, cancel_event: threading.Event | None = None):
        self.fetcher = fetcher
        self._cancel = cancel_event or threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def sync_report(
        self,
        user: User,
        days: int,
        repo: HealthRepository | None = None,
        on_progress: Callable[[SyncProgress], None] | None = None,
    ) -> SyncReport:
        """执行一次完整同步，返回逐流报告。"""
        run_lock = _sync_lock_for_user(user.id)
        run_lock.acquire()
        try:
            self._cancel.clear()
            window = FetchWindow.days_back(days)
            streams: list[StreamReport] = []
            started = time.monotonic()
            budget = 90 if days <= 7 else min(45 + days * 3, 20 * 60)
            deadline = started + budget

            def emit(stream: str, current: int, total: int, message: str) -> None:
                if on_progress:
                    on_progress(SyncProgress(stream=stream, current=current, total=total, message=message))

            def check() -> None:
                if self._cancel.is_set():
                    raise ZeppAuthError("同步已取消")
                if time.monotonic() > deadline:
                    raise ZeppAuthError("同步超时，已停止后续请求")

            # 1. heart_rate (core)
            emit("heart_rate", 1, 8, "正在同步心率")
            check()
            try:
                records = self.fetcher.fetch_heart_rate_records(window)
                streams.append(self._persist_records("heart_rate", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._failure_report("heart_rate", exc))

            # 2. daily_summary (core)
            emit("daily_summary", 2, 8, "正在同步每日概览")
            check()
            try:
                records = self.fetcher.fetch_daily_statistics_records(window)
                streams.append(self._persist_records("daily_summary", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._failure_report("daily_summary", exc))

            # 3. workouts (core)
            emit("workouts", 3, 8, "正在同步运动")
            check()
            try:
                records = self.fetcher.fetch_workout_records(window)
                streams.append(self._persist_records("workouts", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    streams.append(self._unavailable_report("workouts", exc))
                else:
                    streams.append(self._failure_report("workouts", exc))

            # 4. workout_detail (从 workouts 中提取 pending)
            emit("workout_detail", 4, 8, "正在同步运动明细")
            check()
            try:
                records = self._fetch_pending_workout_details(window, repo, user)
                if not records:
                    streams.append(
                        StreamReport(
                            stream="workout_detail",
                            status="success",
                            records_written=0,
                            raw_records=0,
                            message="没有待拉取的运动明细",
                        )
                    )
                else:
                    streams.append(self._persist_records("workout_detail", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    streams.append(self._unavailable_report("workout_detail", exc))
                else:
                    streams.append(self._failure_report("workout_detail", exc))

            # 5. sleep (optional)
            emit("sleep", 5, 8, "正在同步睡眠")
            check()
            try:
                records = self.fetcher.fetch_sleep_records(window)
                streams.append(self._persist_records("sleep", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    streams.append(self._unavailable_report("sleep", exc))
                else:
                    streams.append(self._failure_report("sleep", exc))

            # 6. hrv (optional)
            emit("hrv", 6, 8, "正在同步心率变异性")
            check()
            try:
                records = self.fetcher.fetch_hrv_records(window)
                streams.append(self._persist_records("hrv", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    streams.append(self._unavailable_report("hrv", exc))
                else:
                    streams.append(self._failure_report("hrv", exc))

            # 7. wellness (optional: stress, SpO2, respiration, PAI, RMSSD)
            emit("wellness", 7, 8, "正在同步压力、血氧等指标")
            check()
            try:
                records = self.fetcher.fetch_wellness_records(window)
                if records:
                    streams.append(self._persist_records("wellness", records, repo, user))
                else:
                    streams.append(StreamReport(
                        stream="wellness", status="unavailable", capability="unavailable",
                        message="当前账号没有返回可选健康指标",
                    ))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._unavailable_report("wellness", exc))

            # 8. dense_files (metadata only until a file payload is verified)
            emit("dense_files", 8, 8, "正在同步高频心率文件索引")
            check()
            try:
                records = self.fetcher.fetch_dense_file_records(window)
                if records:
                    streams.append(self._persist_records("dense_files", records, repo, user))
                else:
                    streams.append(StreamReport(
                        stream="dense_files", status="unavailable", capability="unavailable",
                        message="当前账号没有返回高频心率文件索引",
                    ))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._unavailable_report("dense_files", exc))

            core_failed = any(
                s.stream in ("heart_rate", "daily_summary", "workouts") and s.status == "failed"
                for s in streams
            )
            success = not core_failed
            total_written = sum(s.records_written for s in streams)

            return SyncReport(
                success=success,
                streams=streams,
                records_written=total_written,
                message="至少一个核心数据流失败；同步未报告成功" if core_failed else None,
            )
        finally:
            run_lock.release()

    # ---- internal ----

    def _fetch_pending_workout_details(
        self, window: FetchWindow, repo: HealthRepository | None, user: User
    ) -> list[FetchedRecord]:
        """从已同步的 workouts 中提取未拉明细的条目。

        Only workouts with a vendor `source` can be used by the detail API.
        """
        if repo is None:
            return []
        pending = repo.pending_workout_details(user.id, window.start, window.end)
        records: list[FetchedRecord] = []
        for workout in pending:
            if not workout.vendor_source:
                continue
            records.append(self.fetcher.fetch_workout_detail(
                workout.workout_id,
                workout.vendor_source,
                workout.started_at or window.start,
                None,
            ))
        return records

    def _persist_records(
        self, stream: str, records: list[FetchedRecord], repo: HealthRepository | None, user: User
    ) -> StreamReport:
        aggregate = StreamReport(stream=stream, status="success", records_written=0, raw_records=0)
        successes = 0
        notices = 0
        for rec in records:
            one = self._persist_record(rec, repo, user)
            aggregate.records_written += one.records_written
            aggregate.raw_records += one.raw_records
            if one.status == "success":
                successes += 1
            else:
                notices += 1
                aggregate.status = one.status
                aggregate.capability = one.capability
                aggregate.message = one.message
        if successes > 0 and aggregate.records_written > 0:
            aggregate.status = "success"
            aggregate.capability = "verified"
            if notices > 0:
                aggregate.message = f"已解析可用数据；{notices} 个可选响应没有可识别记录"
        return aggregate

    def _persist_record(
        self, record: FetchedRecord, repo: HealthRepository | None, user: User
    ) -> StreamReport:
        report = StreamReport(
            stream=record.raw.stream,
            status="success",
            records_written=0,
            raw_records=1,
            capability=record.raw.capability,
        )
        if repo is None:
            return report
        try:
            written = self._write_stream(record, repo, user)
            report.records_written = written
        except SQLAlchemyError:
            raise
        except Exception as exc:
            report.status = "failed"
            report.capability = "unavailable"
            report.message = str(exc)
        return report

    def _write_stream(self, record: FetchedRecord, repo: HealthRepository, user: User) -> int:
        """将单条记录写入存储，返回写入条数。"""
        stream = record.raw.stream
        payload = record.raw.payload
        parser = ZeppParser()
        written = 0

        if stream == "sleep":
            sleeps, activities = parser.parse_band(payload)
            for day, sleep in sleeps.items():
                sleep.user_id = user.id
                daily = DailyHealth(user_id=user.id, date=day, sleep=sleep)
                repo.save_daily(daily)
                written += 1
            # band_data 中同时也含 activity
            for day, act in activities.items():
                act.user_id = user.id
                existing = repo.health_daily(user.id, day)
                if existing:
                    # 更新已有记录的 activity
                    repo.save_daily(DailyHealth(user_id=user.id, date=day, activity=act))
                else:
                    repo.save_daily(DailyHealth(user_id=user.id, date=day, activity=act))
                written += 1
            heart_rate = parser.parse_band_heart_rate(payload)
            for sample in heart_rate:
                sample.user_id = user.id
            written += repo.save_metric_samples(heart_rate)

        elif stream == "workouts":
            parts = record.raw.source_key.split(":", 2)
            sport_hint = parts[1] if len(parts) >= 2 else ""
            workouts = parser.parse_sport_history(payload, sport_hint=sport_hint)
            # 按天聚合写入 training
            from collections import defaultdict
            by_day: dict = defaultdict(list)
            for w in workouts:
                w.user_id = user.id
                repo.save_workout(w)
                if w.started_at:
                    by_day[local_day(w.started_at)].append(w)
            for day, ws in by_day.items():
                training = TrainingRecord(
                    user_id=user.id,
                    date=day,
                    workout_count=len(ws),
                    total_duration=sum(w.duration for w in ws),
                    total_load=sum(w.load for w in ws),
                )
                repo.save_daily(DailyHealth(user_id=user.id, date=day, training=training))
                written += 1

        elif stream == "workout_detail":
            parts = record.raw.source_key.split(":", 2)
            workout_id = parts[1] if len(parts) >= 2 else ""
            workout = repo.workout(user.id, workout_id) if workout_id else None
            summary_end = None
            if workout and isinstance(workout.data, dict):
                summary_end = parser._parse_datetime_value(workout.data.get("ended_at"))
            samples = parser.parse_workout_heart_rate(payload, summary_end=summary_end)
            detail = {
                "sample_count": len(samples),
                "sampling": "second_level" if samples else "unavailable",
                "heart_rate_source": "unknown",
            }
            written = int(bool(workout_id) and repo.save_workout_detail(
                user.id, workout_id, detail, samples=samples
            ))

        elif stream == "heart_rate":
            samples = parser.parse_heart_rate_samples(payload)
            for sample in samples:
                sample.user_id = user.id
            written = repo.save_metric_samples(samples)

        elif stream == "daily_summary":
            # Store vendor daily metrics independently from computed Vitalis scores.
            metrics = parser.parse_daily_metrics(payload)
            for metric in metrics:
                metric.user_id = user.id
            written += repo.save_daily_metrics(metrics)
            charge_samples = parser.parse_charge_samples(payload)
            for sample in charge_samples:
                sample.user_id = user.id
            written += repo.save_metric_samples(charge_samples)
            readiness_samples = parser.parse_readiness_samples(payload)
            for sample in readiness_samples:
                sample.user_id = user.id
            written += repo.save_metric_samples(readiness_samples)
            hrv_map = parser.parse_hrv_events(payload)
            for day in hrv_map:
                existing = repo.health_daily(user.id, day)
                daily = DailyHealth(user_id=user.id, date=day)
                if existing:
                    daily.hrv = existing.hrv
                    daily.recovery_score = existing.recovery_score
                if day in hrv_map:
                    daily.hrv = hrv_map[day]
                repo.save_daily(daily)
                written += 1

        elif stream == "hrv":
            hrv_samples = parser.parse_hrv_samples(payload, "hrv_sdnn")
            for sample in hrv_samples:
                sample.user_id = user.id
            written += repo.save_metric_samples(hrv_samples)
            hrv_map = parser.parse_hrv_events(payload)
            for day, val in hrv_map.items():
                existing = repo.health_daily(user.id, day)
                daily = DailyHealth(user_id=user.id, date=day, hrv=val)
                if existing:
                    daily.recovery_score = existing.recovery_score
                repo.save_daily(daily)
                written += 1

        elif stream == "wellness":
            metrics, samples = parser.parse_wellness(payload, record.raw.source_key)
            for metric in metrics:
                metric.user_id = user.id
            for sample in samples:
                sample.user_id = user.id
            written = repo.save_daily_metrics(metrics) + repo.save_metric_samples(samples)

        elif stream == "dense_files":
            files = parser.parse_dense_file_index(payload, "second_heart_rate")
            for item in files:
                item.user_id = user.id
            written = repo.save_dense_data_files(files)

        return written

    def _failure_report(self, stream: str, error: Exception) -> StreamReport:
        msg = str(error)
        needs_reauth = isinstance(error, ZeppAuthError) and error.needs_reauth
        return StreamReport(
            stream=stream,
            status="failed",
            records_written=0,
            raw_records=0,
            capability="unavailable",
            needs_reauth=needs_reauth,
            message=msg,
        )

    def _unavailable_report(self, stream: str, error: Exception) -> StreamReport:
        return StreamReport(
            stream=stream,
            status="unavailable",
            records_written=0,
            raw_records=0,
            capability="unavailable",
            message=str(error),
        )
