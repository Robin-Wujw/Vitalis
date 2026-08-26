"""Zepp 厂商格式 -> Vitalis Schema 的解析器。

职责：把 Zepp 原始 JSON 转换成统一的 SleepRecord / ActivityRecord /
TrainingRecord / Workout。此处是「厂商格式隔离」的关键边界 ——
上层模块永远看不到 Zepp 的字段名（sleepScore/stages 等）。
"""

from datetime import date, datetime, time, timedelta, timezone

from vitalis.models import (
    ActivityRecord,
    DailyMetric,
    Device,
    MetricSample,
    SleepRecord,
    TrainingRecord,
    Workout,
    WorkoutSample,
    WorkoutType,
)

MAX_WORKOUT_SECONDS = 12 * 60 * 60

_TYPE_MAP = {
    "running": WorkoutType.RUNNING,
    "strength": WorkoutType.STRENGTH,
    "cycling": WorkoutType.CYCLING,
    "swimming": WorkoutType.SWIMMING,
    "walking": WorkoutType.WALKING,
    "hiit": WorkoutType.HIIT,
    "yoga": WorkoutType.YOGA,
}

# 解析 helper 使用（真实 sport history 的类型名 -> Vitalis WorkoutType）
_WORKOUT_TYPE_MAP = {
    "run": WorkoutType.RUNNING,
    "running": WorkoutType.RUNNING,
    "treadmill": WorkoutType.RUNNING,
    "indoor_run": WorkoutType.RUNNING,
    "walking": WorkoutType.WALKING,
    "hiking": WorkoutType.WALKING,
    "trail": WorkoutType.RUNNING,
    "ride": WorkoutType.CYCLING,
    "cycling": WorkoutType.CYCLING,
    "indoor_cycling": WorkoutType.CYCLING,
    "swimming": WorkoutType.SWIMMING,
    "strength": WorkoutType.STRENGTH,
    "elliptical": WorkoutType.OTHER,
    "rowing": WorkoutType.OTHER,
    "climb": WorkoutType.OTHER,
    "yoga": WorkoutType.YOGA,
    "badminton": WorkoutType.OTHER,
    "activity": WorkoutType.OTHER,
    "unknown": WorkoutType.OTHER,
}


