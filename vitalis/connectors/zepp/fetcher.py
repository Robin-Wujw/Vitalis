"""Zepp 数据获取器：对齐 ZeppBridge fetcher/mod.rs。

支持：
  - 窗口分块（默认 7 天，避免单请求过大）
  - 心率分页（cursor 翻页）
  - 运动历史游标翻页（13 种运动类型）
  - 2 年最大窗口（730 天）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from vitalis.connectors.zepp.client import ZeppAPIClient, ZeppAuthError
from vitalis.time import local_day, local_day_utc_bounds

MAX_SYNC_DAYS = 730  # 2 年
CHUNK_DAYS = 7
HEART_RATE_PAGE_LIMIT = 1000


@dataclass
class FetchWindow:
    """时间窗口，支持切成 N 天小块。"""

    start: datetime
    end: datetime

    @classmethod
    def days_back(cls, days: int) -> "FetchWindow":
        if not 1 <= days <= MAX_SYNC_DAYS:
            raise ZeppAuthError(
                f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间",
                kind="invalid_request",
            )
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return cls(start=start, end=end)

    @classmethod
    def local_dates(cls, start: date, end: date) -> "FetchWindow":
        if start > end:
            start, end = end, start
        days = (end - start).days + 1
        if days > MAX_SYNC_DAYS:
            raise ZeppAuthError(
                f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间",
                kind="invalid_request",
            )
        start_utc, _ = local_day_utc_bounds(start)
        _, end_utc = local_day_utc_bounds(end)
        return cls(start=start_utc, end=end_utc)

    def start_day(self) -> str:
        return local_day(self.start).isoformat()

    def end_day(self) -> str:
        return local_day(self.end - timedelta(microseconds=1)).isoformat()

    def chunks(self, chunk_days: int = CHUNK_DAYS) -> list["FetchWindow"]:
        chunk_days = max(1, chunk_days)
        chunks: list[FetchWindow] = []
        cursor = self.start
        while cursor < self.end:
            nxt = min(cursor + timedelta(days=chunk_days), self.end)
            if nxt > cursor:
                chunks.append(FetchWindow(start=cursor, end=nxt))
            cursor = nxt
        if not chunks:
            chunks.append(self)
        return chunks


@dataclass
class RawRecord:
    """单条拉取原始记录（对齐 ZeppBridge FetchedRecord.raw）。"""

    stream: str
    source_key: str
    start_utc: datetime
    end_utc: datetime | None
    payload: dict
    capability: str = "verified"


@dataclass
class FetchedRecord:
    """拉取结果。"""

    raw: RawRecord


class FetchBatch(list[FetchedRecord]):
    """Fetched records plus in-memory coverage for one logical stream."""

    def __init__(self, *, expected_chunks: int = 0):
        super().__init__()
        self.expected_chunks = expected_chunks
        self.successful_chunks = 0
        self.unavailable_ranges: list[tuple[datetime, datetime]] = []
        self.unavailable_capabilities: list[tuple[str, str]] = []

    @property
    def unavailable_chunks(self) -> int:
        return len(self.unavailable_ranges)

    @property
    def partial(self) -> bool:
        return self.successful_chunks > 0 and self.unavailable_chunks > 0

    def add_success(self, record: FetchedRecord) -> None:
        self.append(record)
        self.successful_chunks += 1

    def add_unavailable(self, window: FetchWindow) -> None:
        self.unavailable_ranges.append((window.start, window.end))

    def add_unavailable_capability(self, name: str, message: str) -> None:
        self.unavailable_capabilities.append((name, message))


class PartialFetchError(ZeppAuthError):
    """A terminal fetch error after earlier chunks completed."""

    def __init__(self, error: ZeppAuthError, records: list[FetchedRecord]):
        super().__init__(
            str(error), kind=error.kind, needs_reauth=error.needs_reauth
        )
        self.records = records


def _heart_rate_items(payload: dict) -> list[dict]:
    """从心率响应中提取样本列表（防御多种 payload 形状）。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "records", "results", "list"):
        arr = payload.get(key)
        if isinstance(arr, list):
            return arr
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "records", "results", "list"):
            arr = data.get(key)
            if isinstance(arr, list):
                return arr
    return []


