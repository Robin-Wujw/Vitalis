"""Deterministic feature analyzers over normalized profile data."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import log
from statistics import median

from .baseline import BaselineEngine, daily_stream_values
from .contracts import (
    Availability,
    BaselineStats,
    ConfidenceBand,
    Deviation,
    HeartRateCoverageFeature,
    HrvFeatures,
    HrvStreamFeature,
    LoadState,
    NocturnalHeartRateFeature,
    OxygenFeatures,
    OvernightVitalsFeatures,
    ProfileStates,
    RecoveryFeatures,
    RecoveryState,
    SleepFeatures,
    SleepState,
    TrainingFeatures,
    WorkoutFeature,
)
from .profile import (
    RawDailyProfile,
    SeriesPoint,
    device_label,
    device_measurement_site,
)
from .running import RunningAnalyzer
from .strength import StrengthAnalyzer
from vitalis.time import local_sleep_window, utc_to_local
from vitalis.connectors.zepp.sport_types import CATEGORY_LABELS, FAMILY_LABELS
from .localization import (
    AVAILABILITY_LABELS,
    CONFIDENCE_LABELS,
    DRIVER_LABELS,
    LIMITATION_LABELS,
    LOAD_LABELS,
    RECOVERY_LABELS,
    SLEEP_LABELS,
    labels,
)


@dataclass(frozen=True)
class _HrvCandidate:
    metric: str
    metric_priority: int
    source: str
    scope: str
    device_id: str | None
    device_label: str
    measurement_site: str
    value: float
    sample_count_today: int
    baseline: BaselineStats | None

    @property
    def selection_key(self) -> tuple:
        baseline_available = int(bool(
            self.baseline and self.baseline.status == Availability.AVAILABLE
        ))
        baseline_days = self.baseline.distinct_days if self.baseline else 0
        arm_worn = int(self.measurement_site == "upper_arm")
        return (
            baseline_available,
            self.metric_priority,
            baseline_days,
            min(self.sample_count_today, 1_440),
            arm_worn,
            self.source,
            self.scope,
            self.device_id or "",
        )


class SleepAnalyzer:
    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> tuple[SleepFeatures, SleepState]:
        record = raw.sleep_by_day.get(raw.day)
        if not record:
            return (
                SleepFeatures(
                    status=Availability.INSUFFICIENT_DATA,
                    status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                    limitations=["target_day_sleep_missing"],
                    limitation_labels=[LIMITATION_LABELS["target_day_sleep_missing"]],
                ),
                SleepState.INSUFFICIENT_DATA,
            )
        duration = int(record["sleep_duration"])
        baseline = _baseline_for(
            baselines, "sleep_duration", "zepp", "normalized_daily_record", None
        )
        deviation = BaselineEngine.deviation(duration, baseline) if baseline else None
        if duration < 420 or (deviation and deviation.direction == "below"):
            state = SleepState.BELOW_BASELINE
        elif deviation and deviation.direction == "above":
            state = SleepState.ABOVE_BASELINE
        else:
            state = SleepState.NEAR_BASELINE

        bedtimes = [
            _clock_minutes(item.get("bedtime"))
            for day, item in raw.sleep_by_day.items()
            if raw.day - timedelta(days=7) <= day <= raw.day and item.get("bedtime")
        ]
        bedtimes = [value for value in bedtimes if value is not None]
        regularity = median(abs(value - median(bedtimes)) for value in bedtimes) if len(bedtimes) >= 3 else None
        limitations = ["sleep_stages_are_trend_only"]
        if baseline is None or baseline.status == Availability.INSUFFICIENT_DATA:
            limitations.append("sleep_28d_baseline_insufficient")
        if regularity is None:
            limitations.append("sleep_regularity_history_insufficient")
        rem_sleep = record.get("rem_sleep")
        return SleepFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            duration_minutes=duration,
            bedtime=str(record["bedtime"]) if record.get("bedtime") else None,
            wake_time=str(record["wake_time"]) if record.get("wake_time") else None,
            deep_minutes=int(record.get("deep_sleep", 0)),
            rem_minutes=int(rem_sleep) if rem_sleep is not None else None,
            awake_minutes=int(record.get("awake", 0)),
            vendor_sleep_score=record.get("sleep_score"),
            duration_deviation=deviation,
            regularity_minutes=round(float(regularity), 1) if regularity is not None else None,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        ), state


class HrvAnalyzer:
    METRIC_PRIORITY = {"hrv_rmssd": 3, "sleep_hrv": 2, "hrv_sdnn": 1}

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> HrvFeatures:
        candidates: list[_HrvCandidate] = []
        for metric, priority in self.METRIC_PRIORITY.items():
            for stream, value in daily_stream_values(raw.series.get(metric, []), raw.day).items():
                source, scope, device_id = stream
                baseline = _baseline_for(baselines, metric, source, scope, device_id)
                sample_count = sum(
                    1
                    for point in raw.series.get(metric, [])
                    if point.day == raw.day
                    and (point.source, point.source_scope, point.device_id) == stream
                )
                candidates.append(_HrvCandidate(
                    metric=metric,
                    metric_priority=priority,
                    source=source,
                    scope=scope,
                    device_id=device_id,
                    device_label=device_label(raw, device_id),
                    measurement_site=device_measurement_site(raw, device_id),
                    value=value,
                    sample_count_today=sample_count,
                    baseline=baseline,
                ))
        if not candidates:
            nocturnal_hr = _analyze_nocturnal_heart_rate(raw, None)
            nocturnal_deviation = _nocturnal_deviation(nocturnal_hr)
            return HrvFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                rhr_bpm=nocturnal_hr.median_bpm,
                rhr_deviation=nocturnal_deviation,
                nocturnal_heart_rate=nocturnal_hr,
                limitations=["target_day_hrv_missing"],
                limitation_labels=[LIMITATION_LABELS["target_day_hrv_missing"]],
            )
        selected = max(candidates, key=lambda item: item.selection_key)
        device_id = selected.device_id
        metric = selected.metric
        value = selected.value
        baseline = selected.baseline
        deviation = BaselineEngine.deviation(value, baseline) if baseline else None
        (
            recent_7d_median,
            previous_7d_median,
            recent_7d_change,
            recent_7d_direction,
            recent_7d_days,
            previous_7d_days,
        ) = _same_stream_hrv_change(raw, selected)
        metric_candidates = [item for item in candidates if item.metric == metric]
        streams = [
            HrvStreamFeature(
                metric=item.metric,
                device_id=item.device_id,
                device_label=item.device_label,
                measurement_site=item.measurement_site,
                value_ms=round(item.value, 2),
                ln_rmssd=(
                    round(log(item.value), 4)
                    if item.metric == "hrv_rmssd" and item.value > 0
                    else None
                ),
                deviation=(
                    BaselineEngine.deviation(item.value, item.baseline)
                    if item.baseline else None
                ),
                sample_count_today=item.sample_count_today,
                baseline_distinct_days=(
                    item.baseline.distinct_days if item.baseline else 0
                ),
                selected=item == selected,
            )
            for item in sorted(
                metric_candidates,
                key=lambda item: (item.device_label, item.source, item.scope),
            )
        ]
        (
            fusion_direction,
            fusion_confidence,
            fusion_summary,
            corroboration_status,
            corroborating_stream_count,
            corroboration_affects_decision,
        ) = _corroborate_hrv_streams(selected, metric_candidates)

        nocturnal_hr = _analyze_nocturnal_heart_rate(raw, device_id)
        rhr_candidates = []
        for rhr_metric in ("sleep_rhr", "resting_hr"):
            for stream, rhr in daily_stream_values(raw.series.get(rhr_metric, []), raw.day).items():
                rhr_baseline = _baseline_for(baselines, rhr_metric, *stream)
                same_device = int(stream[2] == device_id and device_id is not None)
                available = int(bool(rhr_baseline and rhr_baseline.status == Availability.AVAILABLE))
                rhr_candidates.append((same_device, available, rhr_metric == "sleep_rhr", rhr, rhr_baseline))
        rhr_value = None
        rhr_deviation = None
        if (
            nocturnal_hr.status == Availability.AVAILABLE
            and nocturnal_hr.median_bpm is not None
            and nocturnal_hr.direction != "unknown"
        ):
            rhr_value = nocturnal_hr.median_bpm
            rhr_deviation = _nocturnal_deviation(nocturnal_hr)
        elif rhr_candidates:
            _, _, _, rhr_value, rhr_baseline = max(rhr_candidates, key=lambda item: item[:3])
            rhr_deviation = BaselineEngine.deviation(rhr_value, rhr_baseline) if rhr_baseline else None

        limitations = []
        if baseline is None or baseline.status == Availability.INSUFFICIENT_DATA:
            limitations.append("hrv_28d_baseline_insufficient")
        if corroboration_status == "conflicting":
            limitations.append("multi_device_hrv_disagreement")
        if rhr_value is None:
            limitations.append("target_day_rhr_missing")
        elif rhr_deviation is None or rhr_deviation.direction == "unknown":
            limitations.append("rhr_28d_baseline_insufficient")
        heart_rate_coverage = []
        for coverage_device_id, item in sorted(
            raw.dense_heart_rate_coverage.items(),
            key=lambda value: device_label(raw, value[0]),
        ):
            heart_rate_coverage.append(HeartRateCoverageFeature(
                device_id=coverage_device_id,
                device_label=device_label(raw, coverage_device_id),
                measurement_site=device_measurement_site(raw, coverage_device_id),
                today_coverage_minutes=round(
                    item["today_coverage_seconds"] / 60, 1
                ),
                coverage_hours_28d=round(
                    item["coverage_seconds_28d"] / 3_600, 1
                ),
                covered_days_28d=len(item["covered_days"]),
                file_count_28d=item["file_count_28d"],
                payload_decoded=item["payload_decoded"],
            ))
        if heart_rate_coverage and not any(
            item.payload_decoded for item in heart_rate_coverage
        ):
            limitations.append("dense_heart_rate_payload_not_decoded")
        return HrvFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            preferred_metric=metric,
            preferred_device_id=device_id,
            preferred_device_label=selected.device_label,
            value_ms=round(value, 2),
            ln_rmssd=round(log(value), 4) if metric == "hrv_rmssd" and value > 0 else None,
            deviation=deviation,
            recent_7d_median_ms=recent_7d_median,
            previous_7d_median_ms=previous_7d_median,
            recent_7d_change_percent=recent_7d_change,
            recent_7d_direction=recent_7d_direction,
            recent_7d_days=recent_7d_days,
            previous_7d_days=previous_7d_days,
            fusion_method="canonical_device_with_corroboration",
            fusion_direction=fusion_direction,
            fusion_confidence=fusion_confidence,
            fusion_confidence_label=CONFIDENCE_LABELS[fusion_confidence.value],
            fusion_summary=fusion_summary,
            corroboration_status=corroboration_status,
            corroborating_stream_count=corroborating_stream_count,
            corroboration_affects_decision=corroboration_affects_decision,
            rhr_bpm=round(rhr_value, 1) if rhr_value is not None else None,
            rhr_deviation=rhr_deviation,
            nocturnal_heart_rate=nocturnal_hr,
            streams=streams,
            heart_rate_coverage=heart_rate_coverage,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


def _same_stream_hrv_change(raw: RawDailyProfile, selected: _HrvCandidate):
    points = [
        point
        for point in raw.series.get(selected.metric, [])
        if point.source == selected.source
        and point.source_scope == selected.scope
        and point.device_id == selected.device_id
    ]
    by_day: dict[date, list[float]] = defaultdict(list)
    for point in points:
        by_day[point.day].append(point.value)
    daily = {day: median(values) for day, values in by_day.items()}
    recent = [
        value for day, value in daily.items()
        if raw.day - timedelta(days=6) <= day <= raw.day
    ]
    previous = [
        value for day, value in daily.items()
        if raw.day - timedelta(days=13) <= day <= raw.day - timedelta(days=7)
    ]
    if len(recent) < 3 or len(previous) < 3:
        return None, None, None, "unknown", len(recent), len(previous)
    recent_median = float(median(recent))
    previous_median = float(median(previous))
    if previous_median <= 0:
        return round(recent_median, 2), round(previous_median, 2), None, "unknown", len(recent), len(previous)
    change = (recent_median - previous_median) / previous_median * 100
    direction = "above" if change > 5 else "below" if change < -5 else "near"
    return (
        round(recent_median, 2),
        round(previous_median, 2),
        round(change, 1),
        direction,
        len(recent),
        len(previous),
    )


def _nocturnal_deviation(feature: NocturnalHeartRateFeature) -> Deviation | None:
    if feature.direction == "unknown":
        return None
    return Deviation(
        metric="nocturnal_heart_rate",
        baseline_window_days=28,
        percent=feature.deviation_percent,
        direction=feature.direction,
    )


def _analyze_nocturnal_heart_rate(
    raw: RawDailyProfile,
    preferred_hrv_device: str | None,
) -> NocturnalHeartRateFeature:
    summaries: dict[date, dict[str | None, dict]] = {}
    for sleep_day, record in raw.sleep_by_day.items():
        window = local_sleep_window(
            sleep_day, record.get("bedtime"), record.get("wake_time")
        )
        if window is None:
            continue
        start, end = window
        by_device: dict[str | None, list[SeriesPoint]] = defaultdict(list)
        for point in raw.heart_rate_samples:
            if not isinstance(point.observed_at, datetime):
                continue
            if start <= utc_to_local(point.observed_at) <= end:
                by_device[point.device_id].append(point)
        for device_id, points in by_device.items():
            summary = _night_heart_rate_summary(points, start, end)
            if summary is not None:
                summaries.setdefault(sleep_day, {})[device_id] = summary

    target = summaries.get(raw.day, {})
    if not target:
        return NocturnalHeartRateFeature(
            status=Availability.INSUFFICIENT_DATA,
            status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
        )
    history_counts = {
        device_id: sum(
            device_id in values
            for day, values in summaries.items()
            if raw.day - timedelta(days=28) <= day < raw.day
        )
        for device_id in target
    }
    device_id, current = max(
        target.items(),
        key=lambda item: (
            2 if item[1]["coverage_ratio"] >= 0.8 else 1,
            int(device_measurement_site(raw, item[0]) == "upper_arm"),
            int(item[0] == preferred_hrv_device and item[0] is not None),
            history_counts[item[0]],
            item[1]["coverage_ratio"],
        ),
    )
    historical = [
        values[device_id]["median_bpm"]
        for day, values in summaries.items()
        if raw.day - timedelta(days=28) <= day < raw.day and device_id in values
    ]
    baseline = float(median(historical)) if len(historical) >= 7 else None
    change = (
        (current["median_bpm"] - baseline) / baseline * 100
        if baseline and baseline > 0 else None
    )
    direction = "unknown"
    if change is not None:
        mad = median(abs(value - baseline) for value in historical)
        robust_z = (
            0.6745 * (current["median_bpm"] - baseline) / mad if mad else None
        )
        comparison = robust_z if robust_z is not None else change / 3
        direction = "above" if comparison > 0.75 else "below" if comparison < -0.75 else "near"
    return NocturnalHeartRateFeature(
        status=Availability.AVAILABLE,
        status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
        device_id=device_id,
        device_label=device_label(raw, device_id),
        measurement_site=device_measurement_site(raw, device_id),
        baseline_median_bpm=round(baseline, 1) if baseline is not None else None,
        baseline_nights=len(historical),
        deviation_percent=round(change, 1) if change is not None else None,
        direction=direction,
        **current,
    )


def _night_heart_rate_summary(points, start: datetime, end: datetime):
    by_minute: dict[datetime, list[float]] = defaultdict(list)
    for point in points:
        observed = utc_to_local(point.observed_at).replace(second=0, microsecond=0)
        by_minute[observed].append(point.value)
    expected = max(int((end - start).total_seconds() // 60), 1)
    coverage = min(len(by_minute) / expected, 1.0)
    if len(by_minute) < 120 or coverage < 0.5:
        return None
    minute_values = sorted(
        (minute, float(median(values))) for minute, values in by_minute.items()
    )
    midpoint = start + (end - start) / 2
    first = [value for minute, value in minute_values if minute < midpoint]
    second = [value for minute, value in minute_values if minute >= midpoint]
    rolling = []
    for index in range(4, len(minute_values)):
        window = minute_values[index - 4:index + 1]
        if (window[-1][0] - window[0][0]).total_seconds() <= 6 * 60:
            rolling.append((
                window[2][0],
                float(median(value for _, value in window)),
            ))
    if not first or not second or not rolling:
        return None
    first_median = float(median(first))
    second_median = float(median(second))
    low_time, low_value = min(rolling, key=lambda item: item[1])
    return {
        "median_bpm": round(float(median(value for _, value in minute_values)), 1),
        "low_5m_bpm": round(low_value, 1),
        "low_5m_time": low_time.strftime("%H:%M"),
        "first_half_median_bpm": round(first_median, 1),
        "second_half_median_bpm": round(second_median, 1),
        "second_minus_first_bpm": round(second_median - first_median, 1),
        "sample_count": len(points),
        "coverage_ratio": round(coverage, 3),
    }


def _corroborate_hrv_streams(
    selected: _HrvCandidate,
    candidates: list[_HrvCandidate],
) -> tuple[str, ConfidenceBand, str, str, int, bool]:
    if not selected.baseline or selected.baseline.status != Availability.AVAILABLE:
        return (
            "unknown",
            ConfidenceBand.NONE,
            "主设备缺少可解释的个人基线",
            "insufficient",
            0,
            False,
        )
    selected_direction = BaselineEngine.deviation(
        selected.value, selected.baseline
    ).direction
    if selected_direction == "unknown":
        return (
            "unknown",
            ConfidenceBand.NONE,
            "主设备暂时无法与个人基线比较",
            "insufficient",
            0,
            False,
        )

    secondary_directions = []
    for item in candidates:
        if item == selected or not item.baseline:
            continue
        if item.baseline.status != Availability.AVAILABLE:
            continue
        direction = BaselineEngine.deviation(item.value, item.baseline).direction
        if direction != "unknown":
            secondary_directions.append(direction)

    if not secondary_directions:
        return (
            selected_direction,
            ConfidenceBand.MODERATE,
            "按连续主设备的个人基线判断",
            "not_available",
            0,
            False,
        )
    opposite = {
        "above": "below",
        "below": "above",
    }.get(selected_direction)
    if opposite is None or opposite not in secondary_directions:
        return (
            selected_direction,
            ConfidenceBand.MODERATE,
            "次设备未出现相反方向",
            "consistent",
            len(secondary_directions),
            False,
        )
    return (
        "unknown",
        ConfidenceBand.LOW,
        "可比设备出现相反方向，本次不采用 HRV 作为恢复依据",
        "conflicting",
        len(secondary_directions),
        True,
    )


class OvernightVitalsAnalyzer:
    """Interpret overnight respiratory and oxygen signals without diagnosing disease."""

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
        sleep: SleepFeatures,
    ) -> OvernightVitalsFeatures:
        respiratory, respiratory_deviation = _daily_metric_with_deviation(
            raw, baselines, "respiratory_rate"
        )
        temperature, _ = _daily_metric_with_deviation(
            raw, baselines, "skin_temp_delta"
        )
        oxygen = self._oxygen(raw, baselines, sleep)

        outliers = []
        if (
            respiratory_deviation
            and respiratory_deviation.direction == "above"
            and respiratory_deviation.robust_z is not None
            and respiratory_deviation.robust_z >= 2
        ):
            outliers.append("呼吸频率明显高于个人水平")
        if temperature is not None and abs(temperature) >= 0.5:
            relation = "高" if temperature > 0 else "低"
            outliers.append(f"腕温较厂商基线{relation} {abs(temperature):.1f} 摄氏度")
        if oxygen.repeated_elevation:
            outliers.append("夜间血氧下降指数连续偏高")

        available = any((
            respiratory is not None,
            temperature is not None,
            oxygen.status == Availability.AVAILABLE,
        ))
        limitations = []
        if respiratory is not None and respiratory_deviation is None:
            limitations.append("respiratory_rate_baseline_insufficient")
        return OvernightVitalsFeatures(
            status=(Availability.AVAILABLE if available else Availability.INSUFFICIENT_DATA),
            status_label=AVAILABILITY_LABELS[
                Availability.AVAILABLE.value
                if available else Availability.INSUFFICIENT_DATA.value
            ],
            respiratory_rate=round(respiratory, 1) if respiratory is not None else None,
            respiratory_rate_deviation=respiratory_deviation,
            skin_temperature_delta_c=(
                round(temperature, 2) if temperature is not None else None
            ),
            oxygen=oxygen,
            outlier_labels=outliers,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )

    @staticmethod
    def _oxygen(
        raw: RawDailyProfile,
        _baselines: dict[str, list[BaselineStats]],
        sleep: SleepFeatures,
    ) -> OxygenFeatures:
        # Zepp assigns a night's ODI summary to the preceding night date, while
        # Vitalis sleep records use the wake date.
        oxygen_day = raw.day - timedelta(days=1)
        candidates = []
        source_baselines = BaselineEngine().build(
            {"spo2_odi": raw.series.get("spo2_odi", [])}, oxygen_day
        )
        coverage_values = daily_stream_values(
            raw.series.get("spo2_measured_minutes", []), oxygen_day
        )
        event_values = daily_stream_values(
            raw.series.get("spo2_odi_events", []), oxygen_day
        )
        for stream, odi in daily_stream_values(
            raw.series.get("spo2_odi", []), oxygen_day
        ).items():
            source, scope, device_id = stream
            baseline = _baseline_for(
                source_baselines, "spo2_odi", source, scope, device_id
            )
            measured = coverage_values.get(stream)
            candidates.append((
                int(measured is not None),
                int(bool(baseline and baseline.status == Availability.AVAILABLE)),
                int(bool(stream[2])),
                measured or 0,
                stream,
                odi,
                baseline,
            ))
        if not candidates:
            return OxygenFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                limitations=["target_night_oxygen_missing", "oxygen_is_screening_only"],
                limitation_labels=labels(
                    ["target_night_oxygen_missing", "oxygen_is_screening_only"],
                    LIMITATION_LABELS,
                ),
            )

        _, _, _, measured, stream, odi, baseline = max(
            candidates, key=lambda item: item[:4]
        )
        sleep_minutes = sleep.duration_minutes
        coverage_ratio = (
            min(float(measured) / sleep_minutes, 1.0)
            if measured and sleep_minutes and sleep_minutes > 0 else None
        )
        coverage_ok = bool(
            measured >= 240
            and (coverage_ratio is None or coverage_ratio >= 0.7)
        )
        if not coverage_ok:
            return OxygenFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                device_id=stream[2],
                measured_minutes=int(measured) if measured else None,
                coverage_ratio=round(coverage_ratio, 3) if coverage_ratio is not None else None,
                odi_events_per_hour=round(float(odi), 2),
                limitations=["oxygen_coverage_insufficient", "oxygen_is_screening_only"],
                limitation_labels=labels(
                    ["oxygen_coverage_insufficient", "oxygen_is_screening_only"],
                    LIMITATION_LABELS,
                ),
            )

        deviation = BaselineEngine.deviation(odi, baseline) if baseline else None
        baseline_value = baseline.reference_value if baseline else None
        threshold = 5.0
        if baseline and baseline.status == Availability.AVAILABLE:
            threshold = max(
                threshold,
                float(baseline.reference_value or 0)
                + max(2 * float(baseline.mad or 0), 1.5),
            )
        recent = _same_stream_daily_values(
            raw.series.get("spo2_odi", []), oxygen_day, stream, 3
        )
        elevated_nights = sum(value >= threshold for value in recent)
        repeated = odi >= threshold and len(recent) >= 2 and elevated_nights >= 2
        if repeated:
            interpretation = "repeated_elevation"
        elif odi >= threshold:
            interpretation = "single_night_elevation"
        else:
            interpretation = "within_personal_range"

        night_values = _sleep_window_metric_values(raw, "spo2", stream[2])
        return OxygenFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            device_id=stream[2],
            median_percent=(
                round(float(median(night_values)), 1) if len(night_values) >= 20 else None
            ),
            lower_10th_percent=(
                round(_percentile(night_values, 0.1), 1) if len(night_values) >= 20 else None
            ),
            sample_count=len(night_values),
            measured_minutes=int(measured),
            coverage_ratio=round(coverage_ratio, 3) if coverage_ratio is not None else None,
            odi_events_per_hour=round(float(odi), 2),
            odi_events=(
                int(event_values[stream]) if stream in event_values else None
            ),
            odi_baseline=(round(float(baseline_value), 2) if baseline_value is not None else None),
            odi_deviation=deviation,
            repeated_elevation=repeated,
            interpretation=interpretation,
            limitations=["oxygen_is_screening_only"],
            limitation_labels=[LIMITATION_LABELS["oxygen_is_screening_only"]],
        )


class TrainingAnalyzer:
    STRENGTH_TYPES = {"strength", "strength_training", "weight_training", "functional_strength"}

    def analyze(
        self,
        raw: RawDailyProfile,
        baselines: dict[str, list[BaselineStats]],
    ) -> TrainingFeatures:
        if not raw.training_by_day and not raw.workouts:
            return TrainingFeatures(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                limitations=["training_history_missing"],
                limitation_labels=[LIMITATION_LABELS["training_history_missing"]],
            )
        today = raw.training_by_day.get(raw.day)
        recent_7 = [
            item for day, item in raw.training_by_day.items()
            if raw.day - timedelta(days=7) <= day < raw.day
        ]
        recent_28 = [
            item for day, item in raw.training_by_day.items()
            if raw.day - timedelta(days=28) <= day < raw.day
        ]
        today_load = float(today.get("total_load", 0)) if today else 0.0
        load_7d = sum(float(item.get("total_load", 0)) for item in recent_7)
        prior_week_loads = []
        for week in range(1, 4):
            week_end = raw.day - timedelta(days=week * 7 + 1)
            week_start = week_end - timedelta(days=6)
            prior_week_loads.append(sum(
                float(item.get("total_load", 0))
                for item_day, item in raw.training_by_day.items()
                if week_start <= item_day <= week_end
            ))
        has_full_comparison_window = bool(
            raw.training_by_day
            and min(raw.training_by_day) <= raw.day - timedelta(days=28)
        )
        reference = (
            sum(prior_week_loads) / len(prior_week_loads)
            if has_full_comparison_window and sum(prior_week_loads) > 0
            else None
        )
        load_change = (
            (load_7d - reference) / reference * 100
            if reference and reference > 0 else None
        )
        direction = "unknown"
        if load_change is not None:
            direction = (
                "above" if load_change > 25
                else "below" if load_change < -25
                else "near"
            )
        deviation = Deviation(
            metric="training_load_7d",
            baseline_window_days=21,
            percent=round(load_change, 1) if load_change is not None else None,
            direction=direction,
        ) if load_change is not None else None
        if direction == "above":
            load_state = LoadState.ELEVATED
        elif direction == "below":
            load_state = LoadState.LOW
        elif direction == "near":
            load_state = LoadState.NORMAL
        else:
            load_state = LoadState.INSUFFICIENT_DATA

        aerobic_minutes = 0
        strength_sessions = 0
        strength_days: list[date] = []
        type_counts: dict[str, int] = {}
        type_labels: dict[str, str] = {}
        mode_counts: dict[str, int] = {}
        recent_workouts: list[WorkoutFeature] = []
        for workout in raw.workouts:
            workout_day = workout.get("local_day")
            if not workout_day or workout_day < raw.day - timedelta(days=6):
                continue
            data = workout.get("data", {})
            workout_type = str(data.get("type", "")).lower()
            sport_mode = str(data.get("sport_mode") or "unknown")
            sport_mode_label = str(data.get("sport_mode_label") or "未知运动")
            family = str(data.get("training_family") or "skill")
            type_counts[workout_type] = type_counts.get(workout_type, 0) + 1
            type_labels[workout_type] = CATEGORY_LABELS.get(workout_type, "其他运动")
            mode_counts[sport_mode_label] = mode_counts.get(sport_mode_label, 0) + 1
            if family == "aerobic":
                aerobic_minutes += int(data.get("duration", 0) or 0)
            if workout_type in self.STRENGTH_TYPES:
                strength_sessions += 1
                strength_days.append(workout_day)
            recent_workouts.append(WorkoutFeature(
                date=workout_day,
                type=workout_type,
                type_label=CATEGORY_LABELS.get(workout_type, "其他运动"),
                sport_mode=sport_mode,
                sport_mode_label=sport_mode_label,
                training_family=family,
                training_family_label=FAMILY_LABELS.get(family, "未知"),
                recognition_confidence=str(data.get("recognition_confidence", "NONE")),
                recognition_confidence_label=str(data.get("recognition_confidence_label", "无法识别")),
                recognition_source=str(data.get("recognition_source", "missing_vendor_type")),
                recognition_source_label=str(data.get("recognition_source_label", "缺少厂商运动类型")),
                vendor_type_id=data.get("vendor_type_id"),
                duration_minutes=int(data.get("duration", 0) or 0),
                vendor_load=float(data.get("load", 0) or 0),
                heart_rate_avg_bpm=int(data.get("heart_rate_avg", 0) or 0) or None,
                heart_rate_max_bpm=int(data.get("heart_rate_max", 0) or 0) or None,
                detail_available=bool(workout.get("detail_available")),
            ))
        limitations = ["training_load_is_vendor_derived"]
        if reference is None:
            limitations.append("training_load_comparison_insufficient")
        limitations.append("session_rpe_unavailable")
        running = RunningAnalyzer().analyze(raw)
        strength = StrengthAnalyzer().analyze(raw)
        if running.sessions_28d and running.zone_method == "unavailable":
            limitations.append("aerobic_intensity_classification_unavailable")
        return TrainingFeatures(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            today_duration_minutes=int(today.get("total_duration", 0)) if today else 0,
            today_load=today_load,
            today_workouts=int(today.get("workout_count", 0)) if today else 0,
            duration_7d=sum(int(item.get("total_duration", 0)) for item in recent_7),
            load_7d=round(load_7d, 1),
            load_7d_reference=round(reference, 1) if reference is not None else None,
            load_7d_change_percent=(
                round(load_change, 1) if load_change is not None else None
            ),
            load_28d=round(sum(float(item.get("total_load", 0)) for item in recent_28), 1),
            aerobic_minutes_7d=aerobic_minutes,
            strength_sessions_7d=strength_sessions,
            days_since_last_strength=(raw.day - max(strength_days)).days if strength_days else None,
            workout_type_counts_7d=dict(sorted(type_counts.items())),
            workout_type_labels_7d=dict(sorted(type_labels.items())),
            sport_mode_counts_7d=dict(sorted(mode_counts.items())),
            recent_workouts=sorted(recent_workouts, key=lambda item: item.date, reverse=True),
            running=running,
            strength=strength,
            load_deviation=deviation,
            load_state=load_state,
            load_state_label=LOAD_LABELS[load_state.value],
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


class RecoveryAnalyzer:
    def analyze(
        self,
        raw: RawDailyProfile,
        sleep: SleepFeatures,
        sleep_state: SleepState,
        hrv: HrvFeatures,
        training: TrainingFeatures,
    ) -> RecoveryFeatures:
        positive = []
        negative = []
        hrv_direction = hrv.fusion_direction
        if (
            hrv_direction == "unknown"
            and not hrv.corroboration_affects_decision
            and hrv.deviation
        ):
            hrv_direction = hrv.deviation.direction
        if hrv_direction == "above":
            positive.append("HRV_ABOVE_BASELINE")
        elif hrv_direction == "below":
            negative.append("HRV_BELOW_BASELINE")
        if hrv.rhr_deviation and hrv.rhr_deviation.direction == "below":
            positive.append("RHR_BELOW_BASELINE")
        elif hrv.rhr_deviation and hrv.rhr_deviation.direction == "above":
            negative.append("RHR_ABOVE_BASELINE")
        if sleep_state == SleepState.ABOVE_BASELINE:
            positive.append("SLEEP_ABOVE_BASELINE")
        elif sleep_state == SleepState.BELOW_BASELINE:
            negative.append("SLEEP_BELOW_BASELINE")
        if training.load_state == LoadState.ELEVATED:
            negative.append("TRAINING_LOAD_ELEVATED")

        interpreted = sum((
            hrv_direction in {"above", "near", "below"},
            bool(
                hrv.rhr_deviation
                and hrv.rhr_deviation.direction in {"above", "near", "below"}
            ),
            sleep_state != SleepState.INSUFFICIENT_DATA,
            training.load_state != LoadState.INSUFFICIENT_DATA,
        ))
        if interpreted < 2:
            state = RecoveryState.INSUFFICIENT_DATA
            status = Availability.INSUFFICIENT_DATA
        elif len(negative) >= 2:
            state = RecoveryState.SUPPRESSED
            status = Availability.AVAILABLE
        elif len(positive) >= 2 and not negative:
            state = RecoveryState.GOOD
            status = Availability.AVAILABLE
        else:
            state = RecoveryState.NORMAL
            status = Availability.AVAILABLE

        readiness = _current_median(raw.series.get("readiness", []), raw.day)
        charge = _current_median(
            raw.series.get("hybrid_charge", []) or raw.series.get("bio_charge", []), raw.day
        )
        limitations = list(sleep.limitations)
        if interpreted < 2:
            limitations.append("fewer_than_two_baseline_interpretable_signals")
        if readiness is not None:
            limitations.append("vendor_readiness_is_context_only")
        if charge is not None:
            limitations.append("vendor_charge_is_context_only")
        return RecoveryFeatures(
            status=status,
            status_label=AVAILABILITY_LABELS[status.value],
            state=state,
            state_label=RECOVERY_LABELS[state.value],
            positive_signals=positive,
            positive_signal_labels=labels(positive, DRIVER_LABELS),
            negative_signals=negative,
            negative_signal_labels=labels(negative, DRIVER_LABELS),
            vendor_readiness=readiness,
            vendor_charge=charge,
            limitations=limitations,
            limitation_labels=labels(limitations, LIMITATION_LABELS),
        )


def build_states(sleep: SleepState, recovery: RecoveryFeatures, training: TrainingFeatures) -> ProfileStates:
    return ProfileStates(
        sleep=sleep,
        recovery=recovery.state,
        training_load=training.load_state,
        sleep_label=SLEEP_LABELS[sleep.value],
        recovery_label=RECOVERY_LABELS[recovery.state.value],
        training_load_label=LOAD_LABELS[training.load_state.value],
    )


def _baseline_for(
    baselines: dict[str, list[BaselineStats]],
    metric: str,
    source: str,
    scope: str,
    device_id: str | None,
) -> BaselineStats | None:
    candidates = [
        item for item in baselines.get(metric, [])
        if item.window_days == 28
        and item.source == source
        and item.source_scope == scope
        and item.device_id == device_id
    ]
    return candidates[0] if candidates else None


def _current_median(points: list[SeriesPoint], day: date) -> float | None:
    values = [point.value for point in points if point.day == day]
    return round(float(median(values)), 2) if values else None


def _daily_metric_with_deviation(
    raw: RawDailyProfile,
    baselines: dict[str, list[BaselineStats]],
    metric: str,
) -> tuple[float | None, Deviation | None]:
    candidates = []
    for stream, value in daily_stream_values(raw.series.get(metric, []), raw.day).items():
        baseline = _baseline_for(baselines, metric, *stream)
        candidates.append((
            int(bool(baseline and baseline.status == Availability.AVAILABLE)),
            baseline.distinct_days if baseline else 0,
            int(bool(stream[2])),
            stream,
            float(value),
            baseline,
        ))
    if not candidates:
        return None, None
    _, _, _, _, value, baseline = max(candidates, key=lambda item: item[:3])
    deviation = BaselineEngine.deviation(value, baseline) if baseline else None
    if deviation and deviation.direction == "unknown":
        deviation = None
    return value, deviation


def _same_stream_daily_values(
    points: list[SeriesPoint],
    target: date,
    stream: tuple[str, str, str | None],
    days: int,
) -> list[float]:
    grouped: dict[date, list[float]] = defaultdict(list)
    for point in points:
        if (
            target - timedelta(days=days - 1) <= point.day <= target
            and (point.source, point.source_scope, point.device_id) == stream
        ):
            grouped[point.day].append(point.value)
    return [float(median(grouped[day])) for day in sorted(grouped)]


def _sleep_window_metric_values(
    raw: RawDailyProfile,
    metric: str,
    device_id: str | None,
) -> list[float]:
    record = raw.sleep_by_day.get(raw.day)
    window = (
        local_sleep_window(
            raw.day, record.get("bedtime"), record.get("wake_time")
        )
        if record else None
    )
    if window is None:
        return []
    start, end = window
    return [
        float(point.value)
        for point in raw.series.get(metric, [])
        if isinstance(point.observed_at, datetime)
        and point.device_id == device_id
        and start <= utc_to_local(point.observed_at) <= end
        and 50 <= point.value <= 100
    ]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _clock_minutes(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        hours, minutes, *_ = value.split(":")
        result = int(hours) * 60 + int(minutes)
    else:
        result = value.hour * 60 + value.minute
    return result - 1440 if result >= 12 * 60 else result