class ZeppParser:
    """把 Zepp 原始响应解析为 Vitalis Schema 对象。

    支持两类原始格式：
      - 旧 mock 格式（get_sleep/get_activity/get_training 直出）
      - 真实 band_data / sport_history 格式（apptoken 模式，对齐 ZeppBridge）
    """

    def __init__(self, source: str = "zepp"):
        self.source = source

    # ================= 真实 band_data（睡眠 + 活动） =================

    def parse_band(self, raw: dict) -> tuple[dict[date, SleepRecord], dict[date, ActivityRecord]]:
        """解析手环原始数据 -> {日期: SleepRecord}, {日期: ActivityRecord}。

        band_data.items[].summary 是 base64 编码的 JSON：
          slp: 睡眠 {ss分数, st/ed起止, dp/lt/rm/wk分钟, rhr静息心率}
          stp: 步数 {ttl, cal, dis(米)}
          tz:  时区偏移（秒）
        """
        import base64
        import json

        sleeps: dict[date, SleepRecord] = {}
        activities: dict[date, ActivityRecord] = {}
        # 防御性：data 可能是 dict{"items": [...]} 或直接的 list
        data = raw.get("data") if isinstance(raw, dict) else {}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or []
        else:
            items = []
        for item in items:
            day = self._item_date(item)
            summary = self._base64_summary(item)
            if not day or not summary:
                continue

            sleep = summary.get("slp") or {}
            if sleep.get("ed") and sleep.get("st"):
                st_val, ed_val = sleep.get("st"), sleep.get("ed")
                # 真实数据 st/ed 可能是时间戳（int）或 HH:MM 字符串
                if isinstance(st_val, (int, float)) and isinstance(ed_val, (int, float)):
                    from datetime import datetime, timezone
                    st_dt = datetime.fromtimestamp(st_val, tz=timezone.utc)
                    ed_dt = datetime.fromtimestamp(ed_val, tz=timezone.utc)
                    span = int((ed_dt - st_dt).total_seconds() // 60)
                    duration = max(span - int(sleep.get("wk", 0) or 0), 0)
                    bedtime = st_dt.time().replace(tzinfo=None)
                    wake_time = ed_dt.time().replace(tzinfo=None)
                else:
                    start_min, end_min = self._parse_hhmm(st_val), self._parse_hhmm(ed_val)
                    if start_min is not None and end_min is not None:
                        span = (end_min - start_min) % (24 * 60)
                        awake = int(sleep.get("wk", 0) or 0)
                        duration = max(span - awake, 0)
                        bedtime = self._to_time(str(st_val))
                        wake_time = self._to_time(str(ed_val))
                    else:
                        duration = 0
                        bedtime = None
                        wake_time = None
                if duration > 0:
                    sleeps[day] = SleepRecord(
                        user_id="", source=self.source, date=day,
                        sleep_duration=duration,
                        deep_sleep=int(sleep.get("dp", 0) or 0),
                        rem_sleep=int(sleep.get("rm", 0) or 0),
                        light_sleep=int(sleep.get("lt", 0) or 0),
                        awake=int(sleep.get("wk", 0) or 0),
                        sleep_score=int(sleep["ss"]) if sleep.get("ss") is not None else None,
                        bedtime=bedtime,
                        wake_time=wake_time,
                    )

            steps = summary.get("stp") or {}
            if steps:
                activities[day] = ActivityRecord(
                    user_id="", source=self.source, date=day,
                    steps=int(steps.get("ttl", 0) or 0),
                    calories=int(steps.get("cal", 0) or 0),
                    distance_km=round(float(steps.get("dis", 0) or 0) / 1000, 2),
                    resting_hr=int(sleep.get("rhr", 0) or 0) if sleep.get("rhr") else 0,
                )
        return sleeps, activities

    # ================= 真实 sport history（运动） =================

    def parse_sport_history(self, raw: dict, sport_hint: str = "") -> list[Workout]:
        """运动历史 -> Workout 列表。type 为数字 id（1=run, 6=walking...）。"""
        from .client import SPORT_TYPE_MAP

        items = self._items(raw)
        workouts: list[Workout] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            type_id = it.get("type")
            numeric_type: int | None = None
            if isinstance(type_id, (int, float)):
                numeric_type = int(type_id)
            elif isinstance(type_id, str):
                try:
                    numeric_type = int(type_id.strip())
                except ValueError:
                    pass
            wtype = SPORT_TYPE_MAP.get(numeric_type) if numeric_type is not None else None
            wtype = wtype or it.get("sportType") or it.get("sport")
            # /run/history.json is an aggregate endpoint in real accounts. Only use
            # its URL segment when the record itself has no numeric sport type.
            if not wtype and numeric_type is None:
                wtype = sport_hint
            wtype = wtype or "unknown"
            wtype = _WORKOUT_TYPE_MAP.get(wtype, WorkoutType.OTHER)
            started = self._parse_start(it)
            avg_hr = self._first_number(
                it, ("avg_hr", "avgHr", "avg_heart_rate")
            ) or 0
            max_hr = self._first_number(
                it, ("max_hr", "maxHr", "max_heart_rate")
            ) or 0
            load = self._first_number(
                it, ("training_load", "trainLoad", "exercise_load")
            ) or 0
            calories = self._first_number(it, ("calories", "calorie")) or 0
            distance = self._first_number(it, ("distance", "dis"))
            workouts.append(Workout(
                user_id="", source=self.source,
                workout_id=str(it.get("trackid") or it.get("trackId") or ""),
                started_at=started,
                ended_at=self._parse_datetime_value(
                    it.get("end_time") or it.get("endTime") or it.get("finishTime")
                ),
                type=wtype,
                duration=max((self._duration_minutes(it)), 0),
                heart_rate_avg=max(int(avg_hr), 0),
                heart_rate_max=max(int(max_hr), 0),
                load=max(int(load), 0),
                calories=max(int(calories), 0),
                distance_km=(
                    round(max(distance, 0) / 1000, 3)
                    if distance is not None
                    else None
                ),
                vendor_source=str(it.get("source")) if it.get("source") else None,
            ))
        return workouts

    @staticmethod
    def parse_workout_heart_rate(
        raw: dict, summary_end: datetime | None = None
    ) -> list[WorkoutSample]:
        """Decode Zepp workout heart-rate deltas into per-second samples.

        Each entry is ``seconds_since_previous_change,heart_rate_delta``. The first
        heart-rate delta is the absolute value because accumulation starts at zero.
        Zepp does not include per-sample sensor provenance in this payload, so these
        samples deliberately remain ``source_scope=unknown``.
        """
        if not isinstance(raw, dict):
            return []
        nested = raw.get("data")
        data = nested if isinstance(nested, dict) else raw
        encoded = data.get("heart_rate")
        start = ZeppParser._parse_datetime_value(data.get("trackid"))
        if not isinstance(encoded, str) or start is None:
            return []
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        else:
            start = start.astimezone(timezone.utc)

        pairs: list[tuple[int, int]] = []
        for part in encoded.split(";"):
            if not part:
                continue
            raw_seconds, separator, raw_delta = part.partition(",")
            if not separator:
                continue
            try:
                seconds = 1 if raw_seconds == "" else int(raw_seconds)
                delta = int(raw_delta)
            except ValueError:
                continue
            pairs.append((max(seconds, 0), delta))
        if not pairs:
            return []

        time_span = 0
        raw_time = data.get("time")
        if isinstance(raw_time, str):
            for part in raw_time.split(";"):
                try:
                    time_span += max(int(part), 0)
                except ValueError:
                    continue
        if time_span <= 0:
            time_span = sum(seconds for seconds, _ in pairs)

        end = start + timedelta(seconds=min(time_span, MAX_WORKOUT_SECONDS))
        if summary_end is not None:
            normalized_end = summary_end
            if normalized_end.tzinfo is None:
                normalized_end = normalized_end.replace(tzinfo=timezone.utc)
            else:
                normalized_end = normalized_end.astimezone(timezone.utc)
            end = max(end, normalized_end)
        max_end = start + timedelta(seconds=MAX_WORKOUT_SECONDS)
        end = min(max(end, start + timedelta(seconds=1)), max_end)

        workout_id = str(data.get("trackid") or "")
        samples: list[WorkoutSample] = []
        working = start
        heart_rate = 0
        for index, (seconds, delta) in enumerate(pairs):
            heart_rate += delta
            first_second = 0 if index == 0 else 1
            if seconds < first_second:
                continue
            for _ in range(first_second, seconds + 1):
                if working > end:
                    break
                if 1 <= heart_rate <= 300:
                    samples.append(WorkoutSample(
                        workout_id=workout_id,
                        timestamp=working,
                        heart_rate=heart_rate,
                        source_scope="unknown",
                    ))
                working += timedelta(seconds=1)
        while working <= end:
            if 1 <= heart_rate <= 300:
                samples.append(WorkoutSample(
                    workout_id=workout_id,
                    timestamp=working,
                    heart_rate=heart_rate,
                    source_scope="unknown",
                ))
            working += timedelta(seconds=1)
        return samples

    # ================= 通用指标（时序 + 每日） =================

    @staticmethod
    def parse_heart_rate_samples(raw: dict) -> list[MetricSample]:
        samples: list[MetricSample] = []
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict):
                continue
            nested = item.get("value") if isinstance(item.get("value"), dict) else None
            obj = nested if nested and any(k in nested for k in ("timestamp", "time", "heartRate", "hr")) else item
            timestamp = ZeppParser._parse_datetime_value(
                obj.get("timestamp") or obj.get("time") or obj.get("timeStamp") or obj.get("startTime")
            )
            value = ZeppParser._first_number(obj, ("value", "heartRate", "heart_rate", "hr"))
            if timestamp is None or value is None or not 0 <= value <= 300:
                continue
            samples.append(MetricSample(
                metric="heart_rate",
                timestamp=timestamp,
                value=value,
                unit="bpm",
                source_scope="device" if ZeppParser._device_id(obj) else "user_fused",
                device_id=ZeppParser._device_id(obj),
            ))
        return samples

    @staticmethod
    def parse_daily_metrics(raw: dict) -> list[DailyMetric]:
        """Parse verified DailyHealth/Charge/readiness/watch-stat fields."""
        fields: tuple[tuple[str, tuple[str, ...], str], ...] = (
            ("steps", ("steps", "step", "stepCount", "totalSteps"), "steps"),
            ("calories", ("calories", "calorie", "totalCalories"), "kcal"),
            ("active_minutes", ("activeMinutes", "totalBurningDuration"), "min"),
            ("distance", ("distance", "totalDistance"), "m"),
            ("resting_hr", ("resting_hr", "restingHr", "restingHeartRate", "rhr"), "bpm"),
            ("readiness", ("readiness", "readinessScore", "watchScore", "rdnsScore"), "score"),
            ("physical_readiness", ("phyScore",), "score"),
            ("mental_readiness", ("mentScore",), "score"),
            ("hrv_readiness", ("hrvScore",), "score"),
            ("rhr_readiness", ("rhrScore",), "score"),
            ("bio_charge", ("bio_charge", "bioCharge", "bodyBattery", "chargeScore"), "score"),
            ("training_load", ("training_load", "trainingLoad", "wtlSum", "currnetDayTrainLoad", "load"), "load"),
            ("vo2max", ("vo2max", "vo2Max", "VO2_MAX", "VO2_max"), "ml/kg/min"),
            ("stress", ("stress", "stressScore", "avgStress", "averageStress"), "score"),
            ("spo2", ("spo2", "bloodOxygen", "blood_oxygen"), "%"),
        )
        output: dict[tuple[date, str], DailyMetric] = {}
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict):
                continue
            event_value = item.get("value") if isinstance(item.get("value"), dict) else None
            objects: list[tuple[dict, dict | None]] = []
            nested_samples = event_value.get("samples") if event_value else None
            if isinstance(nested_samples, list):
                objects.extend((sample, item) for sample in nested_samples if isinstance(sample, dict))
            else:
                objects.append((item, event_value))
            for obj, nested in objects:
                day = ZeppParser._metric_date(obj, nested)
                if day is None:
                    continue
                device = ZeppParser._device_id(obj) or ZeppParser._device_id(nested or {})
                for metric, keys, unit in fields:
                    value = ZeppParser._first_number(obj, keys)
                    if value is None and nested:
                        value = ZeppParser._first_number(nested, keys)
                    if value is None:
                        continue
                    output[(day, metric)] = DailyMetric(
                        date=day, metric=metric, value=value, unit=unit,
                        source_scope="device" if device else "user_fused", device_id=device,
                    )
        return list(output.values())

    @staticmethod
    def parse_wellness(raw: dict, source_key: str) -> tuple[list[DailyMetric], list[MetricSample]]:
        """Parse the optional wellness shapes verified by ZeppBridge."""
        label = source_key.split(":")[1] if source_key.startswith("wellness:") else ""
        items = ZeppParser._items(raw)
        daily: list[DailyMetric] = []
        samples: list[MetricSample] = []

        if label == "respiratory_rate":
            import base64
            for item in items:
                value = item.get("value") if isinstance(item, dict) else None
                encoded = value.get("measurements") if isinstance(value, dict) else None
                day = ZeppParser._metric_date(item, value) if isinstance(item, dict) else None
                if not encoded or not day:
                    continue
                try:
                    readings = [float(v) for v in base64.b64decode(encoded) if 4 <= v <= 60]
                except Exception:
                    continue
                if readings:
                    for metric, reading in (
                        ("respiratory_rate", round(sum(readings) / len(readings), 1)),
                        ("respiratory_rate_min", min(readings)),
                        ("respiratory_rate_max", max(readings)),
                    ):
                        daily.append(DailyMetric(date=day, metric=metric, value=reading, unit="brpm", source_scope="device"))
        elif label == "hrv_rmssd":
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
                    continue
                value = item["value"]
                start = ZeppParser._parse_datetime_value(value.get("startTime"))
                if not start:
                    continue
                raw_samples = value.get("samples") or []
                device_ids = ZeppParser._expand_sample_device_ids(
                    value.get("deviceId") or value.get("device_id"), len(raw_samples)
                )
                for index, sample in enumerate(raw_samples):
                    if not isinstance(sample, dict):
                        continue
                    reading = ZeppParser._first_number(sample, ("hrv", "rmssd"))
                    if reading is None or not 1 <= reading <= 400:
                        continue
                    offset = ZeppParser._first_number(sample, ("s", "offset")) or 0
                    samples.append(MetricSample(
                        metric="hrv_rmssd", timestamp=start + timedelta(milliseconds=offset),
                        value=reading, unit="ms", source_scope="device",
                        device_id=device_ids[index],
                    ))
        elif label == "spo2":
            import json
            for item in items:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("subType")
                day = ZeppParser._metric_date(item, None)
                if subtype == "odi" and day:
                    for metric, keys, unit in (
                        ("spo2_odi", ("odi",), "events/h"),
                        ("spo2_odi_events", ("odiNum",), "count"),
                        ("spo2_night_score", ("score",), "score"),
                    ):
                        reading = ZeppParser._first_number(item, keys)
                        if reading is not None:
                            daily.append(DailyMetric(date=day, metric=metric, value=reading, unit=unit, source_scope="device"))
                    continue
                try:
                    extra = json.loads(item.get("extra", "{}"))
                except (TypeError, json.JSONDecodeError):
                    continue
                reading = ZeppParser._first_number(extra, ("spo2", "value"))
                timestamp = ZeppParser._parse_datetime_value(extra.get("timestamp") or item.get("timestamp"))
                if reading is not None and 50 <= reading <= 100 and timestamp:
                    samples.append(MetricSample(metric="spo2", timestamp=timestamp, value=reading, unit="%", source_scope="device"))
        elif label == "pai":
            for item in items:
                if not isinstance(item, dict):
                    continue
                day = ZeppParser._metric_date(item, None)
                if not day:
                    continue
                for metric, keys, unit in (
                    ("pai_daily", ("dailyPai",), "pai"),
                    ("pai_low_zone", ("lowZonePai",), "pai"),
                    ("pai_medium_zone", ("mediumZonePai",), "pai"),
                    ("pai_high_zone", ("highZonePai",), "pai"),
                    ("device_max_hr", ("maxHr",), "bpm"),
                    ("device_resting_hr", ("restHr",), "bpm"),
                ):
                    reading = ZeppParser._first_number(item, keys)
                    if reading is not None:
                        daily.append(DailyMetric(date=day, metric=metric, value=reading, unit=unit, source_scope="device"))
        else:
            daily.extend(ZeppParser.parse_daily_metrics(raw))
        return daily, samples

    @staticmethod
    def _expand_sample_device_ids(encoded: object, sample_count: int) -> list[str | None]:
        """Expand Zepp's ``count,device;count,device`` sample attribution."""
        if sample_count <= 0:
            return []
        if not isinstance(encoded, str) or not encoded.strip():
            return [None] * sample_count

        text = encoded.strip().strip(";")
        if "," not in text:
            return [text] * sample_count

        expanded: list[str] = []
        for segment in text.split(";"):
            count_text, separator, device_id = segment.partition(",")
            if not separator or not device_id.strip():
                return [None] * sample_count
            try:
                count = int(count_text)
            except ValueError:
                return [None] * sample_count
            if count < 0 or len(expanded) + count > sample_count:
                return [None] * sample_count
            expanded.extend([device_id.strip()] * count)

        if len(expanded) != sample_count:
            return [None] * sample_count
        return expanded

    # ================= 真实 events（HRV / 摘要） =================

    @staticmethod
    def parse_hrv_events(raw: dict) -> dict[date, int]:
        """HRV 事件流 -> {日期: 当天均值 ms}。

        对齐 ZeppBridge normalizer：
          - 新版：item.value = {samples: [{sdnn, rmssd, hrv, value}, ...]}
          - 旧版：item.value = 数值（直接取）
          - 时间戳优先 item.timestamp，兜底 value.startTime
        """
        from statistics import fmean

        by_day: dict[date, list[float]] = {}
        # 提取 items（兼容直接数组 / data.items / data 对象）
        items: list = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or []
            elif data is None:
                items = raw.get("items") or []
        for it in items:
            if not isinstance(it, dict):
                continue

            val = it.get("value")
            day = None
            samples: list[float] = []

            if isinstance(val, dict):
                # 新版结构：value.samples[]
                arr = val.get("samples")
                if isinstance(arr, list):
                    for sample in arr:
                        if isinstance(sample, dict):
                            # 取 sdnn / rmssd / hrv / value 任一有效数值
                            for key in ("sdnn", "rmssd", "hrv", "value"):
                                v = sample.get(key)
                                if isinstance(v, (int, float)) and v >= 0:
                                    samples.append(float(v))
                                    break
                    # 日期用 item.timestamp 或 value.startTime
                    day = ZeppParser._ts_date(
                        it.get("timestamp") or it.get("time") or val.get("startTime") or val.get("start_time")
                    )
            elif isinstance(val, (int, float)):
                # 旧版结构：直接数值
                samples.append(float(val))
                day = ZeppParser._ts_date(it.get("ts") or it.get("time") or it.get("timestamp"))

            if day and samples:
                by_day.setdefault(day, []).extend(samples)

        return {d: round(fmean(v)) for d, v in by_day.items() if v}

    @staticmethod
    def parse_load_events(raw: dict) -> dict[date, int]:
        """训练负荷事件 -> {日期: 负荷}。"""
        load: dict[date, int] = {}
        items = ((raw or {}).get("data") or {}).get("items") or []
        # WatchSportStatistics: items[{day, load}]；events: items[{ts, value}]
        for it in items:
            day = ZeppParser._ts_date(it.get("day") or it.get("ts"))
            val = it.get("load" if "load" in it else "value")
            if day and isinstance(val, (int, float)):
                load[day] = int(val)
        return load

    # ================= 旧 mock 直出格式（保留） =================

    def parse_sleep(self, raw: dict) -> SleepRecord | None:
        data = (raw or {}).get("data")
        if not data or data.get("duration") is None:
            return None
        stages = data.get("stages", {}) or {}
        bedtime = self._to_time(data.get("bedTime"))
        wake = self._to_time(data.get("wakeTime"))
        return SleepRecord(
            user_id="",  # 由 sync service 填充
            source=self.source,
            date=date.fromisoformat(data["date"]) if data.get("date") else None,
            sleep_duration=int(data["duration"]),
            deep_sleep=int(stages.get("deep", 0)),
            light_sleep=int(stages.get("light", 0)),
            rem_sleep=int(stages.get("rem", 0)),
            awake=int(data.get("awake", 0)),
            sleep_score=int(data["sleepScore"]) if data.get("sleepScore") is not None else None,
            bedtime=bedtime,
            wake_time=wake,
        )

    # ---- Activity ----
    def parse_activity(self, raw: dict) -> ActivityRecord | None:
        data = (raw or {}).get("data")
        if not data:
            return None
        return ActivityRecord(
            user_id="",
            source=self.source,
            date=date.fromisoformat(data["date"]) if data.get("date") else None,
            steps=int(data.get("steps", 0)),
            active_minutes=int(data.get("activeMinutes", 0)),
            calories=int(data.get("calories", 0)),
            distance_km=float(data.get("distanceKm", 0)),
            resting_hr=int(data.get("restingHr", 0)),
        )

    # ---- Training ----
    def parse_training(self, raw: dict) -> TrainingRecord | None:
        data = (raw or {}).get("data", {})
        items = data.get("items", []) or []
        if not items:
            return None
        total_duration = 0
        total_load = 0
        for it in items:
            total_duration += int(it.get("duration", 0))
            total_load += int(it.get("load", 0))
        day = date.fromisoformat(data.get("date") or items[0]["startTime"][:10])
        return TrainingRecord(
            user_id="",
            date=day,
            workout_count=len(items),
            total_duration=total_duration,
            total_load=total_load,
        )

    def parse_workout(self, raw: dict) -> Workout:
        t = raw.get("type", "other")
        started = raw.get("startTime")
        return Workout(
            user_id="",
            source=self.source,
            workout_id=raw.get("workoutId", ""),
            started_at=datetime.fromisoformat(started) if started else None,
            type=_TYPE_MAP.get(t, WorkoutType.OTHER),
            duration=int(raw.get("duration", 0)),
            heart_rate_avg=int(raw.get("avgHr", 0)),
            heart_rate_max=int(raw.get("maxHr", 0)),
            load=int(raw.get("load", 0)),
            calories=int(raw.get("calories", 0)),
        )

    def parse_device(self, raw: dict) -> Device:
        return Device(
            user_id="",
            source=self.source,
            model=raw.get("model", ""),
            device_id=raw.get("deviceId", ""),
        )

    @staticmethod
    def _to_time(v: str | None) -> time | None:
        if not v:
            return None
        try:
            return time.fromisoformat(v)
        except ValueError:
            return None

    # ---- 真实 band_data 解析工具 ----
    @staticmethod
    def _item_date(item: dict) -> date | None:
        v = item.get("date_time") or item.get("date") or item.get("dayId")
        if not v:
            return None
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            return None

    @staticmethod
    def _base64_summary(item: dict) -> dict | None:
        import base64
        import json

        enc = item.get("summary")
        if not enc:
            return {}
        try:
            return json.loads(base64.b64decode(enc))
        except Exception:
            try:
                return json.loads(enc)  # 兜底：直接 JSON
            except Exception:
                return {}

    @staticmethod
    def _parse_hhmm(v) -> int | None:
        """把 "HH:MM" 转成当天分钟数。"""
        if v is None:
            return None
        s = str(v)
        if ":" in s:
            parts = s.split(":")
            try:
                return int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                return None
        try:
            return int(float(s))  # 已是最小值/时间戳兜底
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_start(it: dict) -> datetime | None:
        v = it.get("start_time") or it.get("startTime") or it.get("beginTime") or it.get("trackid")
        return ZeppParser._parse_datetime_value(v)

    @staticmethod
    def _parse_datetime_value(value) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit():
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        try:
            number = float(value)
            if 19_000_101 <= number <= 21_001_231:
                return datetime.strptime(str(int(number)), "%Y%m%d").replace(tzinfo=timezone.utc)
            if abs(number) >= 10_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _items(raw) -> list:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, dict):
            return []
        for key in ("items", "records", "results", "list"):
            if isinstance(raw.get(key), list):
                return raw[key]
        data = raw.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "records", "results", "list", "summary"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _first_number(obj: dict, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value.strip())
                except ValueError:
                    pass
        return None

    @staticmethod
    def _device_id(obj: dict) -> str | None:
        for key in ("deviceId", "device_id", "did", "mac"):
            value = obj.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _metric_date(obj: dict, nested: dict | None) -> date | None:
        for source in (obj, nested or {}):
            for key in ("date", "day", "dayId", "dateString", "localDate", "timestamp", "time", "startTime"):
                value = source.get(key)
                if value is None:
                    continue
                if isinstance(value, str) and len(value) >= 10 and "-" in value:
                    try:
                        return date.fromisoformat(value[:10])
                    except ValueError:
                        pass
                parsed = ZeppParser._parse_datetime_value(value)
                if parsed:
                    return parsed.date()
        return None

    @staticmethod
    def _duration_minutes(it: dict) -> int:
        """运动时长：优先 end-start，兜底 duration 字段。"""
        start = ZeppParser._parse_start(it)
        v = it.get("end_time") or it.get("endTime") or it.get("finishTime")
        if start and v:
            end = ZeppParser._parse_datetime_value(v)
            if end:
                return max(int((end - start).total_seconds() // 60), 0)
        dur = it.get("duration") or it.get("durationMinutes") or 0
        try:
            return int(float(dur))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _ts_date(v) -> date | None:
        if not v:
            return None
        s = str(v)
        # 纯数字（毫秒/秒时间戳）优先走时间戳分支，避免 fromisoformat 误解析
        if s.isdigit():
            try:
                ts = float(s)
                if ts > 1e12:
                    ts /= 1000
                return datetime.fromtimestamp(ts).date()
            except (TypeError, ValueError, OSError):
                return None
        if "T" in s or len(s) >= 10:
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                pass
        try:
            ts = float(s)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts).date()
        except (TypeError, ValueError, OSError):
            return None
