"""Zepp 厂商格式 -> Vitalis Schema 的解析器。

职责：把 Zepp 原始 JSON 转换成统一的 SleepRecord / ActivityRecord /
TrainingRecord / Workout。此处是「厂商格式隔离」的关键边界 ——
上层模块永远看不到 Zepp 的字段名（sleepScore/stages 等）。
"""

from base64 import b64decode
from binascii import Error as BinasciiError
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import isfinite
import re

from vitalis.domain import (
    ActivityRecord,
    DailyMetric,
    DenseDataFile,
    Device,
    MetricSample,
    NapRecord,
    SleepRecord,
    SleepStageSlice,
    WorkoutHeartRateZone,
    TrainingRecord,
    Workout,
    WorkoutRpe,
    WorkoutDetail,
    WorkoutLap,
    WorkoutMetricSample,
    WorkoutPause,
    StrengthSetObservation,
    WorkoutType,
)
from .catalog import LAP_62_NAMESPACE, resolve_exercise, verified_lap_labels
from .sport_types import resolve_cloud_sport_mode, resolve_sport_mode
from .parsers.supplemental import exertion_metrics, number, sleep_timing, weight_samples, workout_supplements

MAX_WORKOUT_SECONDS = 12 * 60 * 60
MAX_WELLNESS_SAMPLES_PER_EVENT = 10_000
# A run expands to roughly 10 samples per second across heart rate, speed,
# pace, distance, altitude, power and gait; 250,000 covers about six and a half
# hours.  Measured on the server 2026-10-14: a 4 h 39 min marathon is 167,123
# samples from a 1.6 MB response, parsed in 4.1 s at a 309 MB process peak
# (20,000 had rejected every ordinary 35-minute run).
MAX_WORKOUT_DETAIL_SAMPLES = 250_000
MAX_WORKOUT_DETAIL_LAP_ROWS = 2_000
MAX_WORKOUT_DETAIL_PAUSE_ROWS = 10_000
MAX_WORKOUT_DETAIL_STRENGTH_ROWS = 2_000


class WorkoutDetailLimitError(ValueError):
    """The detail payload exceeds bounded parsing limits."""

# Compatibility view for internal callers; the packaged JSON is the only label source.
ZEPP_STRENGTH_LAP_LABELS = verified_lap_labels()

_ALL_DAY_STRESS_FIELDS = (
    ("stress", ("avgStress", "averageStress", "stress"), "score"),
    ("stress_min", ("minStress",), "score"),
    ("stress_max", ("maxStress",), "score"),
    ("stress_relaxed_pct", ("relaxPct", "relaxProportion"), "%"),
    ("stress_normal_pct", ("normalPct", "normalProportion"), "%"),
    ("stress_medium_pct", ("mediumPct", "mediumProportion"), "%"),
    ("stress_high_pct", ("highPct", "highProportion"), "%"),
)

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

_DEVICE_ID_FIELDS = (
    "deviceId", "device_id", "deviceid", "sourceDeviceId", "did",
    "mac", "Mac", "macAddress", "deviceMac",
)
_ACCOUNT_SUMMARY_EVENTS = {"DailyHealth", "Charge", "readiness"}
_ACCOUNT_SUMMARY_KEYS = (
    "events:DailyHealth:summary:", "events:Charge:real_data:",
    "events:readiness:watch_score:", "WatchSportStatistics:SPORT_LOAD:",
    "WatchSportStatistics:VO2_MAX:",
)

