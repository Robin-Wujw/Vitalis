"""Load normalized records into an analysis-ready, identity-isolated profile."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import logging
from statistics import median

from vitalis.intelligence.contracts import (
    Coverage,
    DataQuality,
    DeviceValidity,
    MeasurementFact,
    Provenance,
    QualityFlag,
    QualityStatus,
    ProfileSource,
    Sex,
    TrainingPreferences,
    UserProfile,
)
from vitalis.storage import HealthRepository
from vitalis.time import local_day, local_day_utc_bounds, local_sleep_window
from .localization import QUALITY_LABELS, SIGNAL_LABELS, labels
from .open_health.common import OpenHealthObservation


SAMPLE_METRICS = (
    "hrv_rmssd",
    "hrv_sdnn",
    "spo2",
    "spo2_apnea_low",
)
MAX_SAMPLES_PER_METRIC = 1_000_000
PROFILE_HISTORY_DAYS = 180
DETAIL_WORKOUTS_PER_FAMILY = 8
log = logging.getLogger("vitalis.intelligence.profile")


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
    feedback_by_workout: dict[tuple[str, str], list] = field(default_factory=dict)
    training_preferences: TrainingPreferences | None = None
    user_profile: UserProfile = field(default_factory=lambda: UserProfile(user_id=""))
    device_models: dict[str, str] = field(default_factory=dict)
    dense_heart_rate_coverage: dict[str, dict] = field(default_factory=dict)
    facts: dict[str, list[MeasurementFact]] = field(default_factory=dict)
    data_quality: DataQuality | None = None
    open_health_observations: list[OpenHealthObservation] = field(default_factory=list)
    open_health_load_workouts: list = field(default_factory=list)
    open_health_load_queried_days: list[date] = field(default_factory=list)
    open_health_load_truncated: bool = False
    open_health_rhr_by_day: dict[date, float] = field(default_factory=dict)
    open_health_input_failed: bool = False


class ProfileLoader:
    """Build one local identity's profile without cross-user reconciliation."""

    def __init__(self, repo: HealthRepository):
        self.repo = repo

    def load(
        self,
        user_id: str,
        day: date,
        history_days: int = PROFILE_HISTORY_DAYS,
        profile: UserProfile | None = None,
    ) -> RawDailyProfile:
        start = day - timedelta(days=history_days - 1)
        raw = RawDailyProfile(user_id=user_id, day=day)
        raw.user_profile = profile or self.repo.user_profile(user_id)
        raw.training_preferences = self.repo.training_preferences(user_id)
        raw.sleep_by_day = _records_by_day(self.repo.sleep_range(user_id, start, day))
        raw.activity_by_day = _records_by_day(self.repo.activity_range(user_id, start, day))
        raw.training_by_day = _records_by_day(self.repo.training_range(user_id, start, day))

        self._add_record_series(raw)
        self._add_daily_metrics(raw, start)
        self._add_sample_metrics(raw, start)
        self._add_heart_rate_samples(raw, start)
        try:
            self._add_open_health_inputs(raw)
        except Exception:
            # Shadow-only inputs must never prevent the core profile from loading.
            raw.open_health_input_failed = True
            log.exception("open health input loading failed: user=%s day=%s", user_id, day)
        self._add_device_context(raw)
        workout_rows = [
            row
            for row in self.repo.workouts(user_id, start, day)
            if row.started_at and start <= local_day(row.started_at) <= day
        ]
        detail_keys: list[tuple[str, str]] = []
        for workout_type in ("running", "strength"):
            family_rows = [
                row for row in workout_rows
                if row.detail_synced
                and row.started_at
                and local_day(row.started_at) >= day - timedelta(days=27)
                and str((row.data or {}).get("type") or "").lower() == workout_type
            ]
            detail_keys.extend(
                (row.source, row.workout_id)
                for row in family_rows[:DETAIL_WORKOUTS_PER_FAMILY]
            )
        samples_by_workout: dict[tuple[str, str], list] = defaultdict(list)
        for sample in self.repo.workout_metric_samples_for_workouts(
            user_id, detail_keys
        ):
            samples_by_workout[(sample.source, sample.workout_id)].append(sample)
        workout_keys = [(row.source, row.workout_id) for row in workout_rows]
        exercises_by_workout = self.repo.strength_exercises_for_workout_keys(
            user_id, workout_keys
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
                "samples": samples_by_workout.get((row.source, row.workout_id), []),
                "confirmed_exercises": exercises_by_workout.get(
                    (row.source, row.workout_id), []
                ),
                "data": row.data or {},
            }
            for row in workout_rows
        ]
        raw.facts = self._facts_for_day(raw)
        raw.data_quality = self._quality(raw)
        return raw

    def _add_open_health_inputs(self, raw: RawDailyProfile) -> None:
        """Build isolated nightly observations and the explicit 42-day load input."""
        nightly_start = raw.day - timedelta(days=27)
        nightly_days = [
            nightly_start + timedelta(days=index) for index in range(28)
        ]

        def stream_key(point: SeriesPoint) -> tuple[str, str, str | None]:
            return point.source, point.source_scope, point.device_id

        rmssd_streams: dict[tuple[str, str, str | None], dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
        for point in raw.series.get("hrv_rmssd", []):
            sleep_day = _sleep_day_for_point(point, raw.sleep_by_day)
            if (
                sleep_day is not None
                and nightly_start <= sleep_day <= raw.day
                and point.value > 0
            ):
                rmssd_streams[stream_key(point)][sleep_day].append(point)

        valid_rmssd_streams = {}
        for key, by_day in rmssd_streams.items():
            valid_days = {
                observation_day: points
                for observation_day, points in by_day.items()
                if len(points) >= 3 and _observed_span_minutes(points) >= 30
            }
            if valid_days:
                valid_rmssd_streams[key] = valid_days
        canonical_rmssd = _canonical_stream(
            valid_rmssd_streams, required_day=raw.day
        )

        rhr_streams: dict[tuple[str, str, str | None], dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
        for metric in ("sleep_rhr", "resting_hr"):
            candidate: dict[tuple[str, str, str | None], dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
            for point in raw.series.get(metric, []):
                if nightly_start <= point.day <= raw.day and point.value > 0:
                    candidate[stream_key(point)][point.day].append(point)
            if candidate:
                rhr_streams = candidate
                break
        canonical_rhr = _canonical_stream(rhr_streams, required_day=raw.day)
        resp_streams: dict[tuple[str, str, str | None], dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
        for point in raw.series.get("respiratory_rate", []):
            if nightly_start <= point.day <= raw.day and point.value > 0:
                resp_streams[stream_key(point)][point.day].append(point)
        canonical_resp = _canonical_stream(resp_streams, required_day=raw.day)

        observations = []
        for observation_day in nightly_days:
            sleep = raw.sleep_by_day.get(observation_day, {})
            rmssd_points = (valid_rmssd_streams.get(canonical_rmssd, {}) if canonical_rmssd else {}).get(observation_day, [])
            if not (
                len(rmssd_points) >= 3
                and _observed_span_minutes(rmssd_points) >= 30
            ):
                rmssd_points = []
            source_key = canonical_rmssd or canonical_rhr or canonical_resp
            rhr_key = canonical_rhr if canonical_rhr == source_key else None
            resp_key = canonical_resp if canonical_resp == source_key else None
            rhr_points = (rhr_streams.get(rhr_key, {}) if rhr_key else {}).get(observation_day, [])
            resp_points = (resp_streams.get(resp_key, {}) if resp_key else {}).get(observation_day, [])
            observations.append(OpenHealthObservation.model_validate({
                "date": observation_day,
                "rmssd_ms": _median_value(rmssd_points),
                "rhr_bpm": _median_value(rhr_points),
                "respiratory_rate": _median_value(resp_points),
                "sleep_minutes": _numeric(sleep.get("sleep_duration")),
                "time_in_bed_minutes": _numeric(sleep.get("time_in_bed_minutes")),
                "bedtime": sleep.get("bedtime"),
                "wake_time": sleep.get("wake_time"),
                "nap_minutes": _numeric(sleep.get("nap_minutes")),
                "naps_known": "nap_minutes" in sleep or "has_nap" in sleep,
                "source": source_key[0] if source_key else str(sleep.get("source", "zepp")),
                "source_scope": source_key[1] if source_key else "nightly_observation",
                "device_id": source_key[2] if source_key else None,
                "sleep_source": str(sleep.get("source", "zepp")),
                "sleep_source_scope": str(sleep.get("source_scope", "normalized_daily_record")),
                "sleep_device_id": sleep.get("device_id") or None,
                "sample_count": len(rmssd_points) or None,
                "span_minutes": _observed_span_minutes(rmssd_points) if rmssd_points else None,
            }))
        raw.open_health_observations = observations

        load_start = raw.day - timedelta(days=41)
        sex = raw.user_profile.sex
        hrmax = raw.user_profile.confirmed_hrmax_bpm
        load_profile_ready = (
            sex is not None
            and sex.value == Sex.MALE
            and sex.source == ProfileSource.USER_CONFIRMED
            and hrmax is not None
            and hrmax.source == ProfileSource.USER_CONFIRMED
        )
        raw.open_health_load_workouts = (
            self.repo.open_health_load_inputs(
                raw.user_id,
                load_start,
                raw.day,
                metric="heart_rate",
            )
            if load_profile_ready
            else []
        )
        raw.open_health_load_truncated = bool(
            getattr(raw.open_health_load_workouts, "truncated", False)
        )
        # A local database range query does not prove vendor coverage. The durable
        # sync ledger will populate verified calendar days in a later task.
        raw.open_health_load_queried_days = []
        raw.open_health_rhr_by_day = {}
        for metric in ("sleep_rhr", "resting_hr"):
            values = _canonical_daily_values(
                raw.series.get(metric, []), load_start, raw.day
            )
            if values:
                raw.open_health_rhr_by_day = values
                break

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
            _append(raw, "sleep_wake_count", record.get("wake_count"), "count", day, record.get("source", "zepp"), "normalized_daily_record")
        for day, record in raw.activity_by_day.items():
            _append(raw, "resting_hr", record.get("resting_hr"), "bpm", day, record.get("source", "zepp"), "normalized_daily_record", positive=True)
            _append(raw, "steps", record.get("steps"), "steps", day, record.get("source", "zepp"), "normalized_daily_record")
        for day, record in raw.training_by_day.items():
            source = record.get("source", "canonical_workouts")
            _append(raw, "training_load", record.get("total_load"), "load", day, source, "normalized_daily_record")
            _append(raw, "training_duration", record.get("total_duration"), "min", day, source, "normalized_daily_record")

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
                row.device_id or None,
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
        heart_rate_start = max(start, raw.day - timedelta(days=27))
        minute_values: dict[
            tuple[datetime, str, str, str | None, str], float
        ] = {}
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
            for minute, value, source, source_scope, device_id, unit in (
                self.repo.heart_rate_minute_medians(
                    raw.user_id, start_at, end_at
                )
            ):
                minute_values[(
                    minute,
                    source,
                    source_scope,
                    device_id,
                    unit,
                )] = value
        raw.heart_rate_samples = [
            SeriesPoint(
                metric="heart_rate",
                value=float(value),
                unit=unit,
                day=local_day(minute),
                observed_at=minute,
                source=source,
                source_scope=source_scope,
                device_id=device_id,
            )
            for (minute, source, source_scope, device_id, unit), value
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


def _numeric(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _in_sleep_window(
    point: SeriesPoint, record: dict, *, sleep_day: date | None = None
) -> bool:
    if not isinstance(point.observed_at, datetime):
        return False
    window = local_sleep_window(
        sleep_day or point.day, record.get("bedtime"), record.get("wake_time")
    )
    if window is None:
        return False
    start, end = window
    timestamp = point.observed_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return start <= timestamp.astimezone(timezone.utc) < end


def _sleep_day_for_point(
    point: SeriesPoint, records: dict[date, dict]
) -> date | None:
    for candidate in (point.day, point.day + timedelta(days=1)):
        record = records.get(candidate)
        if record and _in_sleep_window(point, record, sleep_day=candidate):
            return candidate
    return None


def _observed_span_minutes(points: list[SeriesPoint]) -> float:
    timestamps = [point.observed_at for point in points if isinstance(point.observed_at, datetime)]
    if len(timestamps) < 2:
        return 0.0
    return (max(timestamps) - min(timestamps)).total_seconds() / 60.0


def _median_value(points: list[SeriesPoint]) -> float | None:
    return float(median(point.value for point in points)) if points else None


def _canonical_stream(
    streams: dict, *, required_day: date | None = None
) -> tuple | None:
    candidates = [
        key for key in streams
        if required_day is None or required_day in streams[key]
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda key: (
            len(streams[key]),
            sum(len(points) for points in streams[key].values()),
            tuple(value or "" for value in key),
        ),
    )


def _canonical_daily_values(
    points: list[SeriesPoint], start: date, end: date
) -> dict[date, float]:
    streams: dict[tuple[str, str, str | None], dict[date, list[SeriesPoint]]] = defaultdict(lambda: defaultdict(list))
    for point in points:
        if start <= point.day <= end and point.value > 0:
            streams[(point.source, point.source_scope, point.device_id)][point.day].append(point)
    key = _canonical_stream(streams)
    if key is None:
        return {}
    return {
        day: float(median(item.value for item in values))
        for day, values in streams[key].items()
    }


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
