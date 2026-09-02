"""Zepp 厂商格式 -> Vitalis Schema 的解析器。

职责：把 Zepp 原始 JSON 转换成统一的 SleepRecord / ActivityRecord /
TrainingRecord / Workout。此处是「厂商格式隔离」的关键边界 ——
上层模块永远看不到 Zepp 的字段名（sleepScore/stages 等）。
"""

from base64 import b64decode
from binascii import Error as BinasciiError
from datetime import date, datetime, time, timedelta, timezone
import json

from vitalis.models import (
    ActivityRecord,
    DailyMetric,
    DenseDataFile,
    Device,
    MetricSample,
    SleepRecord,
    TrainingRecord,
    Workout,
    WorkoutDetail,
    WorkoutLap,
    WorkoutMetricSample,
    WorkoutPause,
    StrengthSetObservation,
    WorkoutType,
)
from .sport_types import resolve_sport_mode

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

_PRODUCT_MODELS = {
    "146": "Amazfit Balance 2",
    "157": "Amazfit Helio Strap",
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
          slp: 睡眠 {ss分数, st/ed起止, dp/lt/rm/wk分钟, stage分期, rhr静息心率}
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
                    st_dt = datetime.fromtimestamp(st_val, tz=timezone.utc)
                    ed_dt = datetime.fromtimestamp(ed_val, tz=timezone.utc)
                    span = int((ed_dt - st_dt).total_seconds() // 60)
                    duration = max(span - int(sleep.get("wk", 0) or 0), 0)
                    offset = self._first_number(summary, ("tz",)) or 0
                    offset = max(min(int(offset), 18 * 60 * 60), -18 * 60 * 60)
                    bedtime = (st_dt + timedelta(seconds=offset)).time().replace(tzinfo=None)
                    wake_time = (ed_dt + timedelta(seconds=offset)).time().replace(tzinfo=None)
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
                    rem = self._first_number(sleep, ("rm", "remMinutes", "rem"))
                    if rem is None:
                        rem = self._sleep_stage_minutes(sleep, {8, 11})
                    sleeps[day] = SleepRecord(
                        user_id="", source=self.source, date=day,
                        sleep_duration=duration,
                        deep_sleep=int(sleep.get("dp", 0) or 0),
                        rem_sleep=int(rem) if rem is not None else None,
                        light_sleep=int(sleep.get("lt", 0) or 0),
                        awake=int(sleep.get("wk", 0) or 0),
                        sleep_score=int(sleep["ss"]) if sleep.get("ss") is not None else None,
                        bedtime=bedtime,
                        wake_time=wake_time,
                        stages=self._sleep_stage_slices(
                            day, summary, sleep, st_val, ed_val
                        ),
                        wake_count=self._bounded_int(sleep, ("wc",), 0, 100),
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

    @staticmethod
    def parse_band_heart_rate(raw: dict) -> list[MetricSample]:
        """Decode ``band_data.data_hr`` into timestamped minute samples."""
        import base64
        import binascii

        samples: list[MetricSample] = []
        for item in ZeppParser._band_items(raw):
            encoded = item.get("data_hr")
            day = ZeppParser._item_date(item)
            if not isinstance(encoded, str) or not encoded.strip() or day is None:
                continue
            try:
                readings = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError, TypeError):
                continue
            summary = ZeppParser._base64_summary(item) or {}
            offset = ZeppParser._first_number(summary, ("tz",)) or 0
            offset = max(min(int(offset), 18 * 60 * 60), -18 * 60 * 60)
            midnight = datetime.combine(day, time.min, tzinfo=timezone.utc) - timedelta(seconds=offset)
            device_id = ZeppParser._device_id(item)
            for minute, reading in enumerate(readings[:1440]):
                if not 20 <= reading <= 240:
                    continue
                samples.append(MetricSample(
                    metric="heart_rate",
                    timestamp=midnight + timedelta(minutes=minute),
                    value=float(reading),
                    unit="bpm",
                    source_scope="device" if device_id else "unknown",
                    device_id=device_id,
                ))
        return samples

    # ================= 真实 sport history（运动） =================

    def parse_sport_history(self, raw: dict, sport_hint: str = "") -> list[Workout]:
        """运动历史 -> Workout 列表。type 为数字 id（1=run, 6=walking...）。"""
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
            # This is an aggregate endpoint, so only the record's numeric type is
            # authoritative. Missing IDs never inherit the URL or a textual hint.
            mode = resolve_sport_mode(numeric_type)
            wtype = WorkoutType(mode.category)
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
                sport_mode=mode.code,
                sport_mode_label=mode.label_zh,
                training_family=mode.family,
                recognition_confidence=mode.recognition_confidence,
                recognition_confidence_label=mode.recognition_confidence_label,
                recognition_source=mode.recognition_source,
                recognition_source_label=mode.recognition_source_label,
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
                vendor_type_id=numeric_type,
                heart_rate_zone_setting_type=self._bounded_int(
                    it, ("heartrate_setting_type", "heartRateSettingType"), 0, 20
                ),
                heart_rate_zone_boundaries_bpm=self._heart_rate_zone_boundaries(it),
            ))
        return workouts

    @staticmethod
    def parse_workout_detail(
        raw: dict, summary_end: datetime | None = None
    ) -> WorkoutDetail | None:
        """Normalize the current Zepp workout-detail payload.

        Only fields with verified semantics are emitted. Empty vendor fields and
        malformed series remain absent instead of being replaced with zeros.
        """
        if not isinstance(raw, dict):
            return None
        nested = raw.get("data")
        data = nested if isinstance(nested, dict) else raw
        start = ZeppParser._parse_datetime_value(data.get("trackid"))
        if start is None:
            return None
        start = ZeppParser._utc(start)
        workout_id = str(data.get("trackid") or "")
        end = ZeppParser._workout_end(data, start, summary_end)

        samples = ZeppParser._workout_heart_rate_samples(
            workout_id, start, end, data.get("heart_rate")
        )
        series_specs = (
            ("speed", "speed", "m/s", 1.0, 0.0, 20.0),
            ("equivPace", "equivalent_pace", "s/km", 1.0, 60.0, 3600.0),
            ("currentDistance", "distance", "m", 0.01, 0.0, 1_000_000.0),
            ("time_delta_altitude", "altitude", "m", 0.1, -1000.0, 10_000.0),
            ("power_meter", "running_power", "W", 1.0, 0.0, 5000.0),
        )
        for vendor_field, metric, unit, scale, minimum, maximum in series_specs:
            samples.extend(ZeppParser._absolute_workout_series(
                workout_id,
                start,
                data.get(vendor_field),
                metric,
                unit,
                scale,
                minimum,
                maximum,
            ))
        samples.extend(ZeppParser._gait_samples(
            workout_id, start, data.get("gait")
        ))
        samples.extend(ZeppParser._run_posture_samples(
            workout_id, start, data.get("runPosture")
        ))
        samples = ZeppParser._deduplicate_workout_samples(samples)
        counts: dict[str, int] = {}
        for sample in samples:
            counts[sample.metric] = counts.get(sample.metric, 0) + 1

        return WorkoutDetail(
            workout_id=workout_id,
            metrics_present=sorted(counts),
            metric_sample_counts=dict(sorted(counts.items())),
            laps=ZeppParser._workout_laps(data.get("lap")),
            pauses=ZeppParser._workout_pauses(data.get("pause")),
            strength_sets=ZeppParser._strength_sets(data.get("strengthSets")),
            samples=samples,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _workout_end(
        data: dict, start: datetime, summary_end: datetime | None
    ) -> datetime:
        time_span = 0
        raw_time = data.get("time")
        if isinstance(raw_time, str):
            for part in raw_time.split(";"):
                try:
                    time_span += max(int(part), 0)
                except ValueError:
                    continue
        end = start + timedelta(seconds=min(time_span, MAX_WORKOUT_SECONDS))
        if summary_end is not None:
            end = max(end, ZeppParser._utc(summary_end))
        return min(max(end, start + timedelta(seconds=1)), start + timedelta(seconds=MAX_WORKOUT_SECONDS))

    @staticmethod
    def _workout_heart_rate_samples(
        workout_id: str,
        start: datetime,
        end: datetime,
        encoded: object,
    ) -> list[WorkoutMetricSample]:
        if not isinstance(encoded, str) or not encoded:
            return []
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

        samples: list[WorkoutMetricSample] = []
        working = start
        heart_rate = 0
        for index, (seconds, delta) in enumerate(pairs):
            heart_rate += delta
            first_second = 0 if index == 0 else 1
            for _ in range(first_second, seconds + 1):
                if working > end:
                    break
                if 1 <= heart_rate <= 300:
                    samples.append(WorkoutMetricSample(
                        workout_id=workout_id,
                        timestamp=working,
                        metric="heart_rate",
                        value=float(heart_rate),
                        unit="bpm",
                    ))
                working += timedelta(seconds=1)
        while working <= end:
            if 1 <= heart_rate <= 300:
                samples.append(WorkoutMetricSample(
                    workout_id=workout_id,
                    timestamp=working,
                    metric="heart_rate",
                    value=float(heart_rate),
                    unit="bpm",
                ))
            working += timedelta(seconds=1)
        return samples

    @staticmethod
    def _absolute_workout_series(
        workout_id: str,
        start: datetime,
        encoded: object,
        metric: str,
        unit: str,
        scale: float,
        minimum: float,
        maximum: float,
    ) -> list[WorkoutMetricSample]:
        if not isinstance(encoded, str) or not encoded:
            return []
        elapsed = 0
        samples = []
        for part in encoded.split(";"):
            if not part:
                continue
            raw_seconds, separator, raw_value = part.partition(",")
            if not separator:
                continue
            try:
                elapsed += max(int(raw_seconds or 1), 0)
                value = float(raw_value) * scale
            except ValueError:
                continue
            if elapsed > MAX_WORKOUT_SECONDS or not minimum <= value <= maximum:
                continue
            samples.append(WorkoutMetricSample(
                workout_id=workout_id,
                timestamp=start + timedelta(seconds=elapsed),
                metric=metric,
                value=round(value, 4),
                unit=unit,
            ))
        return samples

    @staticmethod
    def _gait_samples(
        workout_id: str, start: datetime, encoded: object
    ) -> list[WorkoutMetricSample]:
        if not isinstance(encoded, str) or not encoded:
            return []
        elapsed = 0
        samples = []
        for part in encoded.split(";"):
            fields = part.split(",")
            if len(fields) < 4:
                continue
            try:
                elapsed += max(int(fields[0] or 1), 0)
                stride = float(fields[-2])
                cadence = float(fields[-1])
            except ValueError:
                continue
            if elapsed <= MAX_WORKOUT_SECONDS and 30 <= cadence <= 300:
                samples.append(WorkoutMetricSample(
                    workout_id=workout_id,
                    timestamp=start + timedelta(seconds=elapsed),
                    metric="cadence",
                    value=cadence,
                    unit="spm",
                ))
            if elapsed <= MAX_WORKOUT_SECONDS and 10 <= stride <= 300:
                samples.append(WorkoutMetricSample(
                    workout_id=workout_id,
                    timestamp=start + timedelta(seconds=elapsed),
                    metric="stride_length",
                    value=stride,
                    unit="cm",
                ))
        return samples

    @staticmethod
    def _run_posture_samples(
        workout_id: str, start: datetime, encoded: object
    ) -> list[WorkoutMetricSample]:
        if not isinstance(encoded, str) or not encoded:
            return []
        elapsed = 0
        samples = []
        for part in encoded.split(";"):
            fields = part.split(",")
            if len(fields) < 4:
                continue
            try:
                elapsed += max(int(fields[0] or 1), 0)
                contact = int(fields[1])
                oscillation = int(fields[2])
                ratio_raw = int(fields[3])
            except ValueError:
                continue
            if elapsed > MAX_WORKOUT_SECONDS:
                continue
            readings = (
                ("ground_contact_time", contact, "ms", contact != 65535 and 50 <= contact <= 1000),
                ("vertical_oscillation", oscillation, "mm", oscillation != 65535 and 1 <= oscillation <= 500),
                (
                    "vertical_stride_ratio",
                    ratio_raw / 10,
                    "%",
                    ratio_raw != 255 and 1 <= ratio_raw <= 1000,
                ),
            )
            for metric, value, unit, valid in readings:
                if valid:
                    samples.append(WorkoutMetricSample(
                        workout_id=workout_id,
                        timestamp=start + timedelta(seconds=elapsed),
                        metric=metric,
                        value=round(float(value), 2),
                        unit=unit,
                    ))
        return samples

    @staticmethod
    def _heart_rate_zone_boundaries(data: dict) -> list[int]:
        raw = data.get("heart_range")
        if raw is None:
            raw = data.get("heartRange")
        values: list[int] = []
        entries = raw if isinstance(raw, list) else str(raw).split(";") if raw else []
        for entry in entries:
            candidate = None
            if isinstance(entry, dict):
                candidate = ZeppParser._first_number(
                    entry, ("boundary", "upper", "max", "bpm", "value")
                )
            elif isinstance(entry, (list, tuple)) and entry:
                candidate = ZeppParser._first_number({"value": entry[-1]}, ("value",))
            elif isinstance(entry, (int, float)):
                candidate = float(entry)
            elif isinstance(entry, str):
                fields = [field.strip() for field in entry.split(",") if field.strip()]
                if fields:
                    candidate = ZeppParser._first_number({"value": fields[-1]}, ("value",))
            if candidate is not None and 30 <= candidate <= 250:
                values.append(int(candidate))
        if len(values) != 6 or any(left >= right for left, right in zip(values, values[1:])):
            return []
        return values

    @staticmethod
    def _workout_laps(encoded: object) -> list[WorkoutLap]:
        if not isinstance(encoded, str) or not encoded:
            return []
        output = []
        for part in encoded.split(";"):
            fields = part.split(",")
            if len(fields) < 3:
                continue
            try:
                index = int(fields[0])
                duration = int(float(fields[1]))
                distance = float(fields[2])
            except ValueError:
                continue
            if index >= 0 and duration >= 0 and distance >= 0:
                output.append(WorkoutLap(
                    index=index,
                    duration_seconds=duration,
                    distance_meters=distance,
                ))
        return output

    @staticmethod
    def _workout_pauses(encoded: object) -> list[WorkoutPause]:
        if not isinstance(encoded, str) or not encoded:
            return []
        output = []
        for part in encoded.split(";"):
            fields = part.split(",")
            if len(fields) < 2:
                continue
            started = ZeppParser._parse_datetime_value(fields[0])
            try:
                duration = int(fields[1])
            except ValueError:
                continue
            if started is not None and duration >= 0:
                output.append(WorkoutPause(
                    started_at=ZeppParser._utc(started),
                    duration_seconds=duration,
                ))
        return output

    @staticmethod
    def _strength_sets(encoded: object) -> list[StrengthSetObservation]:
        if not isinstance(encoded, str) or not encoded:
            return []
        try:
            items = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(items, list):
            return []
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            started = ZeppParser._parse_datetime_value(
                item.get("startTime") or item.get("startedAt") or item.get("timestamp")
            )
            ended = ZeppParser._parse_datetime_value(
                item.get("endTime") or item.get("endedAt")
            )
            output.append(StrengthSetObservation(
                started_at=ZeppParser._utc(started) if started else None,
                ended_at=ZeppParser._utc(ended) if ended else None,
                exercise_id=ZeppParser._first_text(
                    item, ("exerciseId", "exercise_id", "actionId", "movementId")
                ),
                exercise_name=ZeppParser._first_text(
                    item, ("exerciseName", "exercise_name", "name")
                ),
                repetitions=ZeppParser._bounded_int(
                    item, ("repetitions", "reps", "count"), 1, 1000
                ),
                weight_kg=ZeppParser._bounded_float(
                    item, ("weightKg", "weight_kg", "weight"), 0, 2000
                ),
                duration_seconds=ZeppParser._bounded_int(
                    item, ("durationSeconds", "duration", "workTime"), 0, MAX_WORKOUT_SECONDS
                ),
                rest_seconds=ZeppParser._bounded_int(
                    item, ("restSeconds", "rest", "restTime"), 0, MAX_WORKOUT_SECONDS
                ),
            ))
        return output

    @staticmethod
    def _deduplicate_workout_samples(
        samples: list[WorkoutMetricSample],
    ) -> list[WorkoutMetricSample]:
        unique = {(item.metric, item.timestamp): item for item in samples}
        return sorted(unique.values(), key=lambda item: (item.timestamp, item.metric))

    @staticmethod
    def _first_text(data: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _bounded_float(
        data: dict, keys: tuple[str, ...], minimum: float, maximum: float
    ) -> float | None:
        value = ZeppParser._first_number(data, keys)
        if value is None or not minimum <= value <= maximum:
            return None
        return float(value)

    @staticmethod
    def _bounded_int(
        data: dict, keys: tuple[str, ...], minimum: int, maximum: int
    ) -> int | None:
        value = ZeppParser._bounded_float(data, keys, minimum, maximum)
        return int(value) if value is not None else None

    # ================= 通用指标（时序 + 每日） =================

    @staticmethod
    def parse_heart_rate_samples(raw: dict) -> list[MetricSample]:
        samples: list[MetricSample] = []
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict):
                continue
            encoded = item.get("heartRateData")
            if isinstance(encoded, str):
                try:
                    decoded = b64decode(encoded, validate=True)
                except (BinasciiError, ValueError):
                    decoded = b""
                # Verified against 46 real type-2 endpoint rows: one unsigned
                # byte is one measurement at generatedTime. Multi-byte payloads
                # remain unsupported until their sampling interval is verified.
                timestamp = ZeppParser._parse_datetime_value(item.get("generatedTime"))
                value = decoded[0] if len(decoded) == 1 else None
                if timestamp is not None and value is not None and 0 <= value <= 300:
                    device_id = ZeppParser._device_id(item)
                    samples.append(MetricSample(
                        metric="heart_rate",
                        timestamp=timestamp,
                        value=float(value),
                        unit="bpm",
                        source_scope="device" if device_id else "user_fused",
                        device_id=device_id,
                    ))
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
            ("skin_temp_readiness", ("skinTempScore",), "score"),
            ("ahi_readiness", ("ahiScore",), "score"),
            ("bio_charge", ("bio_charge", "bioCharge", "bodyBattery", "chargeScore"), "score"),
            ("hybrid_charge", ("hybrid_charge", "hybridCharge", "hybridChargeScore"), "score"),
            ("training_load", ("training_load", "trainingLoad", "wtlSum", "currnetDayTrainLoad", "load"), "load"),
            ("vo2max", ("vo2max", "vo2Max", "VO2_MAX", "VO2_max", "vo2_max_run", "vo2_max_walking"), "ml/kg/min"),
            ("stress", ("stress", "stressScore", "avgStress", "averageStress"), "score"),
            ("spo2", ("spo2", "bloodOxygen", "blood_oxygen"), "%"),
            ("running_distance", ("totalRunningDistance",), "m"),
            ("cycling_distance", ("totalCyclingDistance",), "m"),
        )
        output: dict[tuple[date, str, str, str], DailyMetric] = {}

        def store(item: DailyMetric) -> None:
            output[(
                item.date,
                item.metric,
                item.source_scope or "unknown",
                item.device_id or "",
            )] = item

        for item in ZeppParser._items(raw):
            if not isinstance(item, dict):
                continue
            event_value = item.get("value") if isinstance(item.get("value"), dict) else None
            if item.get("eventType") == "Charge" and event_value:
                raw_samples = event_value.get("samples") or []
                valid = []
                for sample in raw_samples:
                    if not isinstance(sample, dict):
                        continue
                    total = ZeppParser._first_number(sample, ("total",))
                    if total is not None and 0 <= total <= 100:
                        valid.append(sample)
                latest = max(
                    valid,
                    key=lambda sample: ZeppParser._first_number(sample, ("s", "offset")) or 0,
                    default=None,
                )
                day = ZeppParser._metric_date(item, event_value)
                if latest and day:
                    for metric, key in (
                        ("hybrid_charge", "total"),
                        ("physical_charge", "physical"),
                        ("mental_charge", "mental"),
                    ):
                        reading = ZeppParser._first_number(latest, (key,))
                        if reading is not None and 0 <= reading <= 100:
                            store(DailyMetric(
                                date=day, metric=metric, value=reading, unit="score",
                                source_scope="user_fused",
                            ))
                continue
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
                    store(DailyMetric(
                        date=day, metric=metric, value=value, unit=unit,
                        source_scope="device" if device else "user_fused", device_id=device,
                    ))
                if nested and item.get("eventType") == "readiness":
                    supplemental = (
                        ("skin_temp_delta", "skinTempCalibrated", "C", 0.01, -2.0, 2.0),
                        ("skin_temp_baseline_delta", "skinTempBaseLine", "C", 0.01, -2.0, 2.0),
                        ("sleep_hrv", "sleepHRV", "ms", 1.0, 1.0, 400.0),
                        ("sleep_rhr", "sleepRHR", "bpm", 1.0, 20.0, 250.0),
                        ("hrv_baseline", "hrvBaseline", "ms", 1.0, 1.0, 400.0),
                        ("rhr_baseline", "rhrBaseline", "bpm", 1.0, 20.0, 250.0),
                    )
                    for metric, key, unit, scale, minimum, maximum in supplemental:
                        reading = ZeppParser._first_number(nested, (key,))
                        if reading is None:
                            continue
                        reading *= scale
                        if minimum <= reading <= maximum:
                            store(DailyMetric(
                                date=day, metric=metric, value=reading, unit=unit,
                                source_scope="device" if device else "user_fused",
                                device_id=device,
                            ))
        return list(output.values())

    @staticmethod
    def parse_charge_samples(raw: dict) -> list[MetricSample]:
        """Normalize verified Charge samples without decoding opaque stress blobs."""
        output: list[MetricSample] = []
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict) or item.get("eventType") != "Charge":
                continue
            value = item.get("value") if isinstance(item.get("value"), dict) else None
            if not value:
                continue
            start = ZeppParser._parse_datetime_value(value.get("startTime"))
            raw_samples = value.get("samples") or []
            if start is None or not isinstance(raw_samples, list):
                continue
            device_ids = ZeppParser._expand_sample_device_ids(value.get("deviceId"), len(raw_samples))
            for index, sample in enumerate(raw_samples):
                if not isinstance(sample, dict):
                    continue
                offset = ZeppParser._first_number(sample, ("s", "offset")) or 0
                timestamp = start + timedelta(milliseconds=offset)
                for metric, key in (
                    ("hybrid_charge", "total"),
                    ("physical_charge", "physical"),
                    ("mental_charge", "mental"),
                ):
                    reading = ZeppParser._first_number(sample, (key,))
                    if reading is None or not 0 <= reading <= 100:
                        continue
                    device_id = device_ids[index]
                    output.append(MetricSample(
                        metric=metric, timestamp=timestamp, value=reading, unit="score",
                        source_scope="device" if device_id else "user_fused",
                        device_id=device_id,
                    ))
        return output

    @staticmethod
    def parse_readiness_samples(raw: dict) -> list[MetricSample]:
        """Preserve each device-scoped readiness event as a timestamped series."""
        fields = (
            ("readiness", "rdnsScore", "score", 1.0, 0.0, 100.0),
            ("physical_readiness", "phyScore", "score", 1.0, 0.0, 100.0),
            ("mental_readiness", "mentScore", "score", 1.0, 0.0, 100.0),
            ("hrv_readiness", "hrvScore", "score", 1.0, 0.0, 100.0),
            ("rhr_readiness", "rhrScore", "score", 1.0, 0.0, 100.0),
            ("skin_temp_readiness", "skinTempScore", "score", 1.0, 0.0, 100.0),
            ("ahi_readiness", "ahiScore", "score", 1.0, 0.0, 100.0),
            ("afib_readiness", "afibScore", "score", 1.0, 0.0, 100.0),
            ("skin_temp_delta", "skinTempCalibrated", "C", 0.01, -2.0, 2.0),
            ("skin_temp_baseline_delta", "skinTempBaseLine", "C", 0.01, -2.0, 2.0),
            ("sleep_hrv", "sleepHRV", "ms", 1.0, 1.0, 400.0),
            ("sleep_rhr", "sleepRHR", "bpm", 1.0, 20.0, 250.0),
            ("hrv_baseline", "hrvBaseline", "ms", 1.0, 1.0, 400.0),
            ("rhr_baseline", "rhrBaseline", "bpm", 1.0, 20.0, 250.0),
        )
        output: list[MetricSample] = []
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict) or item.get("eventType") != "readiness":
                continue
            value = item.get("value") if isinstance(item.get("value"), dict) else None
            if not value:
                continue
            timestamp = ZeppParser._parse_datetime_value(
                value.get("timestamp") or item.get("timestamp")
            )
            if timestamp is None:
                continue
            device_id = ZeppParser._device_id(value) or ZeppParser._device_id(item)
            for metric, key, unit, scale, minimum, maximum in fields:
                reading = ZeppParser._first_number(value, (key,))
                if reading is None:
                    continue
                reading *= scale
                if minimum <= reading <= maximum:
                    output.append(MetricSample(
                        metric=metric, timestamp=timestamp, value=reading, unit=unit,
                        source_scope="device" if device_id else "user_fused",
                        device_id=device_id,
                    ))
        return output

    @staticmethod
    def parse_hrv_samples(raw: dict, metric: str = "hrv_sdnn") -> list[MetricSample]:
        """Normalize SDNN/RMSSD event samples with per-sample device attribution."""
        key = "hrv" if metric == "hrv_rmssd" else "sdnn"
        output: list[MetricSample] = []
        for item in ZeppParser._items(raw):
            value = item.get("value") if isinstance(item, dict) and isinstance(item.get("value"), dict) else None
            if not value:
                continue
            start = ZeppParser._parse_datetime_value(value.get("startTime"))
            raw_samples = value.get("samples") or []
            if start is None or not isinstance(raw_samples, list):
                continue
            device_ids = ZeppParser._expand_sample_device_ids(value.get("deviceId"), len(raw_samples))
            for index, sample in enumerate(raw_samples):
                if not isinstance(sample, dict):
                    continue
                reading = ZeppParser._first_number(sample, (key, "rmssd" if key == "hrv" else key))
                if reading is None or not 1 <= reading <= 400:
                    continue
                offset = ZeppParser._first_number(sample, ("s", "offset")) or 0
                device_id = device_ids[index]
                output.append(MetricSample(
                    metric=metric,
                    timestamp=start + timedelta(milliseconds=offset),
                    value=reading,
                    unit="ms",
                    source_scope="device" if device_id else "unknown",
                    device_id=device_id,
                ))
        return output

    @staticmethod
    def parse_dense_file_index(
        raw: dict, stream: str = "second_heart_rate"
    ) -> list[DenseDataFile]:
        """Normalize opaque file metadata without claiming the payload is decoded."""
        output: list[DenseDataFile] = []
        for event in ZeppParser._items(raw):
            if not isinstance(event, dict):
                continue
            value = event.get("value") if isinstance(event.get("value"), dict) else {}
            samples = value.get("samples") or []
            if not isinstance(samples, list):
                continue
            base = ZeppParser._parse_datetime_value(value.get("startTime"))
            device_ids = ZeppParser._expand_sample_device_ids(
                value.get("deviceId"), len(samples)
            )
            for index, sample in enumerate(samples):
                if not isinstance(sample, dict):
                    continue
                file_id = str(sample.get("fileId") or "").strip()
                if not file_id:
                    continue
                day = None
                date_text = sample.get("dateString")
                if isinstance(date_text, str):
                    try:
                        day = date.fromisoformat(date_text[:10])
                    except ValueError:
                        pass
                start_offset = ZeppParser._first_number(sample, ("s",))
                end_offset = ZeppParser._first_number(sample, ("e",))
                start_utc = (
                    base + timedelta(milliseconds=start_offset)
                    if base is not None and start_offset is not None and start_offset >= 0
                    else None
                )
                end_utc = (
                    base + timedelta(milliseconds=end_offset)
                    if base is not None
                    and end_offset is not None
                    and start_offset is not None
                    and end_offset > start_offset
                    else None
                )
                device_id = device_ids[index]
                output.append(DenseDataFile(
                    stream=stream,
                    file_id=file_id,
                    file_type=str(sample.get("fileType") or ""),
                    date=day or (start_utc.date() if start_utc else None),
                    start_utc=start_utc,
                    end_utc=end_utc,
                    source_scope="device" if device_id else "unknown",
                    device_id=device_id,
                    parse_status="indexed",
                    sample_count=0,
                ))
        return output

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
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit="brpm",
                            source_scope="unknown",
                        ))
        elif label == "hrv_rmssd":
            samples.extend(ZeppParser.parse_hrv_samples(raw, "hrv_rmssd"))
        elif label == "lactate_threshold":
            for item in items:
                value = item.get("value") if isinstance(item, dict) and isinstance(item.get("value"), dict) else None
                for sample in (value or {}).get("samples") or []:
                    if not isinstance(sample, dict):
                        continue
                    day = ZeppParser._metric_date(sample, None)
                    if day is None:
                        continue
                    for metric, key, unit, minimum, maximum in (
                        ("lactate_threshold_hr", "lactateThresholdHr", "bpm", 60, 230),
                        ("lactate_threshold_pace", "lactateThresholdPace", "s/km", 100, 1800),
                    ):
                        reading = ZeppParser._first_number(sample, (key,))
                        if reading is not None and minimum <= reading <= maximum:
                            daily.append(DailyMetric(
                                date=day, metric=metric, value=reading, unit=unit,
                                source_scope="user_fused",
                            ))
        elif label == "spo2":
            import json
            for item in items:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("subType")
                day = ZeppParser._metric_date(item, None)
                if subtype == "odi" and day:
                    device_id = ZeppParser._device_id(item)
                    source_scope = "device" if device_id else "unknown"
                    for metric, keys, unit in (
                        ("spo2_odi", ("odi",), "events/h"),
                        ("spo2_odi_events", ("odiNum",), "count"),
                        ("spo2_night_score", ("score",), "score"),
                    ):
                        reading = ZeppParser._first_number(item, keys)
                        if reading is not None:
                            daily.append(DailyMetric(
                                date=day, metric=metric, value=reading, unit=unit,
                                source_scope=source_scope, device_id=device_id,
                            ))
                    seconds = ZeppParser._first_number(item, ("cost",))
                    if seconds is not None and 60 <= seconds <= 86_400:
                        daily.append(DailyMetric(
                            date=day, metric="spo2_measured_minutes",
                            value=round(seconds / 60), unit="min",
                            source_scope=source_scope, device_id=device_id,
                        ))
                    continue
                try:
                    extra = json.loads(item.get("extra", "{}"))
                except (TypeError, json.JSONDecodeError):
                    continue
                reading = ZeppParser._first_number(extra, ("spo2", "value"))
                timestamp = ZeppParser._parse_datetime_value(extra.get("timestamp") or item.get("timestamp"))
                if reading is not None and 50 <= reading <= 100 and timestamp:
                    device_id = ZeppParser._device_id(extra) or ZeppParser._device_id(item)
                    samples.append(MetricSample(
                        metric="spo2", timestamp=timestamp, value=reading, unit="%",
                        source_scope="device" if device_id else "unknown", device_id=device_id,
                    ))
                apnea = ZeppParser._first_number(extra, ("spo2_decrease", "spo2Decrease"))
                if subtype == "osa_event" and apnea is not None and 50 <= apnea <= 100 and timestamp:
                    device_id = ZeppParser._device_id(extra) or ZeppParser._device_id(item)
                    samples.append(MetricSample(
                        metric="spo2_apnea_low", timestamp=timestamp, value=apnea, unit="%",
                        source_scope="device" if device_id else "unknown", device_id=device_id,
                    ))
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
                        device_id = ZeppParser._device_id(item)
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit=unit,
                            source_scope="device" if device_id else "unknown", device_id=device_id,
                        ))
        elif label == "all_day_stress":
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("value") if isinstance(item.get("value"), dict) else None
                day = ZeppParser._metric_date(item, value)
                if day is None:
                    continue
                device_id = ZeppParser._device_id(value or {}) or ZeppParser._device_id(item)
                for metric, keys, unit in (
                    ("stress", ("avgStress", "averageStress", "stress"), "score"),
                    ("stress_min", ("minStress",), "score"),
                    ("stress_max", ("maxStress",), "score"),
                    ("stress_relaxed_pct", ("relaxPct", "relaxProportion"), "%"),
                    ("stress_normal_pct", ("normalPct", "normalProportion"), "%"),
                    ("stress_medium_pct", ("mediumPct", "mediumProportion"), "%"),
                    ("stress_high_pct", ("highPct", "highProportion"), "%"),
                ):
                    reading = ZeppParser._first_number(item, keys)
                    if reading is None and value:
                        reading = ZeppParser._first_number(value, keys)
                    if reading is not None and 0 <= reading <= 100:
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit=unit,
                            source_scope="device" if device_id else "unknown", device_id=device_id,
                        ))
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
            device_id = ZeppParser._device_id({"deviceId": text})
            return [device_id] * sample_count

        expanded: list[str | None] = []
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
            normalized = ZeppParser._device_id({"deviceId": device_id})
            expanded.extend([normalized] * count)

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
            rem_sleep=(
                int(stages["rem"]) if stages.get("rem") is not None else None
            ),
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
        additional = raw.get("additionalInfo")
        if isinstance(additional, str):
            try:
                additional = json.loads(additional)
            except ValueError:
                additional = {}
        if not isinstance(additional, dict):
            additional = {}
        product_id = str(additional.get("productId") or "")
        model = (
            _PRODUCT_MODELS.get(product_id)
            or raw.get("displayName")
            or raw.get("model")
            or (f"Zepp product {product_id}" if product_id else "Zepp device")
        )
        device_id = self._normalize_device_identifier(
            additional.get("btmac") or raw.get("macAddress")
        ) or self._device_id(raw)
        return Device(
            user_id="",
            source=self.source,
            model=str(model),
            device_id=device_id or "",
        )

    def parse_devices(self, raw: dict) -> list[Device]:
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return [
            device
            for item in items
            if isinstance(item, dict)
            and (device := self.parse_device(item)).device_id
        ]

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
    def _band_items(raw: dict) -> list[dict]:
        if not isinstance(raw, dict):
            return []
        data = raw.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [item for item in data["items"] if isinstance(item, dict)]
        return []

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
    def _sleep_stage_minutes(sleep: dict, modes: set[int]) -> int | None:
        stages = sleep.get("stage")
        if not isinstance(stages, list):
            return None
        total = 0
        found = False
        valid_stage_found = False
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            mode = ZeppParser._first_number(stage, ("mode",))
            start = ZeppParser._first_number(stage, ("start",))
            stop = ZeppParser._first_number(stage, ("stop",))
            if mode is None or start is None or stop is None or stop < start:
                continue
            valid_stage_found = True
            if int(mode) in modes:
                found = True
                total += int(stop) - int(start) + 1
        if found:
            return total
        if valid_stage_found and sleep.get("supRem") in (True, 1, "1"):
            return 0
        return None

    @staticmethod
    def _sleep_stage_slices(
        day: date,
        summary: dict,
        sleep: dict,
        session_start: object,
        session_end: object,
    ) -> list:
        from vitalis.models import SleepStageSlice

        stages = sleep.get("stage")
        if not isinstance(stages, list):
            return []
        offset = ZeppParser._first_number(summary, ("tz",)) or 0
        offset = max(min(int(offset), 18 * 60 * 60), -18 * 60 * 60)
        utc_midnight = (
            datetime.combine(day, time.min, tzinfo=timezone.utc)
            - timedelta(seconds=offset)
        )
        names = {5: "deep", 4: "light", 8: "rem", 11: "rem", 7: "awake"}

        def build(anchor: datetime) -> list[SleepStageSlice]:
            output = []
            for item in stages:
                if not isinstance(item, dict):
                    continue
                mode = ZeppParser._first_number(item, ("mode",))
                start = ZeppParser._first_number(item, ("start",))
                stop = ZeppParser._first_number(item, ("stop",))
                if mode is None or start is None or stop is None or stop < start:
                    continue
                start_time = anchor + timedelta(minutes=int(start))
                end_time = anchor + timedelta(minutes=int(stop) + 1)
                output.append(SleepStageSlice(
                    stage=names.get(int(mode), "awake"),
                    start_time=start_time,
                    end_time=end_time,
                ))
            return output

        previous_day = build(utc_midnight - timedelta(days=1))
        start_at = ZeppParser._parse_datetime_value(session_start)
        end_at = ZeppParser._parse_datetime_value(session_end)
        if start_at is None or end_at is None:
            return previous_day
        start_at, end_at = ZeppParser._utc(start_at), ZeppParser._utc(end_at)
        same_day = build(utc_midnight)

        def overlap(items: list[SleepStageSlice]) -> float:
            return sum(
                max(
                    0.0,
                    (min(item.end_time, end_at) - max(item.start_time, start_at)).total_seconds(),
                )
                for item in items
            )

        return same_day if overlap(same_day) > overlap(previous_day) else previous_day

    @staticmethod
    def _normalize_device_identifier(value: object) -> str | None:
        normalized = "".join(
            character for character in str(value or "") if character.isalnum()
        ).upper()
        return normalized if len(normalized) >= 8 else None

    @staticmethod
    def _device_id(obj: dict) -> str | None:
        for key in ("deviceId", "device_id", "did", "mac"):
            value = obj.get(key)
            if value is None:
                continue
            candidates = [
                segment.strip()
                for segment in str(value).split(",")
                if len(segment.strip()) >= 8 and segment.strip().isalnum()
            ]
            if candidates:
                return max(candidates, key=len)
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
