"""Zepp 数据获取器：对齐 ZeppBridge fetcher/mod.rs。

支持：
  - 窗口分块（默认 7 天，避免单请求过大）
  - 心率分页（cursor 翻页）
  - 运动历史游标翻页（账号内混合运动记录）
  - 2 年最大窗口（730 天）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from vitalis.connectors.zepp.client import ZeppAPIClient, ZeppAuthError

MAX_SYNC_DAYS = 730  # 2 年
CHUNK_DAYS = 7
SPO2_MAX_LOCAL_DAYS = 3
HEART_RATE_PAGE_LIMIT = 1000
DAY_MILLISECONDS = 24 * 60 * 60 * 1000


def _zone(timezone_name: str | None) -> ZoneInfo:
    """Resolve a caller-selected IANA zone, preserving the app default."""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except Exception as exc:
            raise ZeppAuthError(
                f"时区无效: {timezone_name}", kind="invalid_request"
            ) from exc
    from vitalis.time import local_timezone

    return local_timezone()


def _local_day(value: datetime, timezone_name: str | None = None) -> date:
    zone = _zone(timezone_name)
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone).date()


def _local_day_utc_bounds(
    day: date, timezone_name: str | None = None
) -> tuple[datetime, datetime]:
    zone = _zone(timezone_name)
    start = datetime.combine(day, datetime_time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


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
    def local_dates(
        cls, start: date, end: date, timezone_name: str | None = None
    ) -> "FetchWindow":
        if start > end:
            start, end = end, start
        days = (end - start).days + 1
        if days > MAX_SYNC_DAYS:
            raise ZeppAuthError(
                f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间",
                kind="invalid_request",
            )
        start_utc, _ = _local_day_utc_bounds(start, timezone_name)
        _, end_utc = _local_day_utc_bounds(end, timezone_name)
        return cls(start=start_utc, end=end_utc)

    def start_day(self, timezone_name: str | None = None) -> str:
        return _local_day(self.start, timezone_name).isoformat()

    def end_day(self, timezone_name: str | None = None) -> str:
        return _local_day(
            self.end - timedelta(microseconds=1), timezone_name
        ).isoformat()

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

    def local_chunks(
        self, chunk_days: int = CHUNK_DAYS, timezone_name: str | None = None
    ) -> list["FetchWindow"]:
        """Split a UTC window on configured-local civil-day boundaries."""
        chunk_days = max(1, int(chunk_days))
        start_day = date.fromisoformat(self.start_day(timezone_name))
        end_day = date.fromisoformat(self.end_day(timezone_name))
        chunks: list[FetchWindow] = []
        cursor = start_day
        while cursor <= end_day:
            last = min(end_day, cursor + timedelta(days=chunk_days - 1))
            start, _ = _local_day_utc_bounds(cursor, timezone_name)
            _, end = _local_day_utc_bounds(last, timezone_name)
            chunks.append(
                FetchWindow(
                    start=max(self.start, start),
                    end=min(self.end, end),
                )
            )
            cursor = last + timedelta(days=1)
        return [chunk for chunk in chunks if chunk.end > chunk.start]


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
    # Retain usable rows while exposing a capped response or stalled cursor.
    incomplete: bool = False
    incomplete_reason: str | None = None


class FetchBatch(list[FetchedRecord]):
    """Fetched records plus in-memory coverage for one logical stream."""

    def __init__(self, *, expected_chunks: int = 0):
        super().__init__()
        self.expected_chunks = expected_chunks
        self.successful_chunks = 0
        self.unavailable_ranges: list[tuple[datetime, datetime]] = []
        self.unavailable_capabilities: list[tuple[str, str]] = []
        self.incomplete_ranges: list[tuple[datetime, datetime]] = []
        self.incomplete_reasons: list[str] = []

    @property
    def unavailable_chunks(self) -> int:
        return len(self.unavailable_ranges)

    @property
    def incomplete(self) -> bool:
        return bool(self.incomplete_ranges)

    @property
    def partial(self) -> bool:
        return self.incomplete or (
            self.successful_chunks > 0 and self.unavailable_chunks > 0
        )

    def add_success(self, record: FetchedRecord, *, count_chunk: bool = True) -> None:
        self.append(record)
        if count_chunk:
            self.successful_chunks += 1
        if record.incomplete:
            self.incomplete_ranges.append(
                (record.raw.start_utc, record.raw.end_utc or record.raw.start_utc)
            )
            if record.incomplete_reason:
                self.incomplete_reasons.append(record.incomplete_reason)

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
            # Normalize every observation before max; normalizing only the
            # final maximum lets a millisecond value dominate mixed input.
            normalized_ts = item_ts // 1000 if item_ts >= 10_000_000_000 else item_ts
            max_ts = normalized_ts if max_ts is None else max(max_ts, normalized_ts)
    if max_ts is None:
        return None
    return max_ts + 1


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


_SPORT_HISTORY_ITEM_KEYS = ("summary", "items", "records", "results", "list")
_SPORT_HISTORY_MISSING = object()


def _sport_history_page(payload: Any) -> tuple[list[Any] | None, Any]:
    """Return structured sport-history rows and its raw pagination cursor.

    A 200 response with no recognized row list is not an empty page. Keeping
    that distinction here prevents both fetch paths from turning an upstream
    envelope change into proof that a sport has no history.
    """
    if not isinstance(payload, dict) or payload.get("code", 1) not in (0, 1, "0", "1"):
        return None, _SPORT_HISTORY_MISSING
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, _SPORT_HISTORY_MISSING
    from vitalis.connectors.zepp.parser import ZeppParser

    for key in _SPORT_HISTORY_ITEM_KEYS:
        rows = data.get(key)
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    return None, _SPORT_HISTORY_MISSING
                track_id = item.get("trackid") or item.get("trackId")
                valid_track_id = (
                    isinstance(track_id, (str, int, float))
                    and not isinstance(track_id, bool)
                    and (not isinstance(track_id, float) or isfinite(track_id))
                    and bool(str(track_id).strip())
                )
                if not valid_track_id or ZeppParser._parse_start(item) is None:
                    return None, _SPORT_HISTORY_MISSING
            return rows, data.get("next", _SPORT_HISTORY_MISSING)
    return None, _SPORT_HISTORY_MISSING


def _sport_history_next_cursor(
    value: Any,
    *,
    start_track_id: int,
    stop_track_id: int,
) -> tuple[int | None, str | None]:
    """Validate the vendor's backward-moving sport-history cursor."""
    if value is _SPORT_HISTORY_MISSING or value is None:
        return None, None
    if isinstance(value, bool):
        return None, "workouts: sport history cursor is invalid"
    if isinstance(value, int):
        cursor = value
    elif isinstance(value, str):
        try:
            cursor = int(value.strip())
        except ValueError:
            return None, "workouts: sport history cursor is invalid"
    else:
        return None, "workouts: sport history cursor is invalid"
    if cursor <= 0:
        return None, None
    if not start_track_id < cursor < stop_track_id:
        return None, "workouts: sport history cursor did not advance within the window"
    return cursor, None


