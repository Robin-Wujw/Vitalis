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
from vitalis.connectors.zepp.dense_hr import decode_sec_hr_archive
from vitalis.connectors.zepp.fetcher import DataFetcher, FetchWindow, FetchedRecord, _payload_items
from vitalis.connectors.zepp.parser import ZeppParser
from vitalis.models import (
    ActivityRecord,
    DenseDataFile,
    NormalizedDaily,
    SleepRecord,
    TrainingRecord,
    User,
)
from vitalis.storage import HealthRepository
from vitalis.time import local_day


_user_sync_locks_guard = threading.Lock()
_user_sync_locks: dict[str, threading.Lock] = {}
WORKOUT_DETAIL_BATCH_SIZE = 4
DENSE_ARCHIVE_BATCH_SIZE = 1


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
    diagnostic_stream: str | None = None
    fetch_status: str = "success"
    parse_status: str = "never"
    write_status: str = "never"
    fetched_at: datetime | None = None
    parsed_at: datetime | None = None
    written_at: datetime | None = None
    error_kind: str | None = None
    diagnostics: list["StreamReport"] = field(default_factory=list)


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

    def __init__(
        self,
        fetcher: DataFetcher,
        cancel_event: threading.Event | None = None,
        dense_archive_budget: int = DENSE_ARCHIVE_BATCH_SIZE,
    ):
        self.fetcher = fetcher
        self._cancel = cancel_event or threading.Event()
        self._dense_archive_budget = max(int(dense_archive_budget), 0)
        self._dense_archives_remaining = self._dense_archive_budget

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
        streams: list[StreamReport] = []
        active_stream = "initialization"
        try:
            self._cancel.clear()
            self._dense_archives_remaining = self._dense_archive_budget
            window = FetchWindow.days_back(days)
            self._refresh_devices(repo, user)
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
            active_stream = "heart_rate"
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
            active_stream = "daily_summary"
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
            active_stream = "workouts"
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

            # 4. sleep (optional)
            active_stream = "sleep"
            emit("sleep", 4, 8, "正在同步睡眠")
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

            # 5. hrv (optional)
            active_stream = "hrv"
            emit("hrv", 5, 8, "正在同步心率变异性")
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

            # 6. wellness (optional: stress, SpO2, respiration, PAI, RMSSD)
            active_stream = "wellness"
            emit("wellness", 6, 8, "正在同步压力、血氧等指标")
            check()
            try:
                records = self.fetcher.fetch_wellness_records(window)
                if records:
                    streams.append(self._persist_records("wellness", records, repo, user))
                else:
                    streams.append(StreamReport(
                        stream="wellness", status="unavailable", capability="unavailable",
                        message="当前账号没有返回可选健康指标",
                        diagnostic_stream="wellness", fetch_status="unavailable",
                        fetched_at=datetime.now(timezone.utc), error_kind="not_available",
                    ))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._unavailable_report("wellness", exc))

            # 7. dense_files
            active_stream = "dense_files"
            emit("dense_files", 7, 8, "正在同步高频心率文件索引")
            check()
            try:
                records = self.fetcher.fetch_dense_file_records(window)
                if records:
                    streams.append(self._persist_records("dense_files", records, repo, user))
                else:
                    streams.append(StreamReport(
                        stream="dense_files", status="unavailable", capability="unavailable",
                        message="当前账号没有返回高频心率文件索引",
                        diagnostic_stream="dense_files", fetch_status="unavailable",
                        fetched_at=datetime.now(timezone.utc), error_kind="not_available",
                    ))
            except ZeppAuthError as exc:
                if "取消" in str(exc) or "超时" in str(exc):
                    raise
                streams.append(self._unavailable_report("dense_files", exc))

            # 8. workout_detail is intentionally last: a slow historical detail
            # upgrade must not block sleep, HRV, oxygen, or other daily health data.
            active_stream = "workout_detail"
            emit("workout_detail", 8, 8, "正在同步运动明细")
            try:
                check()
                records = self._fetch_pending_workout_details(
                    window, repo, user, check=check
                )
                if not records:
                    streams.append(
                        StreamReport(
                            stream="workout_detail",
                            status="success",
                            records_written=0,
                            raw_records=0,
                            message="没有待拉取的运动明细",
                            diagnostic_stream="workout_detail",
                            fetch_status="success",
                            parse_status="success",
                            write_status="no_change",
                            fetched_at=datetime.now(timezone.utc),
                            parsed_at=datetime.now(timezone.utc),
                        )
                    )
                else:
                    streams.append(self._persist_records("workout_detail", records, repo, user))
            except ZeppAuthError as exc:
                if "取消" in str(exc):
                    raise
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    streams.append(self._unavailable_report("workout_detail", exc))
                else:
                    streams.append(self._failure_report("workout_detail", exc))

            core_failed = any(
                s.stream in ("heart_rate", "daily_summary", "workouts") and s.status == "failed"
                for s in streams
            )
            success = not core_failed
            total_written = sum(s.records_written for s in streams)
            self._save_stream_health(repo, user, streams)

            return SyncReport(
                success=success,
                streams=streams,
                records_written=total_written,
                message="至少一个核心数据流失败；同步未报告成功" if core_failed else None,
            )
        except ZeppAuthError as exc:
            if not any(
                report.stream == active_stream and report.fetch_status == "failed"
                for report in streams
            ):
                streams.append(self._failure_report(active_stream, exc))
            self._save_stream_health(repo, user, streams)
            if "同步超时" in str(exc):
                return SyncReport(
                    success=False,
                    streams=streams,
                    records_written=sum(item.records_written for item in streams),
                    message="同步达到时间预算；已保存此前完成的数据流，后续将继续增量同步",
                )
            raise
        finally:
            run_lock.release()

    # ---- internal ----

    def _refresh_devices(
        self, repo: HealthRepository | None, user: User
    ) -> None:
        connector = getattr(self.fetcher, "connector", None)
        if repo is None or connector is None or not hasattr(connector, "fetch_devices"):
            return
        try:
            payload = connector.fetch_devices()
        except ZeppAuthError:
            # Device naming enriches provenance but must not block health streams.
            return
        for device in ZeppParser().parse_devices(payload):
            device.user_id = user.id
            repo.upsert_device(device)

    def _fetch_pending_workout_details(
        self,
        window: FetchWindow,
        repo: HealthRepository | None,
        user: User,
        check: Callable[[], None] | None = None,
    ) -> list[FetchedRecord]:
        """从已同步的 workouts 中提取未拉明细的条目。

        Only workouts with a vendor `source` can be used by the detail API.
        """
        if repo is None:
            return []
        pending = repo.pending_workout_details(
            user.id, window.start, window.end, limit=WORKOUT_DETAIL_BATCH_SIZE
        )
        records: list[FetchedRecord] = []
        for workout in pending:
            if check:
                check()
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
        if stream == "dense_files":
            records = list(reversed(records))
        successes = 0
        notices = 0
        for rec in records:
            one = self._persist_record(rec, repo, user)
            aggregate.diagnostics.append(one)
            aggregate.records_written += one.records_written
            aggregate.raw_records += one.raw_records
            if one.status == "success":
                successes += 1
            else:
                notices += 1
                aggregate.status = one.status
                aggregate.capability = one.capability
                aggregate.message = one.message
        if successes > 0:
            aggregate.status = "success"
            aggregate.capability = "verified"
            if notices > 0:
                aggregate.message = f"已解析可用数据；{notices} 个可选响应没有可识别记录"
        aggregate.fetched_at = max(
            (item.fetched_at for item in aggregate.diagnostics if item.fetched_at),
            default=None,
        )
        aggregate.parsed_at = max(
            (item.parsed_at for item in aggregate.diagnostics if item.parsed_at),
            default=None,
        )
        aggregate.written_at = max(
            (item.written_at for item in aggregate.diagnostics if item.written_at),
            default=None,
        )
        aggregate.fetch_status = (
            "success" if any(item.fetch_status == "success" for item in aggregate.diagnostics)
            else "failed"
        )
        aggregate.parse_status = self._aggregate_stage(
            [item.parse_status for item in aggregate.diagnostics]
        )
        aggregate.write_status = self._aggregate_stage(
            [item.write_status for item in aggregate.diagnostics]
        )
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
            diagnostic_stream=self._diagnostic_stream(record),
            fetched_at=datetime.now(timezone.utc),
        )
        if repo is None:
            return report
        try:
            written, parsed = self._write_stream_result(record, repo, user)
            report.records_written = written
            report.parsed_at = datetime.now(timezone.utc)
            if written > 0:
                report.parse_status = "success"
                report.write_status = "success"
                report.written_at = report.parsed_at
            elif parsed > 0:
                report.parse_status = "success"
                report.write_status = "no_change"
            elif _payload_items(record.raw.payload):
                if record.raw.stream in {"dense_files", "workout_detail"}:
                    report.parse_status = "success"
                    report.write_status = "no_change"
                else:
                    report.parse_status = "unrecognized"
                    report.write_status = "not_run"
                    report.status = "unverified"
                    report.error_kind = "unrecognized_payload"
                    report.message = "云端返回了记录，但当前解析器没有产生结构化数据"
            else:
                report.parse_status = "empty"
                report.write_status = "not_run"
        except SQLAlchemyError:
            raise
        except Exception as exc:
            report.status = "failed"
            report.capability = "unavailable"
            report.message = str(exc)
            report.parsed_at = datetime.now(timezone.utc)
            report.parse_status = "failed"
            report.write_status = "not_run"
            report.error_kind = self._error_kind(exc, "parse")
        return report

    def _write_stream(self, record: FetchedRecord, repo: HealthRepository, user: User) -> int:
        """将单条记录写入存储，返回写入条数。"""
        written, _ = self._write_stream_result(record, repo, user)
        return written

    def _write_stream_result(
        self, record: FetchedRecord, repo: HealthRepository, user: User
    ) -> tuple[int, int]:
        """Write one record and return `(written, parsed)` counts."""
        stream = record.raw.stream
        payload = record.raw.payload
        parser = ZeppParser()
        written = 0
        parsed = 0

        if stream == "sleep":
            sleeps, activities = parser.parse_band(payload)
            for day, sleep in sleeps.items():
                sleep.user_id = user.id
                daily = NormalizedDaily(user_id=user.id, date=day, sleep=sleep)
                repo.save_daily(daily)
                written += 1
            # band_data 中同时也含 activity
            for day, act in activities.items():
                act.user_id = user.id
                repo.save_daily(NormalizedDaily(user_id=user.id, date=day, activity=act))
                written += 1
            heart_rate = parser.parse_band_heart_rate(payload)
            parsed = len(sleeps) + len(activities) + len(heart_rate)
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
            parsed = len(workouts)
            for w in workouts:
                w.user_id = user.id
                repo.save_workout(w)
                written += 1
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
                repo.save_daily(NormalizedDaily(user_id=user.id, date=day, training=training))
                written += 1

        elif stream == "workout_detail":
            parts = record.raw.source_key.split(":", 2)
            workout_id = parts[1] if len(parts) >= 2 else ""
            workout = repo.workout(user.id, workout_id) if workout_id else None
            summary_end = None
            if workout and isinstance(workout.data, dict):
                summary_end = parser._parse_datetime_value(workout.data.get("ended_at"))
            detail = parser.parse_workout_detail(payload, summary_end=summary_end)
            parsed = int(bool(workout_id) and detail is not None)
            written = int(bool(workout_id) and detail is not None and repo.save_workout_detail(
                user.id,
                workout_id,
                detail.model_dump(mode="json", exclude={"samples"}),
                samples=detail.samples,
            ))

        elif stream == "heart_rate":
            samples = parser.parse_heart_rate_samples(payload)
            parsed = len(samples)
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
            parsed = len(metrics) + len(charge_samples) + len(readiness_samples)
            for sample in readiness_samples:
                sample.user_id = user.id
            written += repo.save_metric_samples(readiness_samples)

        elif stream == "hrv":
            hrv_samples = parser.parse_hrv_samples(payload, "hrv_sdnn")
            parsed = len(hrv_samples)
            for sample in hrv_samples:
                sample.user_id = user.id
            written += repo.save_metric_samples(hrv_samples)

        elif stream == "wellness":
            metrics, samples = parser.parse_wellness(payload, record.raw.source_key)
            parsed = len(metrics) + len(samples)
            for metric in metrics:
                metric.user_id = user.id
            for sample in samples:
                sample.user_id = user.id
            written = repo.save_daily_metrics(metrics) + repo.save_metric_samples(samples)

        elif stream == "dense_files":
            files = parser.parse_dense_file_index(payload, "second_heart_rate")
            parsed = len(files)
            for item in files:
                item.user_id = user.id
            grouped: dict[tuple[str, str], list] = {}
            for item in files:
                grouped.setdefault((item.file_type, item.file_id), []).append(item)
            written = 0
            ordered_groups = sorted(
                grouped.items(),
                key=lambda item: max(
                    (file.start_utc or datetime.min.replace(tzinfo=timezone.utc))
                    for file in item[1]
                ),
                reverse=True,
            )
            for (file_type, file_id), indexed_files in ordered_groups:
                existing = repo.dense_data_file_group(
                    user.id, "second_heart_rate", file_id
                )
                handled_keys = {
                    (row.start_utc, row.device_id)
                    for row in existing
                    if row.parse_status in {"decoded", "no_data"}
                }
                indexed_keys = {
                    (
                        item.start_utc.astimezone(timezone.utc).replace(tzinfo=None)
                        if item.start_utc and item.start_utc.tzinfo
                        else item.start_utc,
                        item.device_id or "",
                    )
                    for item in indexed_files
                }
                if indexed_keys and indexed_keys <= handled_keys:
                    continue
                if self._dense_archives_remaining <= 0:
                    existing_keys = {
                        (row.start_utc, row.device_id) for row in existing
                    }
                    new_indexes = [
                        item for item in indexed_files
                        if (
                            item.start_utc.astimezone(timezone.utc).replace(tzinfo=None)
                            if item.start_utc and item.start_utc.tzinfo else item.start_utc,
                            item.device_id or "",
                        ) not in existing_keys
                    ]
                    written += repo.save_dense_data_files(new_indexes)
                    continue
                mapping_files = {
                    (row.start_utc, row.device_id): DenseDataFile(
                        user_id=row.user_id,
                        source=row.source,
                        stream=row.stream,
                        file_id=row.file_id,
                        file_type=row.file_type,
                        date=row.date,
                        start_utc=(
                            row.start_utc.replace(tzinfo=timezone.utc)
                            if row.start_utc else None
                        ),
                        end_utc=(
                            row.end_utc.replace(tzinfo=timezone.utc)
                            if row.end_utc else None
                        ),
                        source_scope=row.source_scope,
                        device_id=row.device_id,
                        parse_status=row.parse_status,
                        sample_count=row.sample_count,
                    )
                    for row in existing
                }
                for item in indexed_files:
                    start_key = (
                        item.start_utc.astimezone(timezone.utc).replace(tzinfo=None)
                        if item.start_utc and item.start_utc.tzinfo
                        else item.start_utc
                    )
                    mapping_files[(start_key, item.device_id or "")] = item
                archive = self.fetcher.fetch_dense_file_archive(file_type, file_id)
                self._dense_archives_remaining -= 1
                decoded = decode_sec_hr_archive(archive, list(mapping_files.values()))
                for sample in decoded.samples:
                    sample.user_id = user.id
                # A multi-day backfill can contain millions of samples. Persist and
                # release each archive instead of retaining the whole window in RAM.
                written += repo.save_dense_data_files(decoded.files)
                written += repo.save_metric_samples(decoded.samples)

        return written, parsed

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
            diagnostic_stream=stream,
            fetch_status="failed",
            fetched_at=datetime.now(timezone.utc),
            error_kind=self._error_kind(error, "fetch"),
        )

    def _unavailable_report(self, stream: str, error: Exception) -> StreamReport:
        return StreamReport(
            stream=stream,
            status="unavailable",
            records_written=0,
            raw_records=0,
            capability="unavailable",
            message=str(error),
            diagnostic_stream=stream,
            fetch_status="unavailable",
            fetched_at=datetime.now(timezone.utc),
            error_kind="not_available",
        )

    @staticmethod
    def _aggregate_stage(statuses: list[str]) -> str:
        for status in ("success", "no_change", "failed", "unrecognized", "empty", "not_run", "never"):
            if status in statuses:
                return status
        return "never"

    @staticmethod
    def _diagnostic_stream(record: FetchedRecord) -> str:
        if record.raw.stream == "wellness":
            parts = record.raw.source_key.split(":")
            if len(parts) > 1 and parts[1]:
                if parts[1] == "spo2":
                    subtype = parts[-1] if parts else ""
                    if subtype == "odi":
                        return "wellness/spo2_odi"
                    if subtype == "osa_event":
                        return "wellness/spo2_osa"
                    return "wellness/spo2_point"
                return f"wellness/{parts[1]}"
        if record.raw.stream == "heart_rate":
            return "heart_rate/minute_endpoint"
        if record.raw.stream == "dense_files" and "second_heart_rate" in record.raw.source_key:
            return "heart_rate/dense_file"
        return record.raw.stream

    @staticmethod
    def _error_kind(error: Exception, stage: str) -> str:
        text = str(error).lower()
        if isinstance(error, ZeppAuthError) and error.needs_reauth:
            return "auth"
        if "unavailable" in text or "not found" in text or "404" in text:
            return "not_available"
        if any(value in text for value in ("timeout", "timed out", "network", "connect")):
            return "network"
        return "unrecognized_payload" if stage == "parse" else "unknown"

    def _save_stream_health(
        self,
        repo: HealthRepository | None,
        user: User,
        reports: list[StreamReport],
    ) -> None:
        if repo is None:
            return
        flattened = []
        for report in reports:
            flattened.extend(report.diagnostics)
            # Aggregate states are saved last so same-named child responses do
            # not overwrite the stream-level result.
            flattened.append(report)
        for report in flattened:
            stream = report.diagnostic_stream or report.stream
            repo.save_sync_stream_state(
                user.id,
                stream,
                fetch_status=report.fetch_status,
                parse_status=report.parse_status,
                write_status=report.write_status,
                fetched_at=report.fetched_at,
                parsed_at=report.parsed_at,
                written_at=report.written_at,
                raw_records=report.raw_records,
                records_written=report.records_written,
                error_kind=report.error_kind,
                message=report.message,
            )
