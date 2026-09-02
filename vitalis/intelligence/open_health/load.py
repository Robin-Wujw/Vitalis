"""Pure Banister TRIMP and training-load calculations for Open Health 1.0."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from math import exp
from statistics import median
from typing import Any, Iterable, Mapping

from vitalis.intelligence.contracts import (
    ConfidenceBand,
    OpenHealthInsights,
    OpenHealthProvenance,
    OpenHealthRefusalReason,
    OpenHealthStatus,
    ProfileSource,
    Sex,
    TrainingLoadDailyPoint,
    TrainingLoadInsight,
    UserProfile,
    WorkoutTrimpInsight,
)
from vitalis.time import local_day


ALGORITHM_REVISION = "banister-trimp-v1"
MODULE = "vitalis.intelligence.open_health.load"
HR_MIN_BPM = 25.0
HR_MAX_BPM = 240.0
MIN_SAMPLE_COUNT = 20
MIN_SPAN_SECONDS = 600.0
MIN_CREDITED_SECONDS = 600.0
MIN_CREDITED_FRACTION = 0.70
WINDOW_DAYS = 42
MIN_HISTORY_DAYS = 14


@dataclass(frozen=True)
class HeartRatePoint:
    timestamp: datetime
    value: float
    source: str = ""
    source_scope: str = "workout_detail"
    device_id: str | None = None
    unit: str = "bpm"


@dataclass(frozen=True)
class PauseInterval:
    started_at: datetime
    duration_seconds: float


@dataclass(frozen=True)
class LoadWorkout:
    source: str
    workout_id: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_minutes: float | None
    heart_rate: tuple[HeartRatePoint, ...] = field(default_factory=tuple)
    pauses: tuple[PauseInterval, ...] = field(default_factory=tuple)
    rhr_bpm: float | None = None


# Names used by callers that prefer explicit input terminology.
TrainingLoadWorkout = LoadWorkout
TrainingLoadHeartRatePoint = HeartRatePoint
TrainingLoadPause = PauseInterval


class LoadWorkoutBatch(list[LoadWorkout]):
    """Bounded workout inputs with explicit truncation metadata."""

    def __init__(self, values: Iterable[LoadWorkout] = (), *, truncated: bool = False):
        super().__init__(values)
        self.truncated = truncated


@dataclass(frozen=True)
class _Score:
    insight: WorkoutTrimpInsight
    reason: OpenHealthRefusalReason | None = None


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            seconds = float(value) / (1000.0 if abs(float(value)) > 10_000_000_000 else 1.0)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _field(profile: Any, name: str) -> Any:
    if profile is None:
        return None
    value = profile.get(name) if isinstance(profile, Mapping) else getattr(profile, name, None)
    return value


def _field_value(profile: Any, name: str) -> Any:
    value = _field(profile, name)
    if isinstance(value, Mapping):
        return value.get("value")
    return getattr(value, "value", value)


def _field_source(profile: Any, name: str) -> Any:
    value = _field(profile, name)
    if isinstance(value, Mapping):
        return value.get("source")
    return getattr(value, "source", None)


def _profile_revision(profile: Any) -> int:
    value = _field(profile, "revision")
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _confirmed_profile(profile: Any) -> tuple[float, int] | OpenHealthRefusalReason:
    sex = _field_value(profile, "sex")
    sex_source = _field_source(profile, "sex")
    hrmax = _field_value(profile, "confirmed_hrmax_bpm")
    hrmax_source = _field_source(profile, "confirmed_hrmax_bpm")
    if sex is None or hrmax is None:
        missing = []
        if sex is None:
            missing.append("UserProfile.sex")
        if hrmax is None:
            missing.append("UserProfile.confirmed_hrmax_bpm")
        return OpenHealthRefusalReason(
            code="MISSING_LOAD_PROFILE",
            detail="TRIMP v1 需要用户确认的男性性别和最大心率。",
            missing_inputs=missing,
        )
    if str(getattr(sex, "value", sex)) != Sex.MALE.value:
        return OpenHealthRefusalReason(
            code="UNSUPPORTED_LOAD_PROFILE",
            detail="TRIMP v1 仅支持 UserProfile.sex=MALE。",
            missing_inputs=["UserProfile.sex=MALE"],
        )
    if str(getattr(sex_source, "value", sex_source)) != ProfileSource.USER_CONFIRMED.value:
        return OpenHealthRefusalReason(
            code="UNCONFIRMED_LOAD_PROFILE",
            detail="TRIMP v1 只接受 source=USER_CONFIRMED 的 profile 字段。",
            missing_inputs=["UserProfile.sex.source=USER_CONFIRMED"],
        )
    if str(getattr(hrmax_source, "value", hrmax_source)) != ProfileSource.USER_CONFIRMED.value:
        return OpenHealthRefusalReason(
            code="UNCONFIRMED_LOAD_PROFILE",
            detail="TRIMP v1 只接受 source=USER_CONFIRMED 的 profile 字段。",
            missing_inputs=["UserProfile.confirmed_hrmax_bpm.source=USER_CONFIRMED"],
        )
    try:
        value = float(hrmax)
    except (TypeError, ValueError):
        value = 0.0
    if not 100.0 <= value <= 240.0:
        return OpenHealthRefusalReason(
            code="INVALID_LOAD_PROFILE",
            detail="confirmed_hrmax_bpm 必须在 100..240 bpm。",
            missing_inputs=["UserProfile.confirmed_hrmax_bpm=100..240"],
        )
    return value, _profile_revision(profile)


def _coerce_point(item: Any, workout_source: str) -> HeartRatePoint | None:
    timestamp = _datetime(_value(item, "timestamp", _value(item, "time")))
    raw_value = _value(item, "value", _value(item, "heart_rate", _value(item, "hr")))
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    source = _value(item, "source", None) or workout_source
    return HeartRatePoint(
        timestamp=timestamp,
        value=value,
        source=str(source),
        source_scope=str(_value(item, "source_scope", "workout_detail") or "workout_detail"),
        device_id=_value(item, "device_id", None) or None,
        unit=str(_value(item, "unit", "bpm") or "bpm"),
    ) if timestamp else None


def _coerce_pause(item: Any) -> PauseInterval | None:
    started = _datetime(_value(item, "started_at", _value(item, "start")))
    try:
        duration = float(_value(item, "duration_seconds", _value(item, "duration", 0)) or 0)
    except (TypeError, ValueError):
        return None
    if started is None or duration <= 0:
        return None
    return PauseInterval(started, duration)


def _coerce_workout(item: Any) -> LoadWorkout:
    if isinstance(item, LoadWorkout):
        return item
    data = _value(item, "data", {})
    detail = _value(item, "detail", {})
    if not isinstance(data, Mapping):
        data = {}
    if not isinstance(detail, Mapping):
        detail = {}
    source = str(_value(item, "source", None) or data.get("source") or "unknown")
    workout_id = str(_value(item, "workout_id", None) or data.get("workout_id") or "")
    started = _datetime(_value(item, "started_at", _value(item, "started", data.get("started_at", data.get("started")))))
    ended = _datetime(_value(item, "ended_at", _value(item, "ended", data.get("ended_at", data.get("ended")))))
    raw_duration = _value(item, "duration_minutes", None)
    if raw_duration is None:
        raw_duration = _value(item, "duration", data.get("duration"))
    duration_in_seconds = _value(item, "duration_seconds", data.get("duration_seconds"))
    try:
        duration = float(raw_duration) if raw_duration is not None else None
        if raw_duration is None and duration_in_seconds is not None:
            duration = float(duration_in_seconds) / 60.0
    except (TypeError, ValueError):
        duration = None
    raw_samples = (
        _value(item, "heart_rate", None)
        or _value(item, "heart_rate_samples", None)
        or _value(item, "samples", None)
        or _value(item, "hr_stream", None)
        or []
    )
    samples = tuple(
        point for point in (_coerce_point(row, source) for row in raw_samples)
        if point is not None
    )
    raw_pauses = _value(item, "pauses", None) or detail.get("pauses", []) or []
    pauses = tuple(
        pause for pause in (_coerce_pause(row) for row in raw_pauses)
        if pause is not None
    )
    raw_rhr = _value(item, "rhr_bpm", None)
    try:
        rhr = float(raw_rhr) if raw_rhr is not None else None
    except (TypeError, ValueError):
        rhr = None
    return LoadWorkout(source, workout_id, started, ended, duration, samples, pauses, rhr)


def _utc_seconds(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).timestamp()


def _pause_overlap_seconds(pauses: tuple[PauseInterval, ...], start: datetime, end: datetime) -> float:
    start_seconds, end_seconds = _utc_seconds(start), _utc_seconds(end)
    intervals = sorted(
        (
            max(start_seconds, _utc_seconds(pause.started_at)),
            min(end_seconds, _utc_seconds(pause.started_at) + pause.duration_seconds),
        )
        for pause in pauses
        if min(end_seconds, _utc_seconds(pause.started_at) + pause.duration_seconds)
        > max(start_seconds, _utc_seconds(pause.started_at))
    )
    merged: list[list[float]] = []
    for left, right in intervals:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return sum(right - left for left, right in merged)


def _in_pause(pauses: tuple[PauseInterval, ...], timestamp: datetime) -> bool:
    value = _utc_seconds(timestamp)
    return any(
        _utc_seconds(pause.started_at) <= value < _utc_seconds(pause.started_at) + pause.duration_seconds
        for pause in pauses
    )


def _insight_base(workout: LoadWorkout, hrmax: float | None = None) -> dict[str, Any]:
    day = local_day(workout.started_at) if workout.started_at else date.min
    return dict(
        workout_id=workout.workout_id,
        source=workout.source,
        date=day,
        hrmax_bpm=hrmax,
    )


def _score(workout: LoadWorkout, hrmax: float, rhr: float | None) -> _Score:
    base = _insight_base(workout, hrmax)
    if workout.started_at is None or workout.ended_at is None or workout.duration_minutes is None:
        return _Score(
            WorkoutTrimpInsight(**base),
            OpenHealthRefusalReason(
                code="MISSING_WORKOUT_TIMING",
                detail="TRIMP 需要 workout 的 started_at、ended_at 和 duration。",
                missing_inputs=["started_at", "ended_at", "duration"],
            ),
        )
    if workout.ended_at <= workout.started_at or workout.duration_minutes <= 0:
        return _Score(
            WorkoutTrimpInsight(**base),
            OpenHealthRefusalReason(
                code="INVALID_WORKOUT_TIMING",
                detail="训练结束时间必须晚于开始时间，duration 必须为正。",
                missing_inputs=["valid_workout_interval"],
            ),
        )
    if rhr is None or not 30.0 <= float(rhr) < hrmax or hrmax - float(rhr) < 20.0:
        return _Score(
            WorkoutTrimpInsight(**base),
            OpenHealthRefusalReason(
                code="MISSING_SAME_DAY_RHR",
                detail="TRIMP 需要同一自然日 30 bpm 以上的有效静息心率，且 HR reserve 至少 20 bpm。",
                missing_inputs=["same_day_rhr_bpm"],
            ),
        )

    interval_seconds = max((workout.ended_at - workout.started_at).total_seconds(), 0.0)
    declared_seconds = workout.duration_minutes * 60.0
    active_seconds = max(
        0.0,
        declared_seconds - _pause_overlap_seconds(workout.pauses, workout.started_at, workout.ended_at),
    )
    if active_seconds <= 0:
        return _Score(WorkoutTrimpInsight(**base), OpenHealthRefusalReason(
            code="INVALID_ACTIVE_DURATION",
            detail="暂停覆盖了整个训练时长，无法得到有效 active duration。",
            missing_inputs=["active_duration"],
        ))

    streams: dict[tuple[str, str, str | None, str], list[HeartRatePoint]] = {}
    for point in workout.heart_rate:
        if point.source != workout.source:
            continue
        if not workout.started_at <= point.timestamp <= workout.ended_at:
            continue
        if not HR_MIN_BPM <= point.value <= HR_MAX_BPM:
            continue
        if _in_pause(workout.pauses, point.timestamp):
            continue
        key = (point.source, point.source_scope, point.device_id, point.unit)
        streams.setdefault(key, []).append(point)
    candidates = []
    for key, stream_points in streams.items():
        sorted_points = sorted(stream_points, key=lambda point: point.timestamp)
        if (
            len(sorted_points) >= MIN_SAMPLE_COUNT
            and (sorted_points[-1].timestamp - sorted_points[0].timestamp).total_seconds()
            >= MIN_SPAN_SECONDS
        ):
            candidates.append((key, sorted_points))
    if not candidates:
        return _Score(
            WorkoutTrimpInsight(**base, active_duration_minutes=active_seconds / 60.0),
            OpenHealthRefusalReason(
                code="INSUFFICIENT_HEART_RATE_COVERAGE",
                detail="单一 source/device 心率流至少需要 20 点且跨度至少 600 秒。",
                missing_inputs=["heart_rate_points>=20", "heart_rate_span_seconds>=600"],
            ),
        )
    key, points = max(
        candidates,
        key=lambda item: (len(item[1]), (item[1][-1].timestamp - item[1][0].timestamp).total_seconds(), tuple(str(value or "") for value in item[0])),
    )
    cadence = median(
        (right.timestamp - left.timestamp).total_seconds()
        for left, right in zip(points, points[1:])
        if right.timestamp > left.timestamp
    )
    if cadence <= 0:
        return _Score(WorkoutTrimpInsight(**base), OpenHealthRefusalReason(
            code="INVALID_HEART_RATE_CADENCE",
            detail="心率相邻时间点间隔无效。",
            missing_inputs=["heart_rate_cadence"],
        ))
    credited_seconds = 0.0
    trimp = 0.0
    clamped = 0
    denominator = hrmax - float(rhr)
    remaining_active_seconds = active_seconds
    for index, point in enumerate(points):
        if index + 1 < len(points):
            next_time = points[index + 1].timestamp
            seconds = min(max((next_time - point.timestamp).total_seconds(), 0.0), cadence)
        else:
            seconds = min(cadence, max((_utc_seconds(workout.ended_at) - _utc_seconds(point.timestamp)), 0.0))
        seconds = max(seconds - _pause_overlap_seconds(workout.pauses, point.timestamp, point.timestamp + timedelta(seconds=seconds)), 0.0)
        seconds = min(seconds, remaining_active_seconds)
        if seconds <= 0:
            continue
        hr = point.value
        if hr > hrmax:
            clamped += 1
            hr = hrmax
        x = min(max((hr - float(rhr)) / denominator, 0.0), 1.0)
        trimp += x * 0.64 * exp(1.92 * x) * (seconds / 60.0)
        credited_seconds += seconds
        remaining_active_seconds -= seconds
    coverage = credited_seconds / active_seconds if active_seconds else 0.0
    insight = WorkoutTrimpInsight(
        **base,
        trimp=trimp if credited_seconds >= MIN_CREDITED_SECONDS and coverage >= MIN_CREDITED_FRACTION else None,
        rhr_bpm=float(rhr),
        selected_source_scope=key[1],
        selected_device_id=key[2],
        sample_count=len(points),
        credited_minutes=credited_seconds / 60.0,
        active_duration_minutes=active_seconds / 60.0,
        coverage_ratio=min(max(coverage, 0.0), 1.0),
        clamped_high_hr_points=clamped,
    )
    if insight.trimp is None:
        return _Score(insight, OpenHealthRefusalReason(
            code="INSUFFICIENT_CREDITED_HEART_RATE",
            detail="有效心率 credited 时间必须至少 10 分钟且覆盖 active duration 的 70%。",
            missing_inputs=["credited_minutes>=10", "credited_active_fraction>=0.70"],
        ))
    return _Score(insight)


def _profile_refusal(
    profile: Any,
    target: date,
    *,
    algorithm: str,
    workout: LoadWorkout | None = None,
) -> OpenHealthInsights:
    reason = _confirmed_profile(profile)
    if isinstance(reason, tuple):
        raise TypeError("profile refusal expected")
    if workout is None:
        payload = TrainingLoadInsight(
            target_date=target,
            period_start=target - timedelta(days=WINDOW_DAYS - 1),
            period_end=target,
        )
    else:
        payload = WorkoutTrimpInsight(
            **_insight_base(workout),
        )
    return OpenHealthInsights(
        algorithm_id=algorithm,
        version="1.0",
        upstream_revision=ALGORITHM_REVISION,
        shadow_only=True,
        status=OpenHealthStatus.REFUSED,
        tier="refused",
        inputs_used=["Workout", "heart_rate", "same_day_rhr", "UserProfile.sex", "UserProfile.confirmed_hrmax_bpm"],
        coverage={},
        confidence=ConfidenceBand.NONE,
        refusal_reason=reason,
        provenance=[OpenHealthProvenance(source="unknown", module=MODULE, algorithm="banister_trimp", upstream_revision=ALGORITHM_REVISION)],
        profile_revision_used=_profile_revision(profile),
        payload=payload,
    )


def _rhr_for_day(mapping: Mapping[Any, Any] | None, day: date) -> float | None:
    if not mapping:
        return None
    value = mapping.get(day, mapping.get(day.isoformat()))
    if isinstance(value, (list, tuple)):
        # The caller must select one canonical source/device stream explicitly.
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def compute_workout_trimp(
    workout: LoadWorkout | Mapping[str, Any] | Any,
    profile: UserProfile | Mapping[str, Any] | Any,
    *,
    rhr_bpm: float | None = None,
) -> OpenHealthInsights:
    item = _coerce_workout(workout)
    target = local_day(item.started_at) if item.started_at else date.today()
    checked = _confirmed_profile(profile)
    if not isinstance(checked, tuple):
        return _profile_refusal(profile, target, algorithm="open_health.workout_trimp", workout=item)
    hrmax, revision = checked
    resting = rhr_bpm if rhr_bpm is not None else item.rhr_bpm
    result = _score(item, hrmax, resting)
    sources = [OpenHealthProvenance(
        source=item.source,
        source_scope=result.insight.selected_source_scope or "workout_detail",
        device_id=result.insight.selected_device_id,
        module=MODULE,
        algorithm="banister_trimp",
        upstream_revision=ALGORITHM_REVISION,
    )]
    return OpenHealthInsights(
        algorithm_id="open_health.workout_trimp",
        version="1.0",
        upstream_revision=ALGORITHM_REVISION,
        shadow_only=True,
        status=OpenHealthStatus.AVAILABLE if result.reason is None else OpenHealthStatus.REFUSED,
        tier="high" if result.reason is None else "refused",
        inputs_used=["Workout.started_at", "Workout.ended_at", "Workout.duration", "Workout.pauses", "heart_rate", "same_day_rhr"],
        coverage={"credited_minutes": result.insight.credited_minutes, "active_duration_minutes": result.insight.active_duration_minutes, "ratio": result.insight.coverage_ratio},
        confidence=1.0 if result.reason is None else ConfidenceBand.NONE,
        refusal_reason=result.reason,
        provenance=sources,
        profile_revision_used=revision,
        payload=result.insight,
    )


def compute_training_load(
    workouts: Iterable[LoadWorkout | Mapping[str, Any] | Any],
    profile: UserProfile | Mapping[str, Any] | Any,
    *,
    target_date: date | None = None,
    period_start: date | None = None,
    queried_days: Iterable[date] | None = None,
    rhr_by_day: Mapping[Any, Any] | None = None,
    resting_hr_by_day: Mapping[Any, Any] | None = None,
    upstream_coverage_verified: bool = False,
    input_truncated: bool = False,
) -> OpenHealthInsights:
    items = [_coerce_workout(item) for item in workouts]
    known_dates = {
        local_day(item.started_at) for item in items if item.started_at is not None
    }
    target = target_date or (max(known_dates) if known_dates else date.today())
    default_start = target - timedelta(days=WINDOW_DAYS - 1)
    start = max(period_start, default_start) if period_start is not None else default_start
    end = target
    if start > end:
        raise ValueError("period_start 不能晚于 target_date")
    checked = _confirmed_profile(profile)
    if not isinstance(checked, tuple):
        return _profile_refusal(profile, target, algorithm="open_health.training_load")
    hrmax, revision = checked
    query_days = set(queried_days) if queried_days is not None else set(known_dates)
    query_days = {day for day in query_days if start <= day <= end}
    history_days = len(query_days)
    daily: list[TrainingLoadDailyPoint] = []
    scored: list[WorkoutTrimpInsight] = []
    unknown_dates: list[date] = []
    workouts_by_day: dict[date, list[LoadWorkout]] = {}
    for item in items:
        if item.started_at is None:
            continue
        day = local_day(item.started_at)
        if start <= day <= end:
            workouts_by_day.setdefault(day, []).append(item)
    resting = rhr_by_day if rhr_by_day is not None else resting_hr_by_day
    reasons: list[OpenHealthRefusalReason] = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        day_workouts = workouts_by_day.get(day, [])
        if not day_workouts:
            status = "REST" if day in query_days else "UNKNOWN"
            daily.append(TrainingLoadDailyPoint(date=day, status=status, trimp=0.0 if status == "REST" else None))
            if status == "UNKNOWN":
                unknown_dates.append(day)
            continue
        day_values: list[float] = []
        day_unknown = False
        for item in day_workouts:
            result = _score(item, hrmax, item.rhr_bpm if item.rhr_bpm is not None else _rhr_for_day(resting, day))
            scored.append(result.insight)
            if result.reason is not None:
                day_unknown = True
                reasons.append(result.reason)
            elif result.insight.trimp is not None:
                day_values.append(result.insight.trimp)
        if day_unknown or len(day_values) != len(day_workouts):
            daily.append(TrainingLoadDailyPoint(
                date=day,
                status="UNKNOWN",
                trimp=None,
                workout_count=len(day_workouts),
                scored_workout_count=len(day_values),
                unknown_workout_count=len(day_workouts) - len(day_values),
            ))
            unknown_dates.append(day)
        else:
            daily.append(TrainingLoadDailyPoint(
                date=day,
                status="SCORED",
                trimp=sum(day_values),
                workout_count=len(day_workouts),
                scored_workout_count=len(day_values),
            ))
    total_days = len(daily)
    covered_days = sum(point.status in {"REST", "SCORED"} for point in daily)
    coverage = covered_days / total_days if total_days else 0.0
    training_days = sum(point.workout_count > 0 for point in daily)
    detailed_days = sum(point.status == "SCORED" for point in daily if point.workout_count > 0)
    detail_coverage = detailed_days / training_days if training_days else 1.0
    recent = daily[-7:]
    recent_coverage = sum(point.status in {"REST", "SCORED"} for point in recent) / len(recent) if recent else 0.0

    atl_values: list[float | None] = [None] * len(daily)
    ctl_values: list[float | None] = [None] * len(daily)
    tsb_values: list[float | None] = [None] * len(daily)
    atl_state: float | None = None
    ctl_state: float | None = None
    known_run: list[float] = []
    unknown_seen = False
    lambda_atl = 1.0 - exp(-1.0 / 7.0)
    lambda_ctl = 1.0 - exp(-1.0 / 42.0)
    for index, point in enumerate(daily):
        if point.status == "UNKNOWN" or unknown_seen:
            unknown_seen = True
            continue
        value = float(point.trimp or 0.0)
        known_run.append(value)
        if len(known_run) == 7:
            atl_state = sum(known_run) / 7.0
            ctl_state = sum(known_run) / 7.0
        elif len(known_run) > 7 and atl_state is not None and ctl_state is not None:
            atl_state = (1.0 - lambda_atl) * atl_state + lambda_atl * value
            ctl_state = (1.0 - lambda_ctl) * ctl_state + lambda_ctl * value
        if atl_state is not None and ctl_state is not None:
            atl_values[index] = atl_state
            ctl_values[index] = ctl_state
            tsb_values[index] = ctl_state - atl_state
    daily = [point.model_copy(update={"atl": atl_values[index], "ctl": ctl_values[index], "tsb": tsb_values[index], "lower_bound": coverage < 0.95 or recent_coverage < 0.90}) for index, point in enumerate(daily)]
    final_atl = atl_values[-1] if atl_values else None
    final_ctl = ctl_values[-1] if ctl_values else None
    final_tsb = tsb_values[-1] if tsb_values else None
    lower_bound = (
        not upstream_coverage_verified
        or input_truncated
        or coverage < 0.95
        or recent_coverage < 0.90
        or detail_coverage < 0.95
    )
    payload = TrainingLoadInsight(
        target_date=target,
        period_start=start,
        period_end=end,
        daily_points=daily[:WINDOW_DAYS],
        atl=final_atl,
        ctl=final_ctl,
        tsb=final_tsb,
        coverage_ratio=coverage,
        recent_7d_coverage_ratio=recent_coverage,
        detail_coverage_ratio=detail_coverage,
        unknown_dates=unknown_dates,
        workout_trimp=scored,
        lower_bound=lower_bound,
    )
    if history_days < MIN_HISTORY_DAYS:
        status = OpenHealthStatus.REFUSED
        tier = "refused"
        reason = OpenHealthRefusalReason(
            code="INSUFFICIENT_LOAD_HISTORY",
            detail="Training load 至少需要 14 个已查询自然日。",
            missing_inputs=["queried_history_days>=14"],
        )
    elif detail_coverage < 0.80 or coverage < 0.80:
        status = OpenHealthStatus.REFUSED
        tier = "refused"
        reason = OpenHealthRefusalReason(
            code="INSUFFICIENT_LOAD_COVERAGE",
            detail="训练详情覆盖率低于 80%，拒绝生成 load 状态。",
            missing_inputs=["detail_coverage>=0.80", "calendar_coverage>=0.80"],
        )
    else:
        complete_coverage = (
            upstream_coverage_verified
            and not input_truncated
            and coverage >= 0.95
            and detail_coverage >= 0.95
            and recent_coverage >= 0.90
        )
        status = OpenHealthStatus.AVAILABLE if complete_coverage else OpenHealthStatus.PARTIAL
        tier = (
            "high"
            if (end - start).days + 1 == WINDOW_DAYS and complete_coverage
            else "low"
        )
        reason = None
    sources = sorted({item.source for item in items if start <= (local_day(item.started_at) if item.started_at else date.min) <= end})
    provenance = [OpenHealthProvenance(source=source, module=MODULE, algorithm="banister_trimp", upstream_revision=ALGORITHM_REVISION) for source in sources] or [OpenHealthProvenance(source="unknown", module=MODULE, algorithm="banister_trimp", upstream_revision=ALGORITHM_REVISION)]
    return OpenHealthInsights(
        algorithm_id="open_health.training_load",
        version="1.0",
        upstream_revision=ALGORITHM_REVISION,
        shadow_only=True,
        status=status,
        tier=tier,
        inputs_used=["Workout.started_at", "Workout.ended_at", "Workout.duration", "Workout.pauses", "heart_rate", "same_day_rhr", "UserProfile.sex", "UserProfile.confirmed_hrmax_bpm"],
        coverage={"calendar_days": total_days, "covered_days": covered_days, "ratio": coverage, "detail_ratio": detail_coverage, "recent_7d_ratio": recent_coverage, "upstream_coverage_verified": upstream_coverage_verified, "input_truncated": input_truncated, "unknown_dates": [day.isoformat() for day in unknown_dates]},
        confidence=ConfidenceBand.HIGH if tier == "high" else ConfidenceBand.LOW if tier == "low" else ConfidenceBand.NONE,
        note=" ".join(filter(None, (
            None if upstream_coverage_verified else "上游同步覆盖尚未由持久 chunk 账本验证；负荷按本地记录给出下界估计。",
            "训练心率输入达到安全上限并被截断。" if input_truncated else None,
        ))) or None,
        refusal_reason=reason,
        provenance=provenance,
        profile_revision_used=revision,
        payload=payload,
    )


# Conventional aliases used by the other Open Health modules.
compute_load = compute_training_load
training_load_insight = compute_training_load
trimp_insight = compute_workout_trimp