def _heart_rate_cursor(items: list[dict]) -> int | None:
    """从已合并的 items 中计算下一页 cursor（max timestamp + 1s）。"""
    max_ts: int | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        item_ts: int | None = None
        candidates = [item]
        if isinstance(item.get("value"), dict):
            candidates.append(item["value"])
        for candidate in candidates:
            for key in (
                "timestamp", "time", "timeStamp", "startTime", "generatedTime"
            ):
                val = candidate.get(key)
                if val is None:
                    continue
                if isinstance(val, (int, float)):
                    item_ts = int(val)
                    break
                try:
                    item_ts = int(str(val).strip())
                    break
                except ValueError:
                    continue
            if item_ts is not None:
                break
        if item_ts is not None:
            max_ts = item_ts if max_ts is None else max(max_ts, item_ts)
    if max_ts is None:
        return None
    # +1 秒（秒级）或 +1000 毫秒（毫秒级）
    return max_ts + (1000 if max_ts >= 10_000_000_000 else 1)


def _payload_items(payload: dict) -> list[dict]:
    """提取 payload 中的结构化记录列表。"""
    if not isinstance(payload, dict):
        return []
    for key in ("items", "records", "results", "list", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for inner_key in ("items", "records", "results", "list", "summary"):
                inner = val.get(inner_key)
                if isinstance(inner, list):
                    return inner
    return []


class DataFetcher:
    """Zepp 数据获取器（翻译自 Rust DataFetcher）。"""

    def __init__(self, connector: ZeppAPIClient):
        self.connector = connector

    # ---- heart_rate ----

    def fetch_heart_rate_records(self, window: FetchWindow) -> list[FetchedRecord]:
        chunks = window.chunks(CHUNK_DAYS)
        records = FetchBatch(expected_chunks=len(chunks))
        last_error: Exception | None = None
        for chunk in chunks:
            try:
                records.add_success(self._fetch_heart_rate_record(chunk))
            except ZeppAuthError as exc:
                if exc.kind == "not_available":
                    last_error = exc
                    records.add_unavailable(chunk)
                    continue
                if records:
                    raise PartialFetchError(exc, records) from exc
                raise
        if not records:
            raise last_error or ZeppAuthError("心率窗口没有可识别记录")
        return records

    def _fetch_heart_rate_record(self, window: FetchWindow) -> FetchedRecord:
        end = int(window.end.timestamp())
        cursor = int(window.start.timestamp())
        merged: list[dict] = []
        payload: dict = {}
        while True:
            payload = self.connector.fetch_heart_rate(
                cursor, end, HEART_RATE_PAGE_LIMIT, 2
            )
            items = _heart_rate_items(payload)
            page_len = len(items)
            merged.extend(items)
            if page_len < HEART_RATE_PAGE_LIMIT:
                break
            nxt = _heart_rate_cursor(merged)
            if nxt is None or nxt <= cursor:
                break
            cursor = nxt
        out_payload: dict
        if not merged:
            # Preserve the successful empty response; do not refetch the same chunk.
            out_payload = payload
        else:
            out_payload = {"items": merged}
        return FetchedRecord(
            raw=RawRecord(
                stream="heart_rate",
                source_key=f"heart_rate:{int(window.start.timestamp())}:{int(window.end.timestamp())}",
                start_utc=window.start,
                end_utc=window.end,
                payload=out_payload,
            )
        )

    # ---- sleep ----

    def fetch_sleep_records(self, window: FetchWindow) -> list[FetchedRecord]:
        chunks = window.chunks(CHUNK_DAYS)
        records = FetchBatch(expected_chunks=len(chunks))
        last_error: Exception | None = None
        for chunk in chunks:
            try:
                records.add_success(self._fetch_sleep_record(chunk))
            except ZeppAuthError as exc:
                if exc.kind == "not_available":
                    last_error = exc
                    records.add_unavailable(chunk)
                    continue
                if records:
                    raise PartialFetchError(exc, records) from exc
                raise
        if not records:
            raise last_error or ZeppAuthError("睡眠窗口没有可识别记录")
        return records

    def _fetch_sleep_record(self, window: FetchWindow) -> FetchedRecord:
        payload = self.connector.fetch_band_data(
            window.start_day(), window.end_day(), "detail", 8, 0
        )
        from vitalis.connectors.zepp.parser import ZeppParser

        # 防御性提取第一个 item 的 summary
        data = payload.get("data") if isinstance(payload, dict) else {}
        items = data.get("items") if isinstance(data, dict) else []
        first_summary = items[0].get("summary") if isinstance(items, list) and items else None
        capability = "verified" if ZeppParser._base64_summary({"summary": first_summary}) is not None else "unverified"
        return FetchedRecord(
            raw=RawRecord(
                stream="sleep",
                source_key=f"band_data:detail:{window.start_day()}:{window.end_day()}",
                start_utc=window.start,
                end_utc=window.end,
                payload=payload,
                capability=capability,
            )
        )

    # ---- workouts ----

    def fetch_workout_records(self, window: FetchWindow) -> list[FetchedRecord]:
        start_ts = int(window.start.timestamp())
        end_ts = int(window.end.timestamp())
        from vitalis.connectors.zepp.client import SPORTS

        records = FetchBatch(expected_chunks=len(SPORTS))
        last_error: Exception | None = None
        for sport in SPORTS:
            stop_track_id = end_ts
            sport_available = False
            while True:
                try:
                    payload = self.connector.fetch_sport_history(
                        sport, start_ts, stop_track_id, 1
                    )
                except ZeppAuthError as exc:
                    if exc.kind == "not_available":
                        last_error = exc
                        records.add_unavailable(window)
                        break
                    if records:
                        raise PartialFetchError(exc, records) from exc
                    raise
                sport_available = True
                data = payload.get("data") or {}
                nxt = data.get("next")
                records.append(
                    FetchedRecord(
                        raw=RawRecord(
                            stream="workouts",
                            source_key=f"sport_history:{sport}:{start_ts}:{stop_track_id}",
                            start_utc=window.start,
                            end_utc=window.end,
                            payload=payload,
                        )
                    )
                )
                if nxt is None or int(nxt) <= 0 or int(nxt) >= stop_track_id or int(nxt) <= start_ts:
                    break
                stop_track_id = int(nxt)
            if sport_available:
                records.successful_chunks += 1
        if not records:
            raise last_error or ZeppAuthError(
                "sport history 没有可用种类",
                kind="not_available",
            )
        return records

    # ---- hrv ----

    def fetch_hrv_records(self, window: FetchWindow) -> list[FetchedRecord]:
        chunks = window.chunks(CHUNK_DAYS)
        records = FetchBatch(expected_chunks=len(chunks))
        last_error: Exception | None = None
        for chunk in chunks:
            try:
                payload = self.connector.fetch_hrv(chunk.start_day(), chunk.end_day())
                records.add_success(
                    FetchedRecord(
                        raw=RawRecord(
                            stream="hrv",
                            source_key=f"events:hrv_sdnn:{chunk.start_day()}:{chunk.end_day()}",
                            start_utc=chunk.start,
                            end_utc=chunk.end,
                            payload=payload,
                        )
                    )
                )
            except ZeppAuthError as exc:
                if exc.kind == "not_available":
                    last_error = exc
                    records.add_unavailable(chunk)
                    continue
                if records:
                    raise PartialFetchError(exc, records) from exc
                raise
        if not records:
            raise last_error or ZeppAuthError("HRV 窗口没有可识别记录")
        return records

    # ---- daily statistics ----

    def fetch_daily_statistics_records(self, window: FetchWindow) -> list[FetchedRecord]:
        chunks = window.chunks(CHUNK_DAYS)
        records = FetchBatch(expected_chunks=len(chunks))
        for chunk in chunks:
            from_ms = int(chunk.start.timestamp() * 1000)
            to_ms = int(chunk.end.timestamp() * 1000)
            try:
                event = self.connector.fetch_events(
                    "DailyHealth", "summary", from_ms, to_ms, 2000, True
                )
            except ZeppAuthError as exc:
                if records:
                    raise PartialFetchError(exc, records) from exc
                raise
            records.add_success(FetchedRecord(raw=RawRecord(
                stream="daily_summary",
                source_key=f"events:DailyHealth:summary:{from_ms}:{to_ms}",
                start_utc=chunk.start,
                end_utc=chunk.end,
                payload=event,
            )))
            for event_type, sub_type in (
                ("Charge", "real_data"),
                ("Charge", "stress_data"),
                ("Charge", "insight_data"),
                ("readiness", "watch_score"),
            ):
                diagnostic = f"daily_summary/{event_type.lower()}_{sub_type}"
                try:
                    payload = self.connector.fetch_events(
                        event_type, sub_type, from_ms, to_ms, 2000, True
                    )
                    records.append(FetchedRecord(raw=RawRecord(
                        stream="daily_summary",
                        source_key=f"events:{event_type}:{sub_type}:{from_ms}:{to_ms}",
                        start_utc=chunk.start,
                        end_utc=chunk.end,
                        payload=payload,
                    )))
                except ZeppAuthError as exc:
                    if exc.kind == "not_available":
                        records.add_unavailable_capability(diagnostic, str(exc))
                        continue
                    raise PartialFetchError(exc, records) from exc
        # WatchSportStatistics: SPORT_LOAD / VO2_MAX
        for statistic in ("SPORT_LOAD", "VO2_MAX"):
            diagnostic = f"daily_summary/{statistic.lower()}"
            try:
                payload = self.connector.fetch_watch_statistics(
                    statistic, window.start_day(), window.end_day(), 900, True
                )
                records.append(
                    FetchedRecord(
                        raw=RawRecord(
                            stream="daily_summary",
                            source_key=f"WatchSportStatistics:{statistic}:{window.start_day()}:{window.end_day()}",
                            start_utc=window.start,
                            end_utc=window.end,
                            payload=payload,
                        )
                    )
                )
            except ZeppAuthError as exc:
                if exc.kind == "not_available":
                    records.add_unavailable_capability(diagnostic, str(exc))
                    continue
                raise PartialFetchError(exc, records) from exc
        return records

    # ---- optional wellness metrics ----

    def fetch_wellness_records(self, window: FetchWindow) -> list[FetchedRecord]:
        """Fetch independently optional wellness streams documented by ZeppBridge."""
        specs = (
            ("all_day_stress", "user", "all_day_stress", None, None),
            ("respiratory_rate", "v2", "RespiratoryRate", "real_data", None),
            ("hrv_rmssd", "v2", "HRVRMSSD", "real_data", None),
            ("lactate_threshold", "v2", "LactateThreshold", "summary", None),
            ("spo2", "user", "blood_oxygen", None, CHUNK_DAYS),
            ("pai", "user", "PaiHealthInfo", None, None),
        )
        records = FetchBatch()

        def merge_capability(batch: FetchBatch) -> None:
            records.extend(batch)
            if batch.partial:
                records.expected_chunks += batch.expected_chunks
                records.successful_chunks += batch.successful_chunks
                records.unavailable_ranges.extend(batch.unavailable_ranges)

        for label, surface, event_type, sub_type, chunk_days in specs:
            chunks = window.chunks(chunk_days or CHUNK_DAYS)
            capability = FetchBatch(expected_chunks=len(chunks))
            for chunk in chunks:
                from_ms = int(chunk.start.timestamp() * 1000)
                to_ms = int(chunk.end.timestamp() * 1000)
                try:
                    if surface == "user":
                        payload = self.connector.fetch_user_events(
                            event_type, sub_type, from_ms, to_ms, 1000, True
                        )
                    else:
                        payload = self.connector.fetch_events(
                            event_type, sub_type or "real_data", from_ms, to_ms, 1000, True
                        )
                except ZeppAuthError as exc:
                    # Each wellness capability is optional; only an incomplete range
                    # within one capability is partial coverage.
                    if exc.kind == "not_available":
                        capability.add_unavailable(chunk)
                        continue
                    records.extend(capability)
                    if records:
                        raise PartialFetchError(exc, records) from exc
                    raise
                capability.add_success(FetchedRecord(raw=RawRecord(
                    stream="wellness",
                    source_key=f"wellness:{label}:{surface}:{chunk.start_day()}:{chunk.end_day()}",
                    start_utc=chunk.start,
                    end_utc=chunk.end,
                    payload=payload,
                    capability="unverified",
                )))
            merge_capability(capability)
        for sub_type in ("odi", "osa_event"):
            chunks = window.chunks(CHUNK_DAYS)
            capability = FetchBatch(expected_chunks=len(chunks))
            for chunk in chunks:
                try:
                    payload = self.connector.fetch_user_events_date_string(
                        "blood_oxygen",
                        sub_type,
                        chunk.start.isoformat().replace("+00:00", "Z"),
                        chunk.end.isoformat().replace("+00:00", "Z"),
                    )
                except ZeppAuthError as exc:
                    if exc.kind == "not_available":
                        capability.add_unavailable(chunk)
                        continue
                    records.extend(capability)
                    if records:
                        raise PartialFetchError(exc, records) from exc
                    raise
                capability.add_success(FetchedRecord(raw=RawRecord(
                    stream="wellness",
                    source_key=f"wellness:spo2:user_day:{chunk.start_day()}:{chunk.end_day()}:{sub_type}",
                    start_utc=chunk.start,
                    end_utc=chunk.end,
                    payload=payload,
                    capability="unverified",
                )))
            merge_capability(capability)
        return records

    # ---- dense measurement file indexes ----

    def fetch_dense_file_records(self, window: FetchWindow) -> list[FetchedRecord]:
        chunks = window.chunks(CHUNK_DAYS)
        records = FetchBatch(expected_chunks=len(chunks))
        for chunk in chunks:
            from_ms = int(chunk.start.timestamp() * 1000)
            to_ms = int(chunk.end.timestamp() * 1000)
            try:
                payload = self.connector.fetch_file_info_events(
                    "second_heart_rate", "real_data", from_ms, to_ms, 2000
                )
            except ZeppAuthError as exc:
                if exc.kind == "not_available":
                    records.add_unavailable(chunk)
                    continue
                if records:
                    raise PartialFetchError(exc, records) from exc
                raise
            records.add_success(FetchedRecord(raw=RawRecord(
                stream="dense_files",
                source_key=f"file_info:second_heart_rate:{chunk.start_day()}:{chunk.end_day()}",
                start_utc=chunk.start,
                end_utc=chunk.end,
                payload=payload,
                capability="indexed",
            )))
        return records

    def fetch_dense_file_archive(self, file_type: str, file_id: str) -> bytes:
        return self.connector.download_dense_file(file_type, file_id)

    # ---- workout detail (pending) ----

    def fetch_workout_detail(self, workout_id: str, source: str, start_utc: datetime, end_utc: datetime | None) -> FetchedRecord:
        payload = self.connector.fetch_sport_detail(workout_id, source)
        return FetchedRecord(
            raw=RawRecord(
                stream="workout_detail",
                source_key=f"workout_detail:{workout_id}:{source}",
                start_utc=start_utc,
                end_utc=end_utc,
                payload=payload,
            )
        )
