"""Deterministic running-session analysis over normalized workout facts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean, median

from .contracts import (
    Availability,
    ConfidenceBand,
    RunningAnalysis,
    ComparableRunBaseline,
    RunningHeartRateZone,
    RunningSegment,
    RunningSessionAnalysis,
)
from .localization import AVAILABILITY_LABELS, CONFIDENCE_LABELS


CLASSIFICATION_LABELS = {
    "RECOVERY_RUN": "恢复跑",
    "EASY_RUN": "轻松跑",
    "STEADY_RUN": "稳定跑",
    "TEMPO_RUN": "节奏或阈值跑",
    "INTERVAL_RUN": "间歇跑",
    "LONG_RUN": "长距离跑",
    "UNCLASSIFIED": "暂未分类",
}
ZONE_LABELS = {
    1: "一区：轻松",
    2: "二区：耐力",
    3: "三区：节奏",
    4: "四区：阈值",
    5: "五区：无氧",
}


class RunningAnalyzer:
    """Analyze running without population HR formulas or universal cadence targets."""

    def analyze(self, raw) -> RunningAnalysis:
        current_start = raw.day - timedelta(days=27)
        previous_start = raw.day - timedelta(days=55)
        current = self._runs(raw.workouts, current_start, raw.day)
        history = self._runs(raw.workouts, raw.day - timedelta(days=179), raw.day)
        previous = self._runs(
            raw.workouts, previous_start, current_start - timedelta(days=1)
        )
        threshold = self._lactate_threshold(raw)
        if not current:
            return RunningAnalysis(
                status=Availability.INSUFFICIENT_DATA,
                status_label=AVAILABILITY_LABELS[Availability.INSUFFICIENT_DATA.value],
                sessions_7d=0,
                duration_minutes_7d=0,
                sessions_28d=0,
                duration_minutes_28d=0,
                limitations=["近 28 天没有跑步记录。"],
            )

        analyses = []
        historical_durations: list[int] = []
        for workout in sorted(current, key=self._workout_date):
            prior_runs = [
                item for item in history
                if self._workout_sort_key(item) < self._workout_sort_key(workout)
            ]
            analysis = self._session(
                workout, threshold, historical_durations, prior_runs
            )
            analyses.append(analysis)
            historical_durations.append(analysis.duration_minutes)

        recent_7 = [
            item for item in current
            if self._workout_date(item) >= raw.day - timedelta(days=6)
        ]
        current_distance = self._total_distance(current)
        previous_distance = self._total_distance(previous)
        distance_change = None
        if current_distance is not None and previous_distance not in (None, 0):
            distance_change = round(
                (current_distance - previous_distance) / previous_distance * 100, 1
            )
        limitations = []
        if threshold is None:
            limitations.append("缺少个人乳酸阈心率，暂不计算跑步心率区间。")
        if current_distance is None:
            limitations.append("部分跑步缺少距离，28 天总距离不可计算。")
        if any(item.median_cadence_spm is None for item in analyses):
            limitations.append("部分跑步缺少步频明细。")
        type_counts: dict[str, int] = {}
        for item in analyses:
            type_counts[item.classification] = type_counts.get(item.classification, 0) + 1
        zone_sources = {
            item.heart_rate_zone_source
            for item in analyses
            if item.heart_rate_zone_source != "unavailable"
        }
        zone_method = (
            "mixed" if len(zone_sources) > 1
            else next(iter(zone_sources)) if zone_sources
            else "unavailable"
        )
        return RunningAnalysis(
            status=Availability.AVAILABLE,
            status_label=AVAILABILITY_LABELS[Availability.AVAILABLE.value],
            zone_method=zone_method,
            lactate_threshold_bpm=threshold,
            sessions_7d=len(recent_7),
            duration_minutes_7d=sum(self._duration(item) for item in recent_7),
            distance_km_7d=self._total_distance(recent_7),
            sessions_28d=len(current),
            duration_minutes_28d=sum(self._duration(item) for item in current),
            distance_km_28d=current_distance,
            distance_change_percent=distance_change,
            session_type_counts_28d=dict(sorted(type_counts.items())),
            recent_sessions=list(reversed(analyses[-8:])),
            limitations=limitations,
        )

    def _session(
        self,
        workout: dict,
        threshold: float | None,
        historical_durations: list[int],
        prior_runs: list[dict],
    ) -> RunningSessionAnalysis:
        data = workout.get("data") or {}
        samples = workout.get("samples") or []
        grouped = self._group_samples(samples)
        heart_rate = grouped.get("heart_rate", [])
        speed = [item for item in grouped.get("speed", []) if item.value > 0]
        cadence = grouped.get("cadence", [])
        power = grouped.get("running_power", [])
        equivalent_pace = grouped.get("equivalent_pace", [])
        ground_contact = grouped.get("ground_contact_time", [])
        vertical_oscillation = grouped.get("vertical_oscillation", [])
        vertical_ratio = grouped.get("vertical_stride_ratio", [])
        duration = self._duration(workout)
        distance = self._distance(workout)
        median_speed = self._median_value(speed)
        average_pace = (
            round(duration * 60 / distance, 1)
            if distance and distance > 0 and duration > 0
            else round(1000 / median_speed, 1) if median_speed else None
        )
        cadence_median = self._median_value(cadence)
        cadence_variability = None
        if cadence_median:
            cadence_mad = median(abs(item.value - cadence_median) for item in cadence)
            cadence_variability = round(cadence_mad / cadence_median * 100, 1)
        device_boundaries = self._device_zone_boundaries(workout)
        zones = self._zones(heart_rate, threshold, device_boundaries)
        zone_source = (
            "device_workout" if device_boundaries
            else "lactate_threshold" if threshold is not None and heart_rate
            else "unavailable"
        )
        segments = self._segments(speed, heart_rate)
        classification, confidence, evidence = self._classify(
            duration,
            zones,
            segments,
            speed,
            historical_durations,
        )
        drift, drift_limitation = self._cardiac_drift(
            speed, heart_rate, classification, confidence
        )
        limitations = []
        if threshold is None:
            limitations.append("本次缺少个人乳酸阈心率，未计算心率区间。")
        if not speed:
            limitations.append("本次缺少连续速度明细。")
        if not cadence:
            limitations.append("本次缺少步频明细。")
        if drift_limitation:
            limitations.append(drift_limitation)
        average_power = mean(item.value for item in power) if power else None
        comparable = self._comparable_baseline(
            workout,
            prior_runs,
            average_pace,
            (
                mean(item.value for item in heart_rate)
                if heart_rate else self._positive(data.get("heart_rate_avg"))
            ),
            average_power,
        )
        return RunningSessionAnalysis(
            workout_id=str(workout.get("workout_id") or ""),
            date=self._workout_date(workout),
            classification=classification,
            classification_label=CLASSIFICATION_LABELS[classification],
            confidence=confidence,
            confidence_label=CONFIDENCE_LABELS[confidence.value],
            duration_minutes=duration,
            distance_km=distance,
            average_pace_seconds_per_km=average_pace,
            median_speed_mps=round(median_speed, 3) if median_speed else None,
            median_cadence_spm=round(cadence_median, 1) if cadence_median else None,
            cadence_variability_percent=cadence_variability,
            average_power_watts=round(average_power, 1) if average_power is not None else None,
            median_equivalent_pace_seconds_per_km=(
                round(self._median_value(equivalent_pace), 1) if equivalent_pace else None
            ),
            median_ground_contact_time_ms=(
                round(self._median_value(ground_contact), 1) if ground_contact else None
            ),
            median_vertical_oscillation_mm=(
                round(self._median_value(vertical_oscillation), 1) if vertical_oscillation else None
            ),
            median_vertical_stride_ratio_percent=(
                round(self._median_value(vertical_ratio), 1) if vertical_ratio else None
            ),
            average_heart_rate_bpm=(
                round(sum(item.value for item in heart_rate) / len(heart_rate), 1)
                if heart_rate else self._positive(data.get("heart_rate_avg"))
            ),
            maximum_heart_rate_bpm=(
                max(item.value for item in heart_rate)
                if heart_rate else self._positive(data.get("heart_rate_max"))
            ),
            heart_rate_zone_source=zone_source,
            heart_rate_zone_boundaries_bpm=device_boundaries,
            heart_rate_zones=zones,
            cardiac_drift_percent=drift,
            comparable_baseline=comparable,
            segments=segments[:20],
            evidence=evidence,
            limitations=limitations,
        )

    @staticmethod
    def _runs(workouts: list[dict], start: date, end: date) -> list[dict]:
        return [
            item for item in workouts
            if start <= RunningAnalyzer._workout_date(item) <= end
            and str((item.get("data") or {}).get("type") or "").lower() == "running"
        ]

    @staticmethod
    def _workout_date(workout: dict) -> date:
        return workout.get("local_day") or date.min

    @staticmethod
    def _workout_sort_key(workout: dict) -> tuple[date, datetime]:
        started = workout.get("started_at")
        if not isinstance(started, datetime):
            started = datetime.min
        elif started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        return RunningAnalyzer._workout_date(workout), started

    @staticmethod
    def _duration(workout: dict) -> int:
        return max(int((workout.get("data") or {}).get("duration") or 0), 0)

    @staticmethod
    def _distance(workout: dict) -> float | None:
        value = (workout.get("data") or {}).get("distance_km")
        if value is None:
            return None
        value = float(value)
        return value if value >= 0 else None

    @staticmethod
    def _total_distance(workouts: list[dict]) -> float | None:
        if not workouts:
            return 0.0
        values = [RunningAnalyzer._distance(item) for item in workouts]
        if any(value is None for value in values):
            return None
        return round(sum(value or 0 for value in values), 3)

    @staticmethod
    def _lactate_threshold(raw) -> float | None:
        points = [
            item for item in raw.series.get("lactate_threshold_hr", [])
            if item.day <= raw.day and 60 <= item.value <= 240
        ]
        if not points:
            return None
        return float(max(points, key=lambda item: (item.day, item.observed_at)).value)

    @staticmethod
    def _group_samples(samples: list) -> dict[str, list]:
        output: dict[str, list] = defaultdict(list)
        for item in samples:
            output[item.metric].append(item)
        for values in output.values():
            values.sort(key=lambda item: item.timestamp)
        return output

    @staticmethod
    def _median_value(samples: list) -> float | None:
        return float(median(item.value for item in samples)) if samples else None

    @staticmethod
    def _zones(
        samples: list,
        threshold: float | None,
        device_boundaries: list[int] | None = None,
    ) -> list[RunningHeartRateZone]:
        if not samples or (threshold is None and not device_boundaries):
            return []
        cuts = (
            list(device_boundaries[1:5])
            if device_boundaries and len(device_boundaries) == 6
            else [round(float(threshold) * ratio) for ratio in (0.81, 0.88, 0.93, 0.99)]
        )
        bounds = (
            (1, None, cuts[0] - 1),
            (2, cuts[0], cuts[1] - 1),
            (3, cuts[1], cuts[2] - 1),
            (4, cuts[2], cuts[3] - 1),
            (5, cuts[3], None),
        )
        counts = {zone: 0 for zone in range(1, 6)}
        for sample in samples:
            zone = (
                1 if sample.value < cuts[0]
                else 2 if sample.value < cuts[1]
                else 3 if sample.value < cuts[2]
                else 4 if sample.value < cuts[3]
                else 5
            )
            counts[zone] += 1
        total = len(samples)
        return [
            RunningHeartRateZone(
                zone=zone,
                label=ZONE_LABELS[zone],
                lower_bpm=lower,
                upper_bpm=upper,
                duration_seconds=counts[zone],
                share_percent=round(counts[zone] / total * 100, 1),
            )
            for zone, lower, upper in bounds
        ]

    @staticmethod
    def _device_zone_boundaries(workout: dict) -> list[int]:
        values = (workout.get("data") or {}).get("heart_rate_zone_boundaries_bpm") or []
        if (
            isinstance(values, list)
            and len(values) == 6
            and all(isinstance(value, int) and 30 <= value <= 250 for value in values)
            and all(left < right for left, right in zip(values, values[1:]))
        ):
            return values
        return []

    def _comparable_baseline(
        self,
        workout: dict,
        prior_runs: list[dict],
        current_pace: float | None,
        current_hr: float | None,
        current_power: float | None,
    ) -> ComparableRunBaseline | None:
        distance = self._distance(workout)
        if not distance or not current_pace:
            return None
        comparable = [
            item for item in reversed(sorted(prior_runs, key=self._workout_sort_key))
            if (candidate_distance := self._distance(item)) is not None
            and distance * 0.8 <= candidate_distance <= distance * 1.2
            and self._workout_pace(item) is not None
        ][:10]
        if len(comparable) < 3:
            return None
        paces = [self._workout_pace(item) for item in comparable]
        baseline_pace = float(median(value for value in paces if value is not None))
        heart_rates = [self._workout_average_hr(item) for item in comparable]
        valid_heart_rates = [value for value in heart_rates if value is not None]
        baseline_hr = float(median(valid_heart_rates)) if len(valid_heart_rates) >= 3 else None
        powers = [self._workout_average_power(item) for item in comparable]
        valid_powers = [value for value in powers if value is not None]
        baseline_power = float(median(valid_powers)) if len(valid_powers) >= 3 else None
        return ComparableRunBaseline(
            sample_count=len(comparable),
            workout_ids=[str(item.get("workout_id") or "") for item in comparable],
            median_pace_seconds_per_km=round(baseline_pace, 1),
            pace_difference_percent=round((current_pace - baseline_pace) / baseline_pace * 100, 1),
            median_heart_rate_bpm=round(baseline_hr, 1) if baseline_hr is not None else None,
            heart_rate_difference_bpm=(
                round(current_hr - baseline_hr, 1)
                if current_hr is not None and baseline_hr is not None else None
            ),
            median_power_watts=round(baseline_power, 1) if baseline_power is not None else None,
            power_difference_percent=(
                round((current_power - baseline_power) / baseline_power * 100, 1)
                if current_power is not None and baseline_power not in (None, 0) else None
            ),
        )

    def _workout_pace(self, workout: dict) -> float | None:
        distance = self._distance(workout)
        duration = self._duration(workout)
        return duration * 60 / distance if distance and duration > 0 else None

    @staticmethod
    def _workout_average_hr(workout: dict) -> float | None:
        samples = [item.value for item in workout.get("samples", []) if item.metric == "heart_rate"]
        if samples:
            return float(mean(samples))
        return RunningAnalyzer._positive((workout.get("data") or {}).get("heart_rate_avg"))

    @staticmethod
    def _workout_average_power(workout: dict) -> float | None:
        values = [item.value for item in workout.get("samples", []) if item.metric == "running_power"]
        return float(mean(values)) if values else None

    def _segments(self, speed: list, heart_rate: list) -> list[RunningSegment]:
        speed_bins = self._bins(speed, 30)
        if len(speed_bins) < 10:
            return []
        values = sorted(value for value in speed_bins.values() if value > 0)
        if len(values) < 10:
            return []
        low = self._percentile(values, 0.30)
        high = self._percentile(values, 0.70)
        if low <= 0 or high < low * 1.15:
            return []
        labels = {}
        for bucket, value in speed_bins.items():
            if value >= high:
                labels[bucket] = "work"
            elif value <= low:
                labels[bucket] = "recovery"
        groups: list[tuple[str, int, int]] = []
        for bucket in sorted(labels):
            kind = labels[bucket]
            if groups and groups[-1][0] == kind and bucket == groups[-1][2] + 1:
                previous = groups[-1]
                groups[-1] = (kind, previous[1], bucket)
            else:
                groups.append((kind, bucket, bucket))
        hr_bins = self._bins(heart_rate, 30)
        output = []
        for kind, first, last in groups:
            duration = (last - first + 1) * 30
            if duration < 30:
                continue
            speeds = [speed_bins[key] for key in range(first, last + 1) if key in speed_bins]
            hrs = [hr_bins[key] for key in range(first, last + 1) if key in hr_bins]
            average_speed = sum(speeds) / len(speeds)
            output.append(RunningSegment(
                kind=kind,
                kind_label="快速段" if kind == "work" else "恢复段",
                start_offset_seconds=first * 30,
                end_offset_seconds=(last + 1) * 30,
                duration_seconds=duration,
                average_speed_mps=round(average_speed, 3),
                average_pace_seconds_per_km=round(1000 / average_speed, 1),
                average_heart_rate_bpm=round(sum(hrs) / len(hrs), 1) if hrs else None,
            ))
        return output

    def _classify(
        self,
        duration: int,
        zones: list[RunningHeartRateZone],
        segments: list[RunningSegment],
        speed: list,
        historical_durations: list[int],
    ) -> tuple[str, ConfidenceBand, list[str]]:
        work_segments = sum(item.kind == "work" for item in segments)
        recovery_segments = sum(item.kind == "recovery" for item in segments)
        zone_share = {item.zone: item.share_percent for item in zones}
        easy_share = zone_share.get(1, 0) + zone_share.get(2, 0)
        threshold_share = zone_share.get(4, 0) + zone_share.get(5, 0)
        if work_segments >= 3 and recovery_segments >= 2:
            return "INTERVAL_RUN", ConfidenceBand.HIGH, [
                f"识别到 {work_segments} 个快速段和 {recovery_segments} 个恢复段。"
            ]
        if historical_durations and duration >= 60 and duration >= median(historical_durations) * 1.3:
            return "LONG_RUN", ConfidenceBand.MODERATE, [
                "时长至少 60 分钟，且明显长于此前个人跑步时长中位数。"
            ]
        if zones and duration >= 20 and threshold_share >= 35:
            return "TEMPO_RUN", ConfidenceBand.HIGH, [
                f"阈值附近及以上心率占比 {threshold_share:.1f}%。"
            ]
        if zones and duration <= 35 and easy_share >= 85:
            return "RECOVERY_RUN", ConfidenceBand.HIGH, [
                f"一区和二区心率占比 {easy_share:.1f}%，且时长不超过 35 分钟。"
            ]
        if zones and easy_share >= 75:
            return "EASY_RUN", ConfidenceBand.HIGH, [
                f"一区和二区心率占比 {easy_share:.1f}%。"
            ]
        if speed:
            return "STEADY_RUN", ConfidenceBand.LOW, [
                "存在连续速度明细，但缺少足够证据归入专项课型。"
            ]
        return "UNCLASSIFIED", ConfidenceBand.NONE, ["跑步明细不足，未判断课型。"]

    def _cardiac_drift(
        self,
        speed: list,
        heart_rate: list,
        classification: str,
        confidence: ConfidenceBand,
    ) -> tuple[float | None, str | None]:
        if (
            classification not in {"RECOVERY_RUN", "EASY_RUN"}
            or confidence not in {ConfidenceBand.MODERATE, ConfidenceBand.HIGH}
        ):
            return None, "本次不是明确识别的连续低强度跑，不解释心率漂移。"
        speed_bins = self._bins(speed, 30)
        hr_bins = self._bins(heart_rate, 30)
        common = [key for key in sorted(speed_bins) if key in hr_bins and speed_bins[key] > 0]
        if len(common) < 40:
            return None, "连续速度与心率重叠不足 20 分钟，未计算心率漂移。"
        common = common[max(1, len(common) // 10):]
        midpoint = len(common) // 2
        first, second = common[:midpoint], common[midpoint:]
        first_speed = median(speed_bins[key] for key in first)
        second_speed = median(speed_bins[key] for key in second)
        if abs(second_speed - first_speed) / first_speed > 0.15:
            return None, "前后半程速度差异超过 15%，不适合解释心率漂移。"
        first_ratio = median(hr_bins[key] for key in first) / first_speed
        second_ratio = median(hr_bins[key] for key in second) / second_speed
        return round((second_ratio / first_ratio - 1) * 100, 1), None

    @staticmethod
    def _bins(samples: list, seconds: int) -> dict[int, float]:
        if not samples:
            return {}
        start = min(item.timestamp for item in samples)
        grouped: dict[int, list[float]] = defaultdict(list)
        for item in samples:
            elapsed = int((item.timestamp - start).total_seconds())
            if elapsed >= 0:
                grouped[elapsed // seconds].append(item.value)
        return {key: float(median(values)) for key, values in grouped.items()}

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        index = int(round((len(values) - 1) * quantile))
        return values[index]

    @staticmethod
    def _positive(value) -> float | None:
        return float(value) if isinstance(value, (int, float)) and value > 0 else None
