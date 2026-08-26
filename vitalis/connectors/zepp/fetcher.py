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
            raise ZeppAuthError(f"同步天数必须在 1..{MAX_SYNC_DAYS} 之间")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return cls(start=start, end=end)

    def start_day(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    def end_day(self) -> str:
        return self.end.strftime("%Y-%m-%d")

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
        for key in ("timestamp", "time", "timeStamp", "startTime"):
            val = item.get(key)
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
            max_ts = item_ts if max_ts is None else max(max_ts, item_ts)
    if max_ts is None:
        return None
    # +1 秒（秒级）或 +1000 毫秒（毫秒级）
    return max_ts + (1000 if max_ts >= 10_000_000_000 else 1)


def _payload_items(payload: dict) -> list[dict]:
    """提取 payload 中的结构化 items 列表。"""
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            inner = val.get("items")
            if isinstance(inner, list):
                return inner
    return []


class DataFetcher:
    """Zepp 数据获取器（翻译自 Rust DataFetcher）。"""

    def __init__(self, connector: ZeppAPIClient):
        self.connector = connector

    # ---- heart_rate ----

    def fetch_heart_rate_records(self, window: FetchWindow) -> list[FetchedRecord]:
        records: list[FetchedRecord] = []
        last_error: Exception | None = None
        for chunk in window.chunks(CHUNK_DAYS):
            try:
                records.append(self._fetch_heart_rate_record(chunk))
            except ZeppAuthError as exc:
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    last_error = exc
                    continue
                raise
        if not records:
            raise last_error or ZeppAuthError("心率窗口没有可识别记录")
        return records

    def _fetch_heart_rate_record(self, window: FetchWindow) -> FetchedRecord:
        end = int(window.end.timestamp())
        cursor = int(window.start.timestamp())
        merged: list[dict] = []
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
            # 保留原始空响应形状
            out_payload = self.connector.fetch_heart_rate(
                int(window.start.timestamp()), int(window.end.timestamp())
            )
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
        records: list[FetchedRecord] = []
        last_error: Exception | None = None
        for chunk in window.chunks(CHUNK_DAYS):
            try:
                records.append(self._fetch_sleep_record(chunk))
            except ZeppAuthError as exc:
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    last_error = exc
                    continue
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
        records: list[FetchedRecord] = []
        last_error: Exception | None = None
        from vitalis.connectors.zepp.client import SPORTS

        for sport in SPORTS:
            stop_track_id = end_ts
            while True:
                try:
                    payload = self.connector.fetch_sport_history(
                        sport, start_ts, stop_track_id, 1
                    )
                except ZeppAuthError as exc:
                    msg = str(exc).lower()
                    if "unavailable" in msg or "http 404" in msg or "not found" in msg:
                        last_error = exc
                        break
                    raise
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
        if not records:
            # 所有运动类型都不可用（如全部 404），返回空列表而非失败
            if last_error is not None:
                return []
            raise ZeppAuthError("sport history 没有可用种类")
        return records

    # ---- hrv ----

    def fetch_hrv_records(self, window: FetchWindow) -> list[FetchedRecord]:
        records: list[FetchedRecord] = []
        last_error: Exception | None = None
        for chunk in window.chunks(CHUNK_DAYS):
            try:
                payload = self.connector.fetch_hrv(chunk.start_day(), chunk.end_day())
                records.append(
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
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    last_error = exc
                    continue
                raise
        if not records:
            raise last_error or ZeppAuthError("HRV 窗口没有可识别记录")
        return records

    # ---- daily statistics ----

    def fetch_daily_statistics_records(self, window: FetchWindow) -> list[FetchedRecord]:
        records: list[FetchedRecord] = []
        from_ms = int(window.start.timestamp() * 1000)
        to_ms = int(window.end.timestamp() * 1000)
        # DailyHealth / summary
        event = self.connector.fetch_events("DailyHealth", "summary", from_ms, to_ms, 2000, True)
        records.append(
            FetchedRecord(
                raw=RawRecord(
                    stream="daily_summary",
                    source_key=f"events:DailyHealth:summary:{from_ms}:{to_ms}",
                    start_utc=window.start,
                    end_utc=window.end,
                    payload=event,
                )
            )
        )
        # 可选子流：Charge / readiness
        for event_type, sub_type in (("Charge", "real_data"), ("readiness", "watch_score")):
            try:
                payload = self.connector.fetch_events(
                    event_type, sub_type, from_ms, to_ms, 2000, True
                )
                records.append(
                    FetchedRecord(
                        raw=RawRecord(
                            stream="daily_summary",
                            source_key=f"events:{event_type}:{sub_type}:{from_ms}:{to_ms}",
                            start_utc=window.start,
                            end_utc=window.end,
                            payload=payload,
                        )
                    )
                )
            except ZeppAuthError as exc:
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    continue
                raise
        # WatchSportStatistics: SPORT_LOAD / VO2_MAX
        for statistic in ("SPORT_LOAD", "VO2_MAX"):
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
                if "Unavailable" in str(exc) or "unavailable" in str(exc).lower():
                    continue
                raise
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
        records: list[FetchedRecord] = []
        for label, surface, event_type, sub_type, chunk_days in specs:
            chunks = window.chunks(chunk_days) if chunk_days else [window]
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
                except ZeppAuthError:
                    # Every wellness stream is optional and account/device dependent.
                    continue
                records.append(FetchedRecord(raw=RawRecord(
                    stream="wellness",
                    source_key=f"wellness:{label}:{surface}:{chunk.start_day()}:{chunk.end_day()}",
                    start_utc=chunk.start,
                    end_utc=chunk.end,
                    payload=payload,
                    capability="unverified",
                )))
        return records

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