class ZeppParser:
    """把 Zepp 原始响应解析为 Vitalis Schema 对象。

    支持两类原始格式：
      - 旧 mock 格式（get_sleep/get_activity/get_training 直出）
      - 真实 band_data / sport_history 格式（apptoken 模式，对齐 ZeppBridge）
    """

    def __init__(self, source: str = "zepp"):
        self.source = source

    # ================= 真实 band_data（睡眠 + 活动） =================

    def parse_band(
        self, raw: dict, timezone_name: str | None = None,
    ) -> tuple[dict[date, SleepRecord], dict[date, ActivityRecord]]:
        """解析手环原始数据 -> {日期: SleepRecord}, {日期: ActivityRecord}。

        band_data.items[].summary 是 base64 编码的 JSON：
          slp: 睡眠 {ss分数, st/ed起止, dp/lt/rm/wk分钟, stage分期, rhr静息心率}
          stp: 步数 {ttl, cal, dis(米)}
          tz:  时区偏移（秒）
        """

        sleeps: dict[date, SleepRecord] = {}
        activities: dict[date, ActivityRecord] = {}
        for item in self._band_items(raw):
            # The day-keyed return type cannot join provenance across items.
            naps_by_day = self.parse_band_naps({"data": {"items": [item]}}, timezone_name)
            day = self._item_date(item)
            summary = self._base64_summary(item)
            if not day or not summary:
                continue
            source_scope, device_id = self._band_provenance(item, summary)
            _, contributing_device_id = self._provenance(item, summary, {"deviceId": summary.get("sn")})

            sleep = summary.get("slp") if isinstance(summary.get("slp"), dict) else {}
            sleep_scope, sleep_device = self._band_provenance(item, summary, sleep=True)
            if sleep.get("ed") and sleep.get("st"):
                st_val, ed_val = sleep.get("st"), sleep.get("ed")
                # 真实数据 st/ed 可能是时间戳（int）或 HH:MM 字符串
                if isinstance(st_val, (int, float)) and isinstance(ed_val, (int, float)):
                    st_dt = datetime.fromtimestamp(st_val, tz=timezone.utc)
                    ed_dt = datetime.fromtimestamp(ed_val, tz=timezone.utc)
                    span = int((ed_dt - st_dt).total_seconds() // 60)
                    duration = max(span - int(sleep.get("wk", 0) or 0), 0)
                    zone = self._band_timezone(summary, timezone_name)
                    bedtime = st_dt.astimezone(zone).time().replace(tzinfo=None)
                    wake_time = ed_dt.astimezone(zone).time().replace(tzinfo=None)
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
                    rem = self._first_number(sleep, ("dt", "rm", "remMinutes", "rem"))
                    if rem is None:
                        rem = self._sleep_stage_minutes(sleep, {8, 11})
                    sleeps[day] = SleepRecord(
                        user_id="", source=self.source, date=day,
                        source_scope=sleep_scope, device_id=sleep_device,
                        contributing_device_id=contributing_device_id if sleep_scope == "user_fused" else None,
                        is_merged=self._merge_flag(sleep.get("isMerged")),
                        sleep_duration=duration,
                        deep_sleep=self._nonnegative_number(sleep, ("dp",)),
                        rem_sleep=int(rem) if rem is not None else None,
                        light_sleep=self._nonnegative_number(sleep, ("lt",)),
                        awake=self._nonnegative_number(sleep, ("wk",)),
                        observed_fields=[
                            name for name, key in (
                                ("deep_sleep", "dp"), ("light_sleep", "lt"), ("awake", "wk")
                            ) if sleep.get(key) is not None
                            and not isinstance(sleep[key], bool)
                            and (value := self._first_number(sleep, (key,))) is not None
                            and value >= 0
                        ],
                        sleep_score=int(sleep["ss"]) if sleep.get("ss") is not None else None,
                        bedtime=bedtime,
                        wake_time=wake_time,
                        stages=self._sleep_stage_slices(
                            day, summary, sleep, st_val, ed_val, timezone_name
                        ),
                        wake_count=self._bounded_int(sleep, ("wc",), 0, 100),
                        naps=naps_by_day.get(day, []),
                        naps_known=day in naps_by_day,
                        **sleep_timing(sleep, self._band_timezone(summary, timezone_name), duration),
                    )

            steps = summary.get("stp") or {}
            if isinstance(steps, dict):
                steps_value = self._nonnegative_number(steps, ("ttl",))
                calories_value = self._nonnegative_number(steps, ("cal",))
                distance_meters = self._nonnegative_number(steps, ("dis",))
                active_minutes = self._nonnegative_number(
                    summary,
                    ("activeMinutes", "active_minutes"),
                )
                resting_hr = self._nonnegative_number(sleep, ("rhr",))
                observed_fields = []
                if steps_value is not None:
                    observed_fields.append("steps")
                if active_minutes is not None:
                    observed_fields.append("active_minutes")
                if calories_value is not None:
                    observed_fields.append("calories")
                if distance_meters is not None:
                    observed_fields.append("distance_km")
                if resting_hr is not None:
                    observed_fields.append("resting_hr")
                if observed_fields:
                    activities[day] = ActivityRecord(
                        user_id="", source=self.source, date=day,
                        source_scope=source_scope, device_id=device_id,
                        contributing_device_id=contributing_device_id if source_scope == "user_fused" else None,
                        is_merged=self._merge_flag(summary.get("isMerged")),
                        steps=int(steps_value) if steps_value is not None else None,
                        active_minutes=(
                            int(active_minutes) if active_minutes is not None else None
                        ),
                        calories=int(calories_value) if calories_value is not None else None,
                        distance_km=(
                            round(distance_meters / 1000, 3)
                            if distance_meters is not None else None
                        ),
                        resting_hr=int(resting_hr) if resting_hr is not None else None,
                        observed_fields=observed_fields,
                    )
        return sleeps, activities

    def parse_band_naps(
        self, raw: dict, timezone_name: str | None = None,
    ) -> dict[date, list[NapRecord]]:
        """Read explicit nap coverage without manufacturing a main sleep.

        A missing key (or malformed list) is unknown; an empty list is known
        absence. The same verified stage anchor is used for main sleep and naps.
        Adjacent stages, including awake stages, form one nap. Any gap splits it.
        Protocol evidence: J-solan/zepp-dashboard docs/zepp-api.md, Sleep section:
        https://github.com/J-solan/zepp-dashboard/blob/main/docs/zepp-api.md
        """
        output: dict[date, list[NapRecord]] = {}
        for item in self._band_items(raw):
            day = self._item_date(item)
            summary = self._base64_summary(item)
            sleep = summary.get("slp") if summary else None
            if day is None or not isinstance(sleep, dict):
                continue
            source_scope, device_id = self._band_provenance(item, summary, sleep=True)
            _, contributing_device_id = self._provenance(item, summary, {"deviceId": summary.get("sn")})
            encoded = sleep.get("odd_stage")
            if not isinstance(encoded, list):
                continue
            anchor = self._sleep_stage_anchor(
                day, summary, sleep, sleep.get("st"), sleep.get("ed"), timezone_name
            )
            stages = self._stage_slices(encoded, anchor)
            if len(stages) != len(encoded):
                continue  # Malformed coverage is not a known zero-minute day.
            stages.sort(key=lambda stage: (stage.start_time, stage.end_time))
            if any(right.start_time < left.end_time for left, right in zip(stages, stages[1:])):
                continue  # Conflicting intervals cannot be summed or guessed apart.
            groups: list[list[SleepStageSlice]] = []
            for stage in stages:
                if groups and stage.start_time == groups[-1][-1].end_time:
                    groups[-1].append(stage)
                else:
                    groups.append([stage])
            naps = []
            for group in groups:
                known = all(stage.stage != "unknown" for stage in group)
                asleep = sum(
                    int((stage.end_time - stage.start_time).total_seconds() // 60)
                    for stage in group if stage.stage in {"deep", "light", "rem"}
                )
                awake = sum(
                    int((stage.end_time - stage.start_time).total_seconds() // 60)
                    for stage in group if stage.stage == "awake"
                )
                naps.append(NapRecord(
                    started_at=group[0].start_time, ended_at=group[-1].end_time,
                    sleep_minutes=asleep if known else None,
                    awake_minutes=awake if known else None,
                    stages=group, source=self.source,
                    source_scope=source_scope, device_id=device_id,
                    contributing_device_id=contributing_device_id if source_scope == "user_fused" else None,
                    is_merged=self._merge_flag(sleep.get("isMerged")),
                ))
            output[day] = naps
        return output

    @staticmethod
    def parse_band_heart_rate(
        raw: dict, timezone_name: str | None = None,
    ) -> list[MetricSample]:
        """Decode local minute slots using the frozen zone, then payload offset.

        Resolve each clock slot before converting to UTC so a DST transition
        does not leave the whole day on midnight's offset.
        """
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
            zone = ZeppParser._band_timezone(summary, timezone_name)
            midnight = datetime.combine(day, time.min, tzinfo=zone)
            provenance = ZeppParser._sample_provenance(
                [None] * len(readings), item, summary, {"deviceId": summary.get("sn")},
            )
            for minute, reading in enumerate(readings[:1440]):
                if not 20 <= reading <= 240:
                    continue
                source_scope, device_id = provenance[minute]
                contributing_device_id = device_id if ZeppParser._merge_flag(summary.get("isMerged")) else None
                if contributing_device_id is not None or ZeppParser._merge_flag(summary.get("isMerged")):
                    source_scope, device_id = "user_fused", None
                samples.append(MetricSample(
                    metric="heart_rate",
                    timestamp=(midnight + timedelta(minutes=minute)).astimezone(timezone.utc),
                    value=float(reading),
                    unit="bpm",
                    source_scope=source_scope, device_id=device_id,
                    contributing_device_id=contributing_device_id,
                    source_field="band_data.data_hr",
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
            if isinstance(type_id, int) and not isinstance(type_id, bool):
                numeric_type = type_id
            elif isinstance(type_id, float) and isfinite(type_id) and type_id.is_integer():
                numeric_type = int(type_id)
            elif isinstance(type_id, str):
                try:
                    numeric_type = int(type_id.strip())
                except ValueError:
                    pass
            # The new /run endpoint uses the cloud namespace. Replayed legacy
            # per-sport chunks retain the public Zepp enum for non-run paths.
            mode = (
                resolve_cloud_sport_mode(numeric_type)
                if sport_hint in {"", "run"}
                else resolve_sport_mode(numeric_type)
            )
            wtype = WorkoutType(mode.category)
            started = self._parse_start(it)
            avg_hr = self._nonnegative_number(
                it, ("avg_hr", "avgHr", "avg_heart_rate")
            )
            max_hr = self._nonnegative_number(
                it, ("max_hr", "maxHr", "max_heart_rate")
            )
            load = self._nonnegative_number(
                it, ("training_load", "trainLoad", "exercise_load")
            )
            calories = self._nonnegative_number(it, ("calories", "calorie"))
            precise_distance = number(it.get("highPrecisionDistance"), 0.001, 1e7)
            distance = precise_distance if precise_distance is not None else self._nonnegative_number(it, ("distance", "dis"))
            groups = self._nonnegative_number(it, ("total_group",)) if mode.family == "strength" else None
            reported_sets = int(groups) if groups is not None and groups > 0 and groups.is_integer() else None
            rpe_value = it.get("rpe")
            session_rpe = (
                WorkoutRpe(
                    value=float(rpe_value), source=self.source,
                    source_scope="workout_summary", source_field="rpe",
                    observed_at=started,
                )
                if isinstance(rpe_value, (int, float)) and not isinstance(rpe_value, bool)
                and 1 <= rpe_value <= 10 and isfinite(rpe_value)
                else None
            )
            observed_fields = [
                name for name, value in (
                    ("heart_rate_avg", avg_hr), ("heart_rate_max", max_hr),
                    ("load", load),
                ) if value is not None
            ]
            auto_recognized = it.get("auto_recognition")
            if not isinstance(auto_recognized, bool):
                auto_recognized = None
            title_value = it.get("sport_title")
            title = (title_value.strip() or None) if isinstance(title_value, str) else None
            # Protocol evidence: J-solan/zepp-dashboard documents the observed
            # te encoding as tenths on a 0–5 scale, separately from exercise_load.
            # https://github.com/J-solan/zepp-dashboard/blob/main/ingest/parsers.py
            raw_effect = self._bounded_float(it, ("te",), 0, 50)
            training_effect = raw_effect / 10 if raw_effect is not None else None
            vendor_zones = self._vendor_heart_rate_zones(it)
            observed_fields.extend(name for name, value in (
                ("auto_recognized", auto_recognized), ("title", title),
                ("training_effect", training_effect),
            ) if value is not None)
            if vendor_zones:
                observed_fields.append("vendor_heart_rate_zones")
            if reported_sets is not None:
                observed_fields.append("vendor_reported_sets")
            if session_rpe is not None:
                observed_fields.append("session_rpe")
            if calories is not None:
                observed_fields.append("calories")
            if distance is not None:
                observed_fields.append("distance_km")
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
                heart_rate_avg=int(avg_hr) if avg_hr is not None else None,
                heart_rate_max=int(max_hr) if max_hr is not None else None,
                load=int(load) if load is not None else None,
                calories=int(calories) if calories is not None else None,
                distance_km=(
                    round(distance / 1000, 6)
                    if distance is not None and distance >= 0
                    else None
                ),
                observed_fields=observed_fields,
                vendor_reported_sets=reported_sets,
                session_rpe=session_rpe,
                auto_recognized=auto_recognized,
                title=title,
                training_effect=training_effect,
                vendor_heart_rate_zones=vendor_zones,
                vendor_source=str(it.get("source")) if it.get("source") else None,
                vendor_type_id=numeric_type,
                vendor_type_namespace=mode.namespace,
                heart_rate_zone_setting_type=self._bounded_int(
                    it, ("heartrate_setting_type", "heartRateSettingType"), 0, 20
                ),
                heart_rate_zone_boundaries_bpm=self._heart_rate_zone_boundaries(it),
                **workout_supplements(it),
            ))
        return workouts

    @staticmethod
    def parse_workout_detail(
        raw: dict, summary_end: datetime | None = None, *, training_family: str | None = None
    ) -> WorkoutDetail | None:
        """Normalize the current Zepp workout-detail payload.

        Only fields with verified semantics are emitted. Empty vendor fields and
        malformed series remain absent instead of being replaced with zeros.
        """
        if not isinstance(raw, dict):
            return None
        nested = raw.get("data")
        data = nested if isinstance(nested, dict) else raw
        ZeppParser._check_detail_token_budget(data)
        start = ZeppParser._parse_datetime_value(data.get("trackid"))
        if start is None:
            return None
        start = ZeppParser._utc(start)
        workout_id = str(data.get("trackid") or "")
        end = ZeppParser._workout_end(data, start, summary_end)
        ZeppParser._check_detail_sample_budget(data, start, end)

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
        if len(samples) > MAX_WORKOUT_DETAIL_SAMPLES:
            raise WorkoutDetailLimitError("运动明细样本超过安全上限")
        samples = ZeppParser._deduplicate_workout_samples(samples)
        counts: dict[str, int] = {}
        for sample in samples:
            counts[sample.metric] = counts.get(sample.metric, 0) + 1

        strength_sets = ZeppParser._strength_sets(data.get("strengthSets"))
        if not strength_sets and training_family == "strength":
            strength_sets = ZeppParser._strength_lap_sets(data.get("lap"))
        return WorkoutDetail(
            workout_id=workout_id,
            metrics_present=sorted(counts),
            metric_sample_counts=dict(sorted(counts.items())),
            laps=ZeppParser._workout_laps(data.get("lap")),
            pauses=ZeppParser._workout_pauses(data.get("pause")),
            strength_sets=strength_sets,
            samples=samples,
        )

    @staticmethod
    def _check_detail_token_budget(data: dict) -> None:
        """Reject pathological delimited fields before split() allocates tokens."""
        for key in ("time", "heart_rate", "speed", "equivPace", "currentDistance",
                    "time_delta_altitude", "power_meter", "gait", "runPosture"):
            value = data.get(key)
            if isinstance(value, str) and value and value.count(";") + 1 > MAX_WORKOUT_DETAIL_SAMPLES:
                raise WorkoutDetailLimitError("运动明细压缩字段超过安全上限")
        for key, limit in (("lap", MAX_WORKOUT_DETAIL_LAP_ROWS), ("pause", MAX_WORKOUT_DETAIL_PAUSE_ROWS)):
            value = data.get(key)
            if isinstance(value, str) and value and value.count(";") + 1 > limit:
                raise WorkoutDetailLimitError("运动明细行数超过安全上限")
        strength_sets = data.get("strengthSets")
        if isinstance(strength_sets, str):
            if strength_sets.count("{") > MAX_WORKOUT_DETAIL_STRENGTH_ROWS:
                raise WorkoutDetailLimitError("力量明细行数超过安全上限")
        elif isinstance(strength_sets, list) and len(strength_sets) > MAX_WORKOUT_DETAIL_STRENGTH_ROWS:
            raise WorkoutDetailLimitError("力量明细行数超过安全上限")

    @staticmethod
    def _check_detail_sample_budget(data: dict, start: datetime, end: datetime) -> None:
        """Reject oversized compressed series before expanding them into objects."""
        budget = 0
        heart_rate = data.get("heart_rate")
        if isinstance(heart_rate, str) and heart_rate:
            budget += max(0, int((end - start).total_seconds())) + 1
        for key, multiplier in (("speed", 1), ("equivPace", 1), ("currentDistance", 1),
                                ("time_delta_altitude", 1), ("power_meter", 1),
                                ("gait", 2), ("runPosture", 3)):
            value = data.get(key)
            if isinstance(value, str):
                budget += (value.count(";") + (1 if value else 0)) * multiplier
            elif isinstance(value, (list, tuple)):
                budget += len(value) * multiplier
            if budget > MAX_WORKOUT_DETAIL_SAMPLES:
                raise WorkoutDetailLimitError("运动明细样本超过安全上限")

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
    def _vendor_heart_rate_zones(data: dict) -> list[WorkoutHeartRateZone]:
        """Keep verified ``seconds,ceiling`` pairs and their original ordering.

        https://github.com/J-solan/zepp-dashboard/blob/main/docs/zepp-api.md#workouts--datasummary
        These are vendor zone observations, not personalized model thresholds.
        """
        encoded = data.get("heart_range")
        if not isinstance(encoded, str):
            return []
        zones = []
        for pair in encoded.split(";"):
            fields = pair.split(",")
            if len(fields) != 2:
                continue
            seconds = ZeppParser._first_number({"value": fields[0]}, ("value",))
            upper = ZeppParser._first_number({"value": fields[1]}, ("value",))
            if (
                seconds is None or upper is None
                or not seconds.is_integer() or not upper.is_integer()
                or seconds < 0 or not 0 < upper <= 300
            ):
                continue
            zones.append(WorkoutHeartRateZone(seconds=int(seconds), upper_bpm=int(upper)))
        return zones

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
        if isinstance(encoded, str):
            if not encoded.strip():
                return []
            try:
                items = json.loads(encoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        elif isinstance(encoded, list):
            items = encoded
        else:
            return []
        if not isinstance(items, list):
            return []
        output = []
        for order, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            started = ZeppParser._parse_datetime_value(
                item.get("startTime") or item.get("startedAt") or item.get("timestamp")
            )
            ended = ZeppParser._parse_datetime_value(
                item.get("endTime") or item.get("endedAt")
            )
            weight_kg = ZeppParser._strength_weight_kg(item)
            weight_value = weight_kg
            weight_unit = "kg" if weight_kg is not None else ZeppParser._first_text(
                item, ("weightUnit", "weight_unit", "unit", "weightUnitName")
            )
            if weight_value is None:
                weight_value = ZeppParser._strength_number(item, ("weight",), 0, 2000)
            weight_basis = ZeppParser._strength_weight_basis(item)
            observation = StrengthSetObservation(
                order=order,
                set_kind=item.get('set_kind') if item.get('set_kind') in {'warmup', 'working', 'technique'} else 'unknown',
                side=item.get('side') if item.get('side') in {'left', 'right', 'bilateral'} else 'unknown',
                started_at=ZeppParser._utc(started) if started else None,
                ended_at=ZeppParser._utc(ended) if ended else None,
                exercise_id=ZeppParser._first_text(
                    item, ("exerciseId", "exercise_id", "actionId", "movementId")
                ),
                exercise_name=ZeppParser._first_text(
                    item, ("exerciseName", "exercise_name", "name")
                ),
                repetitions=ZeppParser._strength_number(
                    item, ("repetitions", "reps", "count"), 1, 1000, integer=True,
                ),
                weight_kg=weight_kg,
                weight_value=weight_value,
                weight_unit=weight_unit if weight_value is not None else None,
                weight_basis=weight_basis,
                duration_seconds=ZeppParser._strength_number(
                    item, ("durationSeconds", "duration", "workTime"), 0, MAX_WORKOUT_SECONDS, integer=True,
                ),
                rest_seconds=ZeppParser._strength_number(
                    item, ("restSeconds", "rest", "restTime"), 0, MAX_WORKOUT_SECONDS, integer=True,
                ),
            )
            if any(getattr(observation, key) is not None for key in (
                "started_at", "ended_at", "exercise_id", "exercise_name",
                "repetitions", "weight_value", "duration_seconds", "rest_seconds",
            )):
                output.append(observation)
        return output

    @staticmethod
    def _strength_lap_sets(encoded: object) -> list[StrengthSetObservation]:
        """Read observed fields of the supported strength-only 62-column layout."""
        if not isinstance(encoded, str):
            return []
        return ZeppParser._strength_lap_rows(encoded.split(";"))

    @staticmethod
    def _strength_lap_rows(rows: Iterable[str]) -> list[StrengthSetObservation]:
        """Normalize both buffered and streamed laps through the same catalog."""
        output = []
        for order, row in enumerate(rows, start=1):
            fields = row.split(",")
            if len(fields) != 62:
                continue
            repetitions = ZeppParser._strength_number(
                {"value": fields[22]}, ("value",), 1, 1000, integer=True,
            )
            raw_weight = ZeppParser._strength_number(
                {"value": fields[21]}, ("value",), -float("inf"), float("inf"),
            )
            unit_code = ZeppParser._strength_number(
                {"value": fields[23]}, ("value",), 0, 2**31 - 1, integer=True,
            )
            code = ZeppParser._strength_number(
                {"value": fields[28]}, ("value",), 1, 2**31 - 1, integer=True,
            )
            resolution = (
                resolve_exercise(LAP_62_NAMESPACE, code)
                if code is not None else None
            )
            exercise_name = resolution.label_zh if resolution else None
            # APK tc20: 0/1 are native kg/lb; 11/12 store tenths. 13/14
            # encode RM/RPE, not mass. Unit 15 is the bodyweight display mode.
            weight_unit = {0: "kg", 1: "lb", 11: "kg", 12: "lb"}.get(unit_code)
            weight = raw_weight
            if weight is not None and unit_code in {11, 12}:
                weight /= 10
            if weight is not None and (not 0 <= weight <= 2000 or unit_code in {13, 14, 15}):
                weight = None
            bodyweight = raw_weight is not None and raw_weight <= 0 and (
                unit_code == 15 or resolution is not None and resolution.self_weight_type == 1
            )
            if repetitions is None and weight is None and code is None and not bodyweight:
                continue
            limitations = [
                "exercise_name_reference_mapping"
                if exercise_name is not None
                else "exercise_name_unverified"
            ]
            if bodyweight:
                limitations.append("bodyweight_vendor_reference")
            elif unit_code in {13, 14, 15}:
                limitations.append("non_mass_weight_encoding")
            if weight is None:
                limitations.append("weight_unavailable")
            elif weight_unit is None and not bodyweight:
                limitations.append("weight_unit_unverified")
            weight_kg = None
            if weight is not None and not bodyweight:
                if weight_unit == "kg":
                    weight_kg = weight
                elif weight_unit == "lb":
                    weight_kg = weight * 0.45359237
            output.append(StrengthSetObservation(
                source="lap_62", order=order, vendor_exercise_code=code,
                exercise_name=exercise_name, repetitions=repetitions,
                vendor_weight_value=raw_weight, vendor_weight_unit=unit_code,
                weight_value=weight, weight_unit=weight_unit if weight is not None else None,
                weight_kg=weight_kg, weight_basis="bodyweight" if bodyweight else None,
                limitations=limitations,
            ))
        return output

    @staticmethod
    def _strength_number(
        item: dict, keys: tuple[str, ...], minimum: float, maximum: float, *, integer: bool = False,
    ) -> float | int | None:
        for key in keys:
            value = item.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if not isfinite(numeric) or not minimum <= numeric <= maximum:
                continue
            if integer and not numeric.is_integer():
                continue
            return int(numeric) if integer else numeric
        return None

    @staticmethod
    def _strength_weight_kg(item: dict) -> float | None:
        explicit = ZeppParser._strength_number(
            item, ("weightKg", "weight_kg"), 0, 2000
        )
        if explicit is not None:
            return explicit
        if "weight" not in item:
            return None
        unit = ZeppParser._first_text(
            item, ("weightUnit", "weight_unit", "unit", "weightUnitName")
        )
        normalized = (unit or "").strip().lower().replace(" ", "")
        if normalized not in {"kg", "kgs", "kilogram", "kilograms", "公斤", "千克"}:
            return None
        return ZeppParser._strength_number(item, ("weight",), 0, 2000)

    @staticmethod
    def _strength_weight_basis(item: dict) -> str | None:
        """Keep only an explicitly supplied, supported loading basis."""
        value = ZeppParser._first_text(item, ("weightBasis", "weight_basis"))
        if value is None:
            return None
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        return normalized if normalized in {"per_hand", "total", "machine", "bodyweight", "assistance"} else None

    @staticmethod
    def _deduplicate_workout_samples(
        samples: list[WorkoutMetricSample],
    ) -> list[WorkoutMetricSample]:
        ordered = sorted(samples, key=lambda item: (
            item.timestamp, item.metric, item.source_scope, item.device_id or ""
        ))
        ordinals: dict[tuple[str, datetime, str, str], int] = {}
        for item in ordered:
            key = item.metric, item.timestamp, item.source_scope, item.device_id or ""
            item.sample_ordinal = ordinals.get(key, 0)
            ordinals[key] = item.sample_ordinal + 1
        return ordered

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
    def _heart_rate_source_identity(item: dict, seen: dict[str, int]) -> tuple[str, int | None]:
        for key in ("sample_id", "sampleId", "record_id", "recordId"):
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                return f"zepp.hr:{str(value)[:96]}", None
        canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]
        ordinal = seen.get(digest, 0)
        seen[digest] = ordinal + 1
        return f"zepp.hr:{digest}:{ordinal}", ordinal

    @staticmethod
    def parse_heart_rate_samples(raw: dict) -> list[MetricSample]:
        samples: list[MetricSample] = []
        seen: dict[str, int] = {}
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
                    source_scope, device_id = ZeppParser._provenance(item)
                    source_record_id, sample_ordinal = ZeppParser._heart_rate_source_identity(item, seen)
                    samples.append(MetricSample(
                        metric="heart_rate",
                        timestamp=timestamp,
                        value=float(value),
                        unit="bpm",
                        source_scope=source_scope,
                        device_id=device_id,
                        source_record_id=source_record_id,
                        sample_ordinal=sample_ordinal,
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
            source_scope, device_id = ZeppParser._provenance(obj, item)
            source_record_id, sample_ordinal = ZeppParser._heart_rate_source_identity(item, seen)
            samples.append(MetricSample(
                metric="heart_rate",
                timestamp=timestamp,
                value=value,
                unit="bpm",
                source_scope=source_scope,
                device_id=device_id,
                source_record_id=source_record_id,
                sample_ordinal=sample_ordinal,
            ))
        return samples

    @staticmethod
    def parse_daily_metrics(
        raw: dict, timezone_name: str | None = None, *, source_key: str | None = None,
    ) -> list[DailyMetric]:
        """Parse daily facts using the operation's frozen local timezone."""
        from vitalis.time import local_day
        fields: tuple[tuple[str, tuple[str, ...], str], ...] = (
            ("steps", ("steps", "step", "stepCount", "totalSteps"), "steps"),
            ("calories", ("calories", "calorie", "totalCalories"), "kcal"),
            ("active_minutes", ("activeMinutes",), "min"),
            ("vendor_burning_minutes", ("totalBurningDuration",), "min"),
            ("distance_km", ("distance", "totalDistance"), "km"),
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
            ("training_load", ("currnetDayTrainLoad",), "load"),
            ("vendor_load_7d", ("wtlSum",), "load"),
            ("vendor_load_optimal_min", ("wtlSumOptimalMin",), "load"),
            ("vendor_load_optimal_max", ("wtlSumOptimalMax",), "load"),
            ("vendor_load_overreaching", ("wtlSumOverreaching",), "load"),
            ("vo2max", ("vo2max", "vo2Max", "VO2_MAX", "VO2_max", "vo2_max_run", "vo2_max_walking"), "ml/kg/min"),
            ("stress", ("stress", "stressScore", "avgStress", "averageStress"), "score"),
            ("spo2", ("spo2", "bloodOxygen", "blood_oxygen"), "%"),
            ("running_distance", ("totalRunningDistance",), "m"),
            ("cycling_distance", ("totalCyclingDistance",), "m"),
            ("step_goal", ("stepGoal",), "steps"),
            ("calorie_goal", ("calorieGoal",), "kcal"),
            ("active_minutes_goal", ("burningDurationGoal",), "min"),
        )
        output: dict[tuple[date, str, str, str], DailyMetric] = {}

        def store(item: DailyMetric) -> None:
            output[(
                item.date,
                item.metric,
                item.source_scope or "unknown",
                item.device_id or "",
            )] = item

        # Reuse one revision selector for daily and timestamped readiness facts.
        for sample in ZeppParser.parse_readiness_samples(raw, timezone_name):
            store(DailyMetric(
                date=local_day(sample.timestamp, timezone_name),
                metric=sample.metric, value=sample.value, unit=sample.unit,
                source_scope=sample.source_scope, device_id=sample.device_id,
                source_field=sample.source_field, observed_at=sample.timestamp,
                qualification=sample.qualification, revision_at=sample.revision_at,
            ))
        # Each Charge component has its own valid timeline; a total=255 must
        # neither drop other components nor roll them back to an older sample.
        charge_latest: dict[tuple[date, str, str, str], MetricSample] = {}
        for sample in ZeppParser.parse_charge_samples(raw):
            day = local_day(sample.timestamp, timezone_name)
            key = (day, sample.metric, sample.source_scope, sample.device_id or "")
            previous = charge_latest.get(key)
            if previous is None or sample.timestamp > previous.timestamp:
                charge_latest[key] = sample
        for (day, _, _, _), sample in charge_latest.items():
            store(DailyMetric(
                date=day, metric=sample.metric, value=sample.value, unit=sample.unit,
                source_scope=sample.source_scope, device_id=sample.device_id,
                source_field=sample.source_field, observed_at=sample.timestamp,
                qualification=sample.qualification,
            ))

        for item in ZeppParser._items(raw):
            if not isinstance(item, dict) or item.get("eventType") in {"Charge", "readiness"}:
                continue
            event_value = item.get("value") if isinstance(item.get("value"), dict) else None
            objects: list[tuple[dict, dict | None, str, str | None]] = []
            nested_samples = event_value.get("samples") if event_value else None
            account_summary = item.get("eventType") in _ACCOUNT_SUMMARY_EVENTS
            if isinstance(nested_samples, list):
                provenance = ZeppParser._sample_provenance(
                    nested_samples, event_value, item,
                    account_summary=account_summary, source_key=source_key,
                )
                objects.extend((sample, item, *provenance[index])
                               for index, sample in enumerate(nested_samples) if isinstance(sample, dict))
            else:
                objects.append((item, event_value, *ZeppParser._provenance(
                    event_value or {}, item, account_summary=account_summary, source_key=source_key,
                )))
            for obj, nested, source_scope, device in objects:
                day = ZeppParser._metric_date(obj, nested)
                if day is None:
                    continue
                for metric, keys, unit in fields:
                    source_obj = obj
                    value = ZeppParser._first_number(obj, keys)
                    if value is None and nested:
                        source_obj = nested
                        value = ZeppParser._first_number(nested, keys)
                    if value is None or (unit == "load" and value < 0):
                        continue
                    if metric == "vendor_burning_minutes" and not 0 <= value <= 1440:
                        continue
                    is_goal = metric in {"step_goal", "calorie_goal", "active_minutes_goal"}
                    if is_goal and value <= 0:
                        continue
                    if metric == "distance_km":
                        value = value / 1000
                    if (
                        (metric == "readiness" or metric.endswith("_readiness"))
                        and not 0 <= value <= 100
                    ):
                        continue
                    store(DailyMetric(
                        date=day, metric=metric, value=value, unit=unit,
                        source_scope=source_scope, device_id=device,
                        source_field=next(key for key in keys if ZeppParser._first_number(source_obj, (key,)) is not None),
                        qualification=("vendor_user_target" if is_goal else "vendor_reference" if unit == "load"
                                       else "vendor_heart_rate_zone_minutes" if metric == "vendor_burning_minutes" else None),
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
            provenance = ZeppParser._sample_provenance(
                raw_samples, value, item, account_summary=item.get("eventType") == "Charge",
            )
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
                    source_scope, device_id = provenance[index]
                    output.append(MetricSample(
                        metric=metric, timestamp=timestamp, value=reading, unit="score",
                        source_scope=source_scope,
                        device_id=device_id, source_field=key,
                        qualification="vendor_reference",
                    ))
        return output

    @staticmethod
    def _latest_readiness_events(raw: dict, timezone_name: str | None = None) -> list[dict]:
        """Select one whole revision per scored local day and device stream.

        value.timestamp is the day scored; item.timestamp is its revision time.
        Evidence: https://github.com/J-solan/zepp-dashboard/blob/main/docs/zepp-api.md#readiness
        """
        from vitalis.time import local_day

        selected: dict[tuple[date, str, str], tuple[datetime, str, dict]] = {}
        for item in ZeppParser._items(raw):
            if not isinstance(item, dict) or item.get("eventType") != "readiness":
                continue
            value = item.get("value")
            if not isinstance(value, dict):
                continue
            anchor = ZeppParser._parse_datetime_value(value.get("timestamp"))
            if anchor is None:
                continue
            source_scope, device = ZeppParser._provenance(value, item, account_summary=True)
            key = (local_day(anchor, timezone_name), device or "", source_scope)
            revision = ZeppParser._parse_datetime_value(item.get("timestamp"))
            rank = ZeppParser._utc(revision) if revision is not None else datetime.min.replace(tzinfo=timezone.utc)
            # Stable tie handling makes permutations of the same response equal;
            # it does not claim that an absent revision is newer than a known one.
            tie = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            previous = selected.get(key)
            if previous is None or (rank, tie) > previous[:2]:
                selected[key] = (rank, tie, item)
        return [selected[key][2] for key in sorted(selected)]

    @staticmethod
    def parse_readiness_revisions(
        raw: dict, timezone_name: str | None = None,
    ) -> list[dict]:
        """Keep latest revision metadata even when every component was withdrawn."""
        from vitalis.time import local_day

        output = []
        for item in ZeppParser._latest_readiness_events(raw, timezone_name):
            value = item["value"]
            observed_at = ZeppParser._utc(ZeppParser._parse_datetime_value(value["timestamp"]))
            revision = ZeppParser._parse_datetime_value(item.get("timestamp"))
            source_scope, device_id = ZeppParser._provenance(value, item, account_summary=True)
            output.append({
                "day": local_day(observed_at, timezone_name),
                "device_id": device_id,
                "source_scope": source_scope,
                "observed_at": observed_at,
                "revision_at": ZeppParser._utc(revision) if revision else None,
            })
        return output

    @staticmethod
    def parse_readiness_samples(
        raw: dict, timezone_name: str | None = None,
    ) -> list[MetricSample]:
        """Emit only the latest whole revision of each day's device-scoped facts."""
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
        for item in ZeppParser._latest_readiness_events(raw, timezone_name):
            value = item["value"]
            timestamp = ZeppParser._utc(ZeppParser._parse_datetime_value(value["timestamp"]))
            revision = ZeppParser._parse_datetime_value(item.get("timestamp"))
            source_scope, device_id = ZeppParser._provenance(value, item, account_summary=True)
            for metric, key, unit, scale, minimum, maximum in fields:
                keys = (key,) if metric != "readiness" else ("rdnsScore", "watchScore", "readinessScore", "readiness")
                source_field = next((field for field in keys if field in value), key)
                reading = ZeppParser._first_number(value, (source_field,))
                if reading is None or reading == 255:
                    continue
                reading *= scale
                if minimum <= reading <= maximum:
                    output.append(MetricSample(
                        metric=metric, timestamp=timestamp, value=reading, unit=unit,
                        source_scope=source_scope,
                        device_id=device_id, source_field=source_field,
                        qualification="vendor_reference",
                        revision_at=ZeppParser._utc(revision) if revision else None,
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
            provenance = ZeppParser._sample_provenance(raw_samples, value, item)
            for index, sample in enumerate(raw_samples):
                if not isinstance(sample, dict):
                    continue
                reading = ZeppParser._first_number(sample, (key, "rmssd" if key == "hrv" else key))
                if reading is None or not 1 <= reading <= 400:
                    continue
                offset = ZeppParser._first_number(sample, ("s", "offset")) or 0
                source_scope, device_id = provenance[index]
                output.append(MetricSample(
                    metric=metric,
                    timestamp=start + timedelta(milliseconds=offset),
                    value=reading,
                    unit="ms",
                    source_scope=source_scope, device_id=device_id,
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
            provenance = ZeppParser._sample_provenance(samples, value, event)
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
                source_scope, device_id = provenance[index]
                output.append(DenseDataFile(
                    stream=stream,
                    file_id=file_id,
                    file_type=str(sample.get("fileType") or ""),
                    date=day or (start_utc.date() if start_utc else None),
                    start_utc=start_utc,
                    end_utc=end_utc,
                    source_scope=source_scope, device_id=device_id,
                    parse_status="indexed",
                    sample_count=0,
                ))
        return output

    @staticmethod
    def is_recognized_all_day_stress_payload(raw: dict) -> bool:
        """Return whether every non-empty stress item has a verified shape.

        Padding can add neighboring days, but one valid item must not hide a
        malformed item from the same response.  A null summary is only enough for
        an item with no timeline or an explicitly empty timeline; it cannot mask a
        malformed timeline.
        """
        items = ZeppParser._items(raw)
        if not items:
            return False

        def recognized_item(item: object) -> bool:
            if not isinstance(item, dict):
                return False
            value = item.get("value") if isinstance(item.get("value"), dict) else None
            if ZeppParser._metric_date(item, value) is None:
                return False

            valid_summary = False
            empty_summary = False
            for source in (item, value or {}):
                for _metric, keys, _unit in _ALL_DAY_STRESS_FIELDS:
                    for key in keys:
                        if key not in source:
                            continue
                        candidate = source.get(key)
                        if candidate is None or (
                            isinstance(candidate, str) and not candidate.strip()
                        ):
                            empty_summary = True
                            continue
                        reading = ZeppParser._first_number(source, (key,))
                        if reading is not None and 0 <= reading <= 100:
                            valid_summary = True

            timeline_present = "data" in item or (
                value is not None and "data" in value
            )
            timeline = item.get("data")
            if timeline is None and value:
                timeline = value.get("data")
            timeline_valid = False
            timeline_empty = False
            timeline_malformed = False
            if timeline_present:
                if isinstance(timeline, str):
                    try:
                        timeline = json.loads(timeline)
                    except json.JSONDecodeError:
                        timeline_malformed = True
                if not isinstance(timeline, list):
                    timeline_malformed = True
                elif not timeline:
                    timeline_empty = True
                else:
                    for point in timeline:
                        if not isinstance(point, dict):
                            continue
                        raw_timestamp = point.get("time")
                        if raw_timestamp is None:
                            raw_timestamp = point.get("timestamp")
                        timestamp = ZeppParser._parse_datetime_value(raw_timestamp)
                        reading = ZeppParser._first_number(
                            point, ("value", "stress", "stressScore")
                        )
                        if (
                            timestamp is not None
                            and reading is not None
                            and 0 <= reading <= 100
                        ):
                            timeline_valid = True
                            break
                    if not timeline_valid:
                        timeline_malformed = True

            if timeline_malformed and not valid_summary:
                return False
            return valid_summary or timeline_valid or timeline_empty or (
                empty_summary and not timeline_present
            )

        return all(recognized_item(item) for item in items)

    @staticmethod
    def parse_wellness(raw: dict, source_key: str, *, timezone_name: str | None = None) -> tuple[list[DailyMetric], list[MetricSample]]:
        """Parse the optional wellness shapes verified by ZeppBridge."""
        label = source_key.split(":")[1] if source_key.startswith("wellness:") else ""
        items = ZeppParser._items(raw)
        daily: list[DailyMetric] = []
        samples: list[MetricSample] = []

        if label == "weight":
            samples.extend(weight_samples(items))
        elif label == "exertion":
            daily.extend(exertion_metrics(items, timezone_name))
        elif label in {"charge_stress", "charge_insights", "readiness_raw"}:
            # Opaque payloads stay in the encrypted journal. No score is inferred.
            return [], []
        elif label == "respiratory_rate":
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
                    source_scope, device_id = ZeppParser._provenance(value, item)
                    for metric, reading in (
                        ("respiratory_rate", round(sum(readings) / len(readings), 1)),
                        ("respiratory_rate_min", min(readings)),
                        ("respiratory_rate_max", max(readings)),
                    ):
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit="brpm",
                            source_scope=source_scope, device_id=device_id,
                        ))
        elif label == "hrv_rmssd":
            samples.extend(ZeppParser.parse_hrv_samples(raw, "hrv_rmssd"))
        elif label == "lactate_threshold":
            for item in items:
                value = item.get("value") if isinstance(item, dict) and isinstance(item.get("value"), dict) else None
                raw_samples = (value or {}).get("samples") or []
                if not isinstance(raw_samples, list):
                    continue
                provenance = ZeppParser._sample_provenance(raw_samples, value or {}, item or {})
                for index, sample in enumerate(raw_samples):
                    if not isinstance(sample, dict):
                        continue
                    source_scope, device_id = provenance[index]
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
                                source_scope=source_scope, device_id=device_id,
                            ))
        elif label == "spo2":
            for item in items:
                if not isinstance(item, dict):
                    continue
                subtype = item.get("subType")
                day = ZeppParser._metric_date(item, None)
                if subtype == "odi" and day:
                    source_scope, device_id = ZeppParser._provenance(item)
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
                    source_scope, device_id = ZeppParser._provenance(extra, item)
                    samples.append(MetricSample(
                        metric="spo2", timestamp=timestamp, value=reading, unit="%",
                        source_scope=source_scope, device_id=device_id,
                    ))
                apnea = ZeppParser._first_number(extra, ("spo2_decrease", "spo2Decrease"))
                if subtype == "osa_event" and apnea is not None and 50 <= apnea <= 100 and timestamp:
                    source_scope, device_id = ZeppParser._provenance(extra, item)
                    samples.append(MetricSample(
                        metric="spo2_apnea_low", timestamp=timestamp, value=apnea, unit="%",
                        source_scope=source_scope, device_id=device_id,
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
                        source_scope, device_id = ZeppParser._provenance(item)
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit=unit,
                            source_scope=source_scope, device_id=device_id,
                        ))
        elif label == "all_day_stress":
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("value") if isinstance(item.get("value"), dict) else None
                day = ZeppParser._metric_date(item, value)
                if day is None:
                    continue
                original_scope, contributing_device_id = ZeppParser._provenance(value or {}, item)
                # Zepp 10.8.7 z42/HealthMatrix selects slots, then the daily
                # uploader stores only value/time and the first record's source.
                # That header is not each sample's independent device identity.
                source_scope = "user_fused" if original_scope == "user_fused" else "user_summary"
                device_id = None
                for metric, keys, unit in _ALL_DAY_STRESS_FIELDS:
                    reading = ZeppParser._first_number(item, keys)
                    if reading is None and value:
                        reading = ZeppParser._first_number(value, keys)
                    if reading is not None and 0 <= reading <= 100:
                        daily.append(DailyMetric(
                            date=day, metric=metric, value=reading, unit=unit,
                            source_scope=source_scope, device_id=device_id,
                            contributing_device_id=contributing_device_id,
                            qualification="vendor_selected", source_field=keys[0],
                        ))

                timeline = item.get("data")
                if timeline is None and value:
                    timeline = value.get("data")
                if isinstance(timeline, str):
                    try:
                        timeline = json.loads(timeline)
                    except json.JSONDecodeError:
                        timeline = []
                if not isinstance(timeline, list):
                    continue
                for point in timeline[:MAX_WELLNESS_SAMPLES_PER_EVENT]:
                    if not isinstance(point, dict):
                        continue
                    raw_timestamp = point.get("time")
                    if raw_timestamp is None:
                        raw_timestamp = point.get("timestamp")
                    timestamp = ZeppParser._parse_datetime_value(raw_timestamp)
                    reading = ZeppParser._first_number(
                        point, ("value", "stress", "stressScore")
                    )
                    if timestamp is None or reading is None or not 0 <= reading <= 100:
                        continue
                    samples.append(MetricSample(
                        metric="stress",
                        timestamp=timestamp,
                        value=reading,
                        unit="score",
                        source_scope=source_scope,
                        device_id=device_id,
                        contributing_device_id=contributing_device_id,
                        qualification="vendor_selected", source_field="all_day_stress.data",
                    ))
        else:
            daily.extend(ZeppParser.parse_daily_metrics(raw))
        return daily, samples

    @staticmethod
    def _expand_sample_device_ids(encoded: object, sample_count: int) -> list[str | None]:
        """Expand exact run coverage before filtering samples; never fill gaps.

        A structurally valid run with an unknown ID still consumes its slots.
        Invalid counts, delimiters or total coverage invalidate the entire map.
        """
        if sample_count <= 0:
            return []
        if isinstance(encoded, str) and ("," in encoded or ";" in encoded):
            runs = ZeppParser._device_runs(encoded)
            if runs is None or sum(count for count, _ in runs) != sample_count:
                return [None] * sample_count
            return [device_id for count, device_id in runs for _ in range(count)]
        return [ZeppParser._normalize_device_identifier(encoded)] * sample_count

    # ================= 真实 events（HRV / 摘要） =================

    @staticmethod
    def parse_hrv_events(
        raw: dict, timezone_name: str | None = None
    ) -> dict[date, int]:
        """HRV 事件流 -> {日期: 当天均值 ms}。

        对齐 ZeppBridge normalizer：
          - 新版：item.value = {samples: [{sdnn, rmssd, hrv, value}, ...]}
          - 旧版：item.value = 数值（直接取）
          - 时间戳优先 item.timestamp，兜底 value.startTime

        Numeric timestamps are converted using the report timezone, rather
        than the host process timezone.  A resumed or mock operation may pass
        its persisted timezone explicitly; otherwise the configured app zone
        is used by the shared time helper.
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
                        it.get("timestamp") or it.get("time") or val.get("startTime") or val.get("start_time"),
                        timezone_name,
                    )
            elif isinstance(val, (int, float)):
                # 旧版结构：直接数值
                samples.append(float(val))
                day = ZeppParser._ts_date(
                    it.get("ts") or it.get("time") or it.get("timestamp"),
                    timezone_name,
                )

            if day and samples:
                by_day.setdefault(day, []).extend(samples)

        return {d: round(fmean(v)) for d, v in by_day.items() if v}

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
            deep_sleep=self._nonnegative_number(stages, ("deep",)),
            light_sleep=self._nonnegative_number(stages, ("light",)),
            rem_sleep=(
                int(stages["rem"]) if stages.get("rem") is not None else None
            ),
            awake=self._nonnegative_number(data, ("awake",)),
            observed_fields=[
                name for name, value in (
                    ("deep_sleep", self._nonnegative_number(stages, ("deep",))),
                    ("light_sleep", self._nonnegative_number(stages, ("light",))),
                    ("awake", self._nonnegative_number(data, ("awake",))),
                ) if value is not None
            ],
            sleep_score=int(data["sleepScore"]) if data.get("sleepScore") is not None else None,
            bedtime=bedtime,
            wake_time=wake,
        )

    # ---- Activity ----
    def parse_activity(self, raw: dict) -> ActivityRecord | None:
        data = (raw or {}).get("data")
        if not isinstance(data, dict) or not data:
            return None
        fields = {
            "steps": self._nonnegative_number(data, ("steps",)),
            "active_minutes": self._nonnegative_number(data, ("activeMinutes",)),
            "calories": self._nonnegative_number(data, ("calories",)),
            "distance_km": self._nonnegative_number(data, ("distanceKm",)),
            "resting_hr": self._nonnegative_number(data, ("restingHr",)),
        }
        observed_fields = [name for name, value in fields.items() if value is not None]
        if not observed_fields:
            return None
        return ActivityRecord(
            user_id="",
            source=self.source,
            date=date.fromisoformat(data["date"]) if data.get("date") else None,
            steps=int(fields["steps"]) if fields["steps"] is not None else None,
            active_minutes=(
                int(fields["active_minutes"])
                if fields["active_minutes"] is not None else None
            ),
            calories=int(fields["calories"]) if fields["calories"] is not None else None,
            distance_km=fields["distance_km"],
            resting_hr=(
                int(fields["resting_hr"])
                if fields["resting_hr"] is not None else None
            ),
            observed_fields=observed_fields,
        )

    # ---- Training ----
    def parse_training(self, raw: dict) -> TrainingRecord | None:
        data = (raw or {}).get("data", {})
        items = data.get("items", []) or []
        if not items:
            return None
        total_duration = 0
        load_values = []
        for it in items:
            total_duration += int(it.get("duration", 0))
            load_values.append(it.get("load"))
        total_load = (
            sum(int(value) for value in load_values)
            if all(value is not None for value in load_values)
            else None
        )
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
        calories = self._nonnegative_number(raw, ("calories",))
        numeric_fields = {
            "heart_rate_avg": self._nonnegative_number(raw, ("avgHr",)),
            "heart_rate_max": self._nonnegative_number(raw, ("maxHr",)),
            "load": self._nonnegative_number(raw, ("load",)),
            "calories": calories,
        }
        observed_fields = [name for name, value in numeric_fields.items() if value is not None]
        return Workout(
            user_id="",
            source=self.source,
            workout_id=raw.get("workoutId", ""),
            started_at=datetime.fromisoformat(started) if started else None,
            type=_TYPE_MAP.get(t, WorkoutType.OTHER),
            duration=int(raw.get("duration", 0)),
            heart_rate_avg=int(numeric_fields["heart_rate_avg"]) if numeric_fields["heart_rate_avg"] is not None else None,
            heart_rate_max=int(numeric_fields["heart_rate_max"]) if numeric_fields["heart_rate_max"] is not None else None,
            load=int(numeric_fields["load"]) if numeric_fields["load"] is not None else None,
            calories=int(calories) if calories is not None else None,
            observed_fields=observed_fields,
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
        aliases = sorted({
            identity
            for obj in (raw, additional)
            for key in (*_DEVICE_ID_FIELDS, "btmac", "sn", "serial", "serialNumber")
            if (identity := self._normalize_device_identifier(obj.get(key))) is not None
        })
        device_id = (
            self._normalize_device_identifier(additional.get("btmac"))
            or self._normalize_device_identifier(raw.get("macAddress"))
            or self._device_id(raw) or self._device_id(additional)
            or (aliases[0] if aliases else None)
        )
        return Device(
            user_id="",
            source=self.source,
            model=str(model),
            device_id=device_id or "",
            aliases=aliases,
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
        text = str(v)
        try:
            if len(text) == 8 and text.isdigit():
                return datetime.strptime(text, "%Y%m%d").date()
            return date.fromisoformat(text[:10])
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
        if value is None or isinstance(value, bool):
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
        except (TypeError, ValueError, OverflowError, OSError):
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
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            try:
                number = float(value)
            except (ValueError, OverflowError):
                continue
            if isfinite(number):
                return number
        return None

    @staticmethod
    def _nonnegative_number(obj: dict, keys: tuple[str, ...]) -> float | None:
        for key in keys:
            if isinstance(obj.get(key), bool):
                continue
            value = ZeppParser._first_number(obj, (key,))
            if value is not None and value >= 0:
                return value
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
    def _band_timezone(summary: dict, timezone_name: str | None = None):
        from vitalis.time import local_timezone

        if timezone_name:
            return local_timezone(timezone_name)
        offset = ZeppParser._first_number(summary, ("tz",))
        if offset is not None and -18 * 3600 <= offset <= 18 * 3600:
            return timezone(timedelta(seconds=offset))
        return local_timezone()

    @staticmethod
    def _stage_slices(stages: object, anchor: datetime) -> list[SleepStageSlice]:
        """Vendor stop is inclusive; preserve unknown modes, never label awake."""
        if not isinstance(stages, list):
            return []
        names = {5: "deep", 4: "light", 8: "rem", 11: "rem", 7: "awake"}
        output = []
        for item in stages:
            if not isinstance(item, dict):
                continue
            mode = ZeppParser._first_number(item, ("mode",))
            start = ZeppParser._first_number(item, ("start",))
            stop = ZeppParser._first_number(item, ("stop",))
            if (
                any(value is None or not value.is_integer() for value in (mode, start, stop))
                or start < 0 or stop < start
            ):
                continue
            try:
                start_time = (anchor + timedelta(minutes=int(start))).astimezone(timezone.utc)
                end_time = (anchor + timedelta(minutes=int(stop) + 1)).astimezone(timezone.utc)
            except (OverflowError, ValueError):
                continue
            if end_time <= start_time:
                continue
            output.append(SleepStageSlice(
                stage=names.get(int(mode), "unknown"), vendor_mode=int(mode),
                start_time=start_time, end_time=end_time,
            ))
        return output

    @staticmethod
    def _sleep_stage_anchor(
        day: date, summary: dict, sleep: dict,
        session_start: object, session_end: object,
        timezone_name: str | None = None,
    ) -> datetime:
        zone = ZeppParser._band_timezone(summary, timezone_name)
        midnight = datetime.combine(day, time.min, tzinfo=zone)
        previous = midnight - timedelta(days=1)
        start_at = ZeppParser._parse_datetime_value(session_start)
        end_at = ZeppParser._parse_datetime_value(session_end)
        if start_at is None or end_at is None:
            return previous
        start_at, end_at = ZeppParser._utc(start_at), ZeppParser._utc(end_at)
        if end_at <= start_at:
            return previous

        def overlap(anchor: datetime) -> float:
            return sum(
                max(0.0, (min(stage.end_time, end_at) - max(stage.start_time, start_at)).total_seconds())
                for stage in ZeppParser._stage_slices(sleep.get("stage"), anchor)
            )

        # Retain the existing st/ed-verified same-day variant; no main sleep
        # evidence means the documented previous-day anchor, not a guessed nap.
        return midnight if overlap(midnight) > overlap(previous) else previous

    @staticmethod
    def _sleep_stage_slices(
        day: date, summary: dict, sleep: dict,
        session_start: object, session_end: object,
        timezone_name: str | None = None,
    ) -> list[SleepStageSlice]:
        anchor = ZeppParser._sleep_stage_anchor(
            day, summary, sleep, session_start, session_end, timezone_name
        )
        return ZeppParser._stage_slices(sleep.get("stage"), anchor)

    @staticmethod
    def _normalize_device_identifier(value: object) -> str | None:
        """Validate one identity; only MAC/EUI punctuation has known semantics."""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        text = str(value).strip()
        if re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?::[0-9A-Fa-f]{2}){0,2}", text) or re.fullmatch(
            r"[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5}(?:-[0-9A-Fa-f]{2}){0,2}", text
        ):
            compact = text.replace(":", "").replace("-", "")
            return compact.upper() if len(compact) in {12, 16} else None
        if re.fullmatch(r"[0-9A-Fa-f]{12}|[0-9A-Fa-f]{16}", text):
            return text.upper()
        return text if re.fullmatch(r"[A-Za-z0-9]{8,128}", text) else None

    @staticmethod
    def _raw_device_identifier(*objects: dict) -> tuple[bool, object]:
        for obj in objects:
            for key in _DEVICE_ID_FIELDS:
                if key in obj and obj[key] is not None:
                    return True, obj[key]
        return False, None

    @staticmethod
    def _device_runs(encoded: object) -> list[tuple[int, str | None]] | None:
        """Validate the entire run map without allocating its expanded length.

        Behavior reviewed against ZeppBridge 1012a00abbb41865e8abcfb8f59b3d264fbafe1b,
        normalizer/device.rs:17-33. Unlike its longest-comma-segment heuristic,
        this independent implementation never attributes a mixed map to one ID.
        """
        if not isinstance(encoded, str):
            return None
        text = encoded.strip()
        if text.endswith(";"):
            text = text[:-1]
        runs = []
        for segment in text.split(";"):
            fields = segment.split(",")
            if len(fields) != 2 or not re.fullmatch(r"[0-9]{1,10}", fields[0].strip()):
                return None
            count = int(fields[0].strip())
            if count <= 0:
                return None
            runs.append((count, ZeppParser._normalize_device_identifier(fields[1])))
        return runs

    @staticmethod
    def _device_id(obj: dict) -> str | None:
        present, encoded = ZeppParser._raw_device_identifier(obj)
        if not present:
            return None
        if isinstance(encoded, str) and ("," in encoded or ";" in encoded):
            runs = ZeppParser._device_runs(encoded)
            if not runs or any(device is None for _, device in runs):
                return None
            identities = {device for _, device in runs}
            return next(iter(identities)) if len(identities) == 1 else None
        return ZeppParser._normalize_device_identifier(encoded)

    @staticmethod
    def _source_scope(
        device_id: str | None, identity_present: bool, *objects: dict,
        account_summary: bool = False, source_key: str | None = None,
    ) -> str:
        # ZeppBridge device.rs:42-65 preserves explicit fusion metadata. Its
        # event-name inference is NOT evidence of Zepp's proprietary algorithm.
        if any(
            obj.get("is_fused") is True or obj.get("isFused") is True
            or any(isinstance(obj.get(key), str) and obj[key].lower() == "user_fused"
                   for key in ("source_scope", "sourceScope"))
            for obj in objects
        ):
            return "user_fused"
        # Zepp 10.8.7 qnx0/bey0 creates App readiness with (type=2,
        # source=2,id='app'); wqx0 serializes id/source to watch_score.
        # This explicit App algorithm result is not a missing-device inference.
        readiness_event = any(obj.get("eventType") == "readiness" and obj.get("subType") == "watch_score" for obj in objects)
        if readiness_event and any(obj.get("deviceId") == "app" and type(obj.get("deviceSource")) is int
                                   and obj["deviceSource"] == 2 and type(obj.get("deviceType", 2)) is int
                                   and obj.get("deviceType", 2) == 2 for obj in objects):
            return "user_summary"
        if device_id is not None:
            return "device"
        if identity_present:
            _, encoded = ZeppParser._raw_device_identifier(*objects)
            # Exact observed bookkeeping, NOT arbitrary malformed run maps.
            # ZeppBridge normalizer/tests/band.rs:123-165 at the reviewed SHA.
            known_account = any(obj.get("eventType") in {"DailyHealth", "Charge"} for obj in objects)
            known_account = known_account or (source_key or "").startswith((
                "events:DailyHealth:summary:", "events:Charge:real_data:",
            ))
            if known_account and isinstance(encoded, str) and encoded.strip() in {"1,", "1,-1", "1440,app"}:
                return "user_summary"
            return "unknown"  # Malformed/ambiguous identity is not a missing ID.
        if account_summary or (source_key or "").startswith(_ACCOUNT_SUMMARY_KEYS):
            return "user_summary"
        return "unknown"

    @staticmethod
    def _provenance(
        *objects: dict, account_summary: bool = False, source_key: str | None = None,
    ) -> tuple[str, str | None]:
        present, encoded = ZeppParser._raw_device_identifier(*objects)
        device_id = ZeppParser._device_id({"deviceId": encoded}) if present else None
        return ZeppParser._source_scope(
            device_id, present, *objects, account_summary=account_summary, source_key=source_key,
        ), device_id

    @staticmethod
    def _sample_provenance(
        samples: list, *objects: dict, account_summary: bool = False,
        source_key: str | None = None,
    ) -> list[tuple[str, str | None]]:
        present, encoded = ZeppParser._raw_device_identifier(*objects)
        devices = ZeppParser._expand_sample_device_ids(encoded, len(samples))
        # Account-side RLE count varies with the uploaded day length. A verified
        # app run must cover exactly these samples; malformed counts stay unknown.
        app_run = re.fullmatch(r"([1-9][0-9]*),app;?", encoded.strip()) if isinstance(encoded, str) else None
        account_event = any(obj.get("eventType") in {"DailyHealth", "Charge"} for obj in objects)
        verified_app_run = bool(account_event and app_run and int(app_run[1]) == len(samples))
        output = []
        for index, sample in enumerate(samples):
            obj = sample if isinstance(sample, dict) else {}
            own_identity, _ = ZeppParser._raw_device_identifier(obj)
            device_id = ZeppParser._device_id(obj) if own_identity else devices[index]
            scope = ZeppParser._source_scope(
                device_id, own_identity or present, obj, *objects,
                account_summary=account_summary, source_key=source_key,
            )
            if verified_app_run and not own_identity and scope == "unknown":
                scope = "user_summary"
            output.append((scope, device_id))
        return output

    @staticmethod
    def _merge_flag(value: object) -> bool | None:
        if value is True or (type(value) is int and value == 1):
            return True
        if value is False or (type(value) is int and value == 0):
            return False
        return None

    @staticmethod
    def _band_provenance(item: dict, summary: dict, *, sleep: bool = False) -> tuple[str, str | None]:
        # Zepp 10.8.7: MergeStrategy merges data_hr + steps and writes summary
        # isMerged; ut61 writes/reads a separate slp.isMerged for sleep.
        selected = summary.get("slp", {}) if sleep else summary
        if isinstance(selected, dict) and ZeppParser._merge_flag(selected.get("isMerged")):
            return "user_fused", None
        return ZeppParser._provenance(item, summary, {"deviceId": summary.get("sn")})

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
    def _ts_date(v, timezone_name: str | None = None) -> date | None:
        if not v:
            return None
        s = str(v)
        # 纯数字（毫秒/秒时间戳）优先走时间戳分支，避免 fromisoformat 误解析
        from vitalis.time import local_day

        def timestamp_date(ts: float) -> date:
            if ts > 1e12:
                ts /= 1000
            return local_day(
                datetime.fromtimestamp(ts, tz=timezone.utc), timezone_name
            )

        if s.isdigit():
            try:
                return timestamp_date(float(s))
            except (TypeError, ValueError, OSError):
                return None
        if "T" in s or len(s) >= 10:
            try:
                return date.fromisoformat(s[:10])
            except ValueError:
                pass
        try:
            return timestamp_date(float(s))
        except (TypeError, ValueError, OSError):
            return None
