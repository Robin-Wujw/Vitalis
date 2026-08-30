"""Load normalized records into an analysis-ready, identity-isolated profile."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from statistics import median

from vitalis.intelligence.contracts import (
    Coverage,
    DataQuality,
    DeviceValidity,
    MeasurementFact,
    Provenance,
    QualityFlag,
    QualityStatus,
    TrainingPreferences,
)
from vitalis.storage import HealthRepository
from vitalis.time import local_day, local_day_utc_bounds, local_sleep_window
from .localization import QUALITY_LABELS, SIGNAL_LABELS, labels


SAMPLE_METRICS = (
    "hrv_rmssd",
    "hrv_sdnn",
    "sleep_hrv",
    "sleep_rhr",
    "readiness",
    "physical_readiness",
    "mental_readiness",
    "skin_temp_delta",
    "hybrid_charge",
    "bio_charge",
    "spo2",
    "spo2_apnea_low",
)
MAX_SAMPLES_PER_METRIC = 1_000_000
MAX_HEART_RATE_SAMPLES_PER_NIGHT = 200_000
PROFILE_HISTORY_DAYS = 180


@dataclass(frozen=True)
class SeriesPoint:
    metric: str
    value: float
    unit: str
    day: date
    observed_at: datetime | date
    source: str
    source_scope: str
    device_id: str | None = None


@dataclass
class RawDailyProfile:
    user_id: str
    day: date
    series: dict[str, list[SeriesPoint]] = field(default_factory=dict)
    heart_rate_samples: list[SeriesPoint] = field(default_factory=list)
    sleep_by_day: dict[date, dict] = field(default_factory=dict)
    activity_by_day: dict[date, dict] = field(default_factory=dict)
    training_by_day: dict[date, dict] = field(default_factory=dict)
    workouts: list[dict] = field(default_factory=list)
    feedback_by_workout: dict[str, list] = field(default_factory=dict)
    training_preferences: TrainingPreferences | None = None
    device_models: dict[str, str] = field(default_factory=dict)
    dense_heart_rate_coverage: dict[str, dict] = field(default_factory=dict)
    facts: dict[str, list[MeasurementFact]] = field(default_factory=dict)
    data_quality: DataQuality | None = None


class ProfileLoader:
    """Build one local identity's profile without cross-user reconciliation."""

    def __init__(self, repo: HealthRepository):
        self.repo = repo

    def load(
        self,
        user_id: str,
        day: date,
        history_days: int = PROFILE_HISTORY_DAYS,
    ) -> RawDailyProfile:
        start = day - timedelta(days=history_days - 1)
        raw = RawDailyProfile(user_id=user_id, day=day)
        raw.training_preferences = self.repo.training_preferences(user_id)
        raw.sleep_by_day = _records_by_day(self.repo.sleep_range(user_id, start, day))
        raw.activity_by_day = _records_by_day(self.repo.activity_range(user_id, start, day))
        raw.training_by_day = _records_by_day(self.repo.training_range(user_id, start, day))

        self._add_record_series(raw)
        self._add_daily_metrics(raw, start)
        self._add_sample_metrics(raw, start)
        self._add_heart_rate_samples(raw, start)
        self._add_device_context(raw)
        workout_rows = [
            row
            for row in self.repo.workouts(user_id, start - timedelta(days=1), day)
            if row.started_at and start <= local_day(row.started_at) <= day
        ]
        detail_ids = [
            row.workout_id
            for row in workout_rows
            if row.detail_synced
            and row.started_at
            and local_day(row.started_at) >= day - timedelta(days=27)
        ]
        samples_by_workout: dict[str, list] = defaultdict(list)
        for sample in self.repo.workout_metric_samples_for_workouts(user_id, detail_ids):
            samples_by_workout[sample.workout_id].append(sample)
        exercises_by_workout = self.repo.strength_exercises_for_workouts(
            user_id, [row.workout_id for row in workout_rows]
        )
        raw.workouts = [
            {
                "workout_id": row.workout_id,
                "started_at": row.started_at,
                "source": row.source,
                "vendor_source": row.vendor_source,
                "local_day": local_day(row.started_at) if row.started_at else None,
                "detail_available": row.detail_synced,
                "detail": row.detail or None,
                "samples": samples_by_workout.get(row.workout_id, []),
                "confirmed_exercises": exercises_by_workout.get(row.workout_id, []),
                "data": row.data or {},
            }
            for row in workout_rows
        ]
        raw.facts = self._facts_for_day(raw)
        raw.data_quality = self._quality(raw)
        return raw

    def _add_device_context(self, raw: RawDailyProfile) -> None:
        raw.device_models = {
            row.device_id.upper(): row.model
            for row in self.repo.devices(raw.user_id)
            if row.device_id
        }
        start = raw.day - timedelta(days=27)
        files = self.repo.dense_data_files(
            raw.user_id, "second_heart_rate", start, raw.day, limit=20_000
        )
        range_start, _ = local_day_utc_bounds(start)
        _, range_end = local_day_utc_bounds(raw.day)
        day_start, day_end = local_day_utc_bounds(raw.day)
        range_start = range_start.replace(tzinfo=None)
        range_end = range_end.replace(tzinfo=None)
        day_start = day_start.replace(tzinfo=None)
        day_end = day_end.replace(tzinfo=None)
        for row in files:
            if not row.device_id:
                continue
            device_id = row.device_id.upper()
            item = raw.dense_heart_rate_coverage.setdefault(device_id, {
                "file_count_28d": 0,
                "covered_days": set(),
                "intervals_28d": [],
                "today_intervals": [],
                "payload_decoded": False,
            })
            item["file_count_28d"] += 1
            if row.date:
                item["covered_days"].add(row.date)
            if row.start_utc and row.end_utc and row.end_utc > row.start_utc:
                interval = (
                    max(row.start_utc, range_start),
                    min(row.end_utc, range_end),
                )
                if interval[1] > interval[0]:
                    item["intervals_28d"].append(interval)
                today_interval = (
                    max(row.start_utc, day_start),
                    min(row.end_utc, day_end),
                )
                if today_interval[1] > today_interval[0]:
                    item["today_intervals"].append(today_interval)
            item["payload_decoded"] = (
                item["payload_decoded"] or row.parse_status == "decoded"
            )
        for item in raw.dense_heart_rate_coverage.values():
            item["coverage_seconds_28d"] = _merged_interval_seconds(
                item.pop("intervals_28d")
            )
            item["today_coverage_seconds"] = _merged_interval_seconds(
                item.pop("today_intervals")
            )
        observed_ids = {
            point.device_id.upper()
            for points in raw.series.values()
            for point in points
            if point.device_id
        } | set(raw.dense_heart_rate_coverage)
        inventory = list(raw.device_models.items())
        for observed_id in observed_ids:
            if observed_id in raw.device_models:
                continue
            matches = [
                model
                for inventory_id, model in inventory
                if len(inventory_id) >= 12
                and observed_id.startswith(inventory_id[:6])
                and observed_id.endswith(inventory_id[-6:])
            ]
            if len(set(matches)) == 1:
                raw.device_models[observed_id] = matches[0]

    def _add_record_series(self, raw: RawDailyProfile) -> None:
        for day, record in raw.sleep_by_day.items():
            _append(raw, "sleep_duration", record.get("sleep_duration"), "min", day, record.get("source", "zepp"), "normalized_daily_record")
            _append(raw, "sleep_score", record.get("sleep_score"), "score", day, record.get("source", "zepp"), "normalized_daily_record")
        for day, record in raw.activity_by_day.items():
            _append(raw, "resting_hr", record.get("resting_hr"), "bpm", day, record.get("source", "zepp"), "normalized_daily_record", positive=True)
            _append(raw, "steps", record.get("steps"), "steps", day, record.get("source", "zepp"), "normalized_daily_record")
        for day, record in raw.training_by_day.items():
            _append(raw, "training_load", record.get("total_load"), "load", day, "zepp", "normalized_daily_record")
            _append(raw, "training_duration", record.get("total_duration"), "min", day, "zepp", "normalized_daily_record")

    def _add_daily_metrics(self, raw: RawDailyProfile, start: date) -> None:
        for row in self.repo.daily_metrics(raw.user_id, start, raw.day):
            _append(
                raw,
                row.metric,
                row.value,
                row.unit,
                row.date,
                row.source,
                row.source_scope,
                row.device_id,
                positive=False,
            )

    def _add_sample_metrics(self, raw: RawDailyProfile, start: date) -> None:
        start_at, _ = local_day_utc_bounds(start)
        _, end_at = local_day_utc_bounds(raw.day)
        end_at -= timedelta(microseconds=1)
        for metric in SAMPLE_METRICS:
            for row in self.repo.metric_samples(
                raw.user_id, metric, start_at, end_at, limit=MAX_SAMPLES_PER_METRIC
            ):
                _append(
                    raw,
                    row.metric,
                    row.value,
                    row.unit,
                    local_day(row.timestamp),
                    row.source,
                    row.source_scope,
                    row.device_id or None,
                    observed_at=row.timestamp,
                    positive=False,
                )

    def _add_heart_rate_samples(self, raw: RawDailyProfile, start: date) -> None:
        heart_rate_start = max(start, raw.day - timedelta(days=34))
        minute_values: dict[
            tuple[datetime, str, str, str | None, str], list[float]
        ] = defaultdict(list)
        for sleep_day, record in raw.sleep_by_day.items():
            if not heart_rate_start <= sleep_day <= raw.day:
                continue
            window = local_sleep_window(
                sleep_day, record.get("bedtime"), record.get("wake_time")
            )
            if window is None:
                continue
            start_at, end_at = (
                value.astimezone(timezone.utc) for value in window
            )
            for row in self.repo.metric_samples(
                raw.user_id,
                "heart_rate",
                start_at,
                end_at,
                limit=MAX_HEART_RATE_SAMPLES_PER_NIGHT,
            ):
                if not isinstance(row.value, (int, float)) or not 25 <= float(row.value) <= 240:
                    continue
                minute = row.timestamp.replace(second=0, microsecond=0)
                minute_values[(
                    minute,
                    row.source,
                    row.source_scope,
                    row.device_id or None,
                    row.unit,
                )].append(float(row.value))
        raw.heart_rate_samples = [
            SeriesPoint(
                metric="heart_rate",
                value=float(median(values)),
                unit=unit,
                day=local_day(minute),
                observed_at=minute,
                source=source,
                source_scope=source_scope,
                device_id=device_id,
            )
            for (minute, source, source_scope, device_id, unit), values
            in sorted(
                minute_values.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                    item[0][2],
                    item[0][3] or "",
                    item[0][4],
                ),
            )
        ]

    @staticmethod
    def _facts_for_day(raw: RawDailyProfile) -> dict[str, list[MeasurementFact]]:
        facts: dict[str, list[MeasurementFact]] = {}
        for metric, points in raw.series.items():
            current = [point for point in points if point.day == raw.day]
            if not current:
                continue
            streams: dict[tuple[str, str, str | None, str], list[SeriesPoint]] = {}
            for point in current:
                streams.setdefault(
                    (point.source, point.source_scope, point.device_id, point.unit), []
                ).append(point)
            facts[metric] = [
                MeasurementFact(
                    metric=metric,
                    value=round(float(median(item.value for item in stream)), 3),
                    unit=unit,
                    observed_at=max(stream, key=lambda item: _observed_key(item.observed_at)).observed_at,
                    provenance=Provenance(
                        source=source,
                        source_scope=scope,
                        device_id=device_id,
                    ),
                )
                for (source, scope, device_id, unit), stream in sorted(
                    streams.items(), key=lambda item: tuple(value or "" for value in item[0])
                )
            ]
        return facts

    def _quality(self, raw: RawDailyProfile) -> DataQuality:
        required = ["sleep_duration", "hrv"]
        missing = []
        if not _has_day(raw.series.get("sleep_duration", []), raw.day):
            missing.append("sleep_duration")
        if not any(_has_day(raw.series.get(metric, []), raw.day) for metric in ("hrv_rmssd", "hrv_sdnn", "sleep_hrv")):
            missing.append("hrv")

        if not missing:
            status = QualityStatus.SUFFICIENT
        elif len(missing) < len(required):
            status = QualityStatus.PARTIAL
        else:
            status = QualityStatus.INSUFFICIENT

        coverage = []
        for metric, points in sorted(raw.series.items()):
            if not points:
                continue
            observed = sorted((point.observed_at for point in points), key=_observed_key)
            coverage.append(Coverage(
                metric=metric,
                sample_count=len(points),
                distinct_days=len({point.day for point in points}),
                first_observed_at=observed[0],
                last_observed_at=observed[-1],
                device_ids=sorted({point.device_id for point in points if point.device_id}),
            ))

        flags = [
            QualityFlag(
                code="MISSING_REQUIRED_SIGNAL",
                severity="error",
                detail=f"缺少当天{SIGNAL_LABELS.get(metric, metric)}观测值。",
            )
            for metric in missing
        ]
        identity = self.repo.identity_context(raw.user_id)
        if identity["shared_local_user_count"] > 1:
            flags.append(QualityFlag(
                code="SOURCE_IDENTITY_SHARED",
                severity="warning",
                detail=(
                    "同一厂商身份关联了多个本地用户；本档案仅使用当前指定用户，"
                    "未合并其他历史。"
                ),
            ))
        if any(
            point.source_scope == "device" and not point.device_id
            for points in raw.series.values()
            for point in points
        ):
            flags.append(QualityFlag(
                code="UNKNOWN_DEVICE_ATTRIBUTION",
                severity="warning",
                detail="部分设备级观测值缺少设备标识。",
            ))
        for metric, points in raw.series.items():
            sample_points = [point for point in points if isinstance(point.observed_at, datetime)]
            if len(sample_points) >= MAX_SAMPLES_PER_METRIC:
                flags.append(QualityFlag(
                    code="SAMPLE_LIMIT_REACHED",
                    severity="error",
                    detail=f"指标 {metric} 的查询已达到 {MAX_SAMPLES_PER_METRIC} 条样本上限。",
                ))
        device_ids = sorted({
            point.device_id
            for points in raw.series.values()
            for point in points
            if point.device_id
        } | set(raw.dense_heart_rate_coverage))
        return DataQuality(
            status=status,
            status_label=QUALITY_LABELS[status.value],
            required_signals=required,
            required_signal_labels=labels(required, SIGNAL_LABELS),
            missing_required_signals=missing,
            missing_required_signal_labels=labels(missing, SIGNAL_LABELS),
            coverage=coverage,
            flags=flags,
            device_validity=[_device_validity(raw, device_id) for device_id in device_ids],
        )


