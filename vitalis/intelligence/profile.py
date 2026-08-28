"""Load normalized records into an analysis-ready, identity-isolated profile."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import median

from vitalis.intelligence.contracts import (
    Coverage,
    DataQuality,
    DeviceValidity,
    MeasurementFact,
    Provenance,
    QualityFlag,
    QualityStatus,
)
from vitalis.storage import HealthRepository
from vitalis.time import local_day, local_day_utc_bounds
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
)
MAX_SAMPLES_PER_METRIC = 1_000_000


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
    sleep_by_day: dict[date, dict] = field(default_factory=dict)
    activity_by_day: dict[date, dict] = field(default_factory=dict)
    training_by_day: dict[date, dict] = field(default_factory=dict)
    workouts: list[dict] = field(default_factory=list)
    facts: dict[str, list[MeasurementFact]] = field(default_factory=dict)
    data_quality: DataQuality | None = None


class ProfileLoader:
    """Build one local identity's profile without cross-user reconciliation."""

    def __init__(self, repo: HealthRepository):
        self.repo = repo

    def load(self, user_id: str, day: date) -> RawDailyProfile:
        start = day - timedelta(days=28)
        raw = RawDailyProfile(user_id=user_id, day=day)
        raw.sleep_by_day = _records_by_day(self.repo.sleep_range(user_id, start, day))
        raw.activity_by_day = _records_by_day(self.repo.activity_range(user_id, start, day))
        raw.training_by_day = _records_by_day(self.repo.training_range(user_id, start, day))

        self._add_record_series(raw)
        self._add_daily_metrics(raw, start)
        self._add_sample_metrics(raw, start)
        raw.workouts = [
            {
                "workout_id": row.workout_id,
                "started_at": row.started_at,
                "source": row.source,
                "vendor_source": row.vendor_source,
                "local_day": local_day(row.started_at) if row.started_at else None,
                "detail_available": row.detail_synced,
                "data": row.data or {},
            }
            for row in self.repo.workouts(user_id, start - timedelta(days=1), day)
            if row.started_at and start <= local_day(row.started_at) <= day
        ]
        raw.facts = self._facts_for_day(raw)
        raw.data_quality = self._quality(raw)
        return raw

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
        })
        return DataQuality(
            status=status,
            status_label=QUALITY_LABELS[status.value],
            required_signals=required,
            required_signal_labels=labels(required, SIGNAL_LABELS),
            missing_required_signals=missing,
            missing_required_signal_labels=labels(missing, SIGNAL_LABELS),
            coverage=coverage,
            flags=flags,
            device_validity=[DeviceValidity(device_id=device_id) for device_id in device_ids],
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