class DataFetcher:
    """Zepp 数据获取器（翻译自 Rust DataFetcher）。"""

    def __init__(
        self, connector: ZeppAPIClient, timezone_name: str | None = None
    ):
        self.connector = connector
        self.timezone_name = timezone_name

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
        incomplete = False
        incomplete_reason: str | None = None
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
            if nxt is None or nxt <= cursor or nxt >= end:
                incomplete = True
                incomplete_reason = (
                    "heart_rate: full page made no in-window cursor progress"
                )
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
            ),
            incomplete=incomplete,
            incomplete_reason=incomplete_reason,
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
                items, cursor_value = _sport_history_page(payload)
                incomplete_reason = None
                if items is None:
                    incomplete_reason = (
                        "workouts: sport history response envelope is malformed"
                    )
                    nxt = None
                else:
                    nxt, incomplete_reason = _sport_history_next_cursor(
                        cursor_value,
                        start_track_id=start_ts,
                        stop_track_id=stop_track_id,
                    )
                records.add_success(
                    FetchedRecord(
                        raw=RawRecord(
                            stream="workouts",
                            source_key=f"sport_history:{sport}:{start_ts}:{stop_track_id}",
                            start_utc=window.start,
                            end_utc=window.end,
                            payload=payload,
                        ),
                        incomplete=incomplete_reason is not None,
                        incomplete_reason=incomplete_reason,
                    ),
                    # A sport may have several pages, but coverage is counted
                    # once per sport partition rather than once per response.
                    count_chunk=False,
                )
                if incomplete_reason is not None or nxt is None:
                    break
                stop_track_id = nxt
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
            records.incomplete_ranges.extend(batch.incomplete_ranges)
            records.incomplete_reasons.extend(batch.incomplete_reasons)

        for label, surface, event_type, sub_type, chunk_days in specs:
            chunks = (
                window.local_chunks(SPO2_MAX_LOCAL_DAYS, self.timezone_name)
                if label == "spo2"
                else window.chunks(chunk_days or CHUNK_DAYS)
            )
            capability = FetchBatch(expected_chunks=len(chunks))
            for chunk in chunks:
                from_ms = int(chunk.start.timestamp() * 1000)
                to_ms = int(chunk.end.timestamp() * 1000)
                try:
                    if surface == "user":
                        request_from_ms = (
                            from_ms - DAY_MILLISECONDS
                            if label == "all_day_stress" else from_ms
                        )
                        request_to_ms = (
                            to_ms + DAY_MILLISECONDS
                            if label == "all_day_stress" else to_ms
                        )
                        payload = self.connector.fetch_user_events(
                            event_type, sub_type,
                            request_from_ms, request_to_ms, 1000, True,
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
                item_count = len(_payload_items(payload))
                capability.add_success(FetchedRecord(raw=RawRecord(
                    stream="wellness",
                    source_key=f"wellness:{label}:{surface}:{chunk.start_day(self.timezone_name)}:{chunk.end_day(self.timezone_name)}",
                    start_utc=chunk.start,
                    end_utc=chunk.end,
                    payload=payload,
                    capability="unverified",
                ), incomplete=label == "spo2" and item_count >= 1000, incomplete_reason=(
                    f"{label}: response reached the 1000-item limit"
                    if label == "spo2" and item_count >= 1000 else None
                )))
            merge_capability(capability)
        for sub_type in ("odi", "osa_event"):
            chunks = window.local_chunks(SPO2_MAX_LOCAL_DAYS, self.timezone_name)
            capability = FetchBatch(expected_chunks=len(chunks))
            for chunk in chunks:
                try:
                    from_day = chunk.start_day(self.timezone_name)
                    to_day = chunk.end_day(self.timezone_name)
                    method = self.connector.fetch_user_events_date_string
                    try:
                        payload = method(
                            "blood_oxygen", sub_type, from_day, to_day,
                            time_zone=self.timezone_name,
                        )
                    except TypeError as exc:
                        # Keep older test doubles runnable while new clients
                        # receive the configured zone.
                        if "time_zone" not in str(exc) and "keyword" not in str(exc):
                            raise
                        payload = method("blood_oxygen", sub_type, from_day, to_day)
                except ZeppAuthError as exc:
                    if exc.kind == "not_available":
                        capability.add_unavailable(chunk)
                        continue
                    records.extend(capability)
                    if records:
                        raise PartialFetchError(exc, records) from exc
                    raise
                item_count = len(_payload_items(payload))
                capability.add_success(FetchedRecord(raw=RawRecord(
                    stream="wellness",
                    source_key=f"wellness:spo2:user_day:{chunk.start_day(self.timezone_name)}:{chunk.end_day(self.timezone_name)}:{sub_type}",
                    start_utc=chunk.start,
                    end_utc=chunk.end,
                    payload=payload,
                    capability="unverified",
                ), incomplete=item_count >= 1000, incomplete_reason=(
                    f"spo2/{sub_type}: response reached the 1000-item limit"
                    if item_count >= 1000 else None
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