def _records_by_day(records: list[dict]) -> dict[date, dict]:
    output = {}
    for record in records:
        value = record.get("date")
        if isinstance(value, str):
            value = date.fromisoformat(value)
        if isinstance(value, date):
            output[value] = record
    return output


def _merged_interval_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged)


def device_label(raw: RawDailyProfile, device_id: str | None) -> str:
    if not device_id:
        return "来源未标识设备"
    model = raw.device_models.get(device_id.upper())
    if model:
        return model
    all_ids = sorted({
        point.device_id
        for points in raw.series.values()
        for point in points
        if point.device_id
    } | set(raw.dense_heart_rate_coverage))
    index = all_ids.index(device_id) if device_id in all_ids else len(all_ids)
    return f"未识别设备 {chr(ord('A') + min(index, 25))}"


def device_measurement_site(raw: RawDailyProfile, device_id: str | None) -> str:
    model = raw.device_models.get((device_id or "").upper(), "").lower()
    if "helio strap" in model:
        return "upper_arm"
    if "balance 2" in model:
        return "wrist"
    return "unknown"


def _device_validity(raw: RawDailyProfile, device_id: str) -> DeviceValidity:
    label = device_label(raw, device_id)
    site = device_measurement_site(raw, device_id)
    if site == "upper_arm":
        return DeviceValidity(
            device_id=device_id,
            device_label=label,
            measurement_site=site,
            status="LIMITED_BY_EVIDENCE",
            evidence_refs=[
                "ARM_PPG_OH1_2019",
                "ARM_WRIST_PPG_2025",
                "PPG_ERROR_SOURCES_2020",
                "PRV_HRV_REVIEW_2013",
            ],
            limitations=[
                "上臂 PPG 形态有外部心率验证，但不是 Helio Strap 型号专项验证",
                "PPG 脉率变异性不能在所有场景下等同于 ECG HRV",
            ],
        )
    if site == "wrist":
        return DeviceValidity(
            device_id=device_id,
            device_label=label,
            measurement_site=site,
            status="LIMITED_BY_EVIDENCE",
            evidence_refs=["WRIST_PPG_META_2020", "PPG_ERROR_SOURCES_2020"],
            limitations=["腕部 PPG 误差随活动类型和运动伪影变化"],
        )
    return DeviceValidity(
        device_id=device_id,
        device_label=label,
        measurement_site="unknown",
    )


def _append(
    raw: RawDailyProfile,
    metric: str,
    value: object,
    unit: str,
    day: date,
    source: str,
    source_scope: str,
    device_id: str | None = None,
    *,
    observed_at: datetime | date | None = None,
    positive: bool = False,
) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    numeric = float(value)
    if positive and numeric <= 0:
        return
    raw.series.setdefault(metric, []).append(SeriesPoint(
        metric=metric,
        value=numeric,
        unit=unit,
        day=day,
        observed_at=observed_at or day,
        source=source,
        source_scope=source_scope,
        device_id=device_id,
    ))


def _has_day(points: list[SeriesPoint], day: date) -> bool:
    return any(point.day == day for point in points)


def _observed_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.combine(value, time.min)
