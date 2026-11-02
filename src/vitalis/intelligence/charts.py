"""Deterministic chart projections; no database, network, or renderer arithmetic.

Series are never blended across source, scope, device, or unit. Empty time
buckets stay absent/null and vendor scores are explicitly reference values.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from math import isfinite
from statistics import median
from zoneinfo import ZoneInfo

from .chart_contracts import (
    Chart, ChartBand, ChartBandPoint, ChartBundle, ChartInterval, ChartPoint,
    ChartReference, ChartRegion, ChartSeries,
)
from .profile import RawDailyProfile, SeriesPoint

UTC = timezone.utc
MAX_SERIES = 16
MAX_WORKOUT_CHARTS = 12
INTRADAY = {
    "heart_rate": ("每分钟心率", "bpm", "line"),
    "stress": ("压力采样", "score", "scatter"),
    "hrv_rmssd": ("HRV · RMSSD", "ms", "scatter"),
    "respiratory_rate": ("呼吸频率采样", "brpm", "scatter"),
}
DAILY = {
    "sleep_duration": ("每日主睡眠时长", "min", "bars"),
    "sleep_score": ("Zepp 睡眠评分", "score", "line"),
    "nap_minutes": ("每日午睡时长", "min", "bars"),
    "readiness": ("Zepp 准备状态", "score", "line"),
    "sleep_hrv": ("夜间 HRV", "ms", "line"),
    "hrv_rmssd": ("每日 RMSSD 记录中位数", "ms", "line"),
    "hrv_sdnn": ("每日 SDNN 记录中位数", "ms", "line"),
    "resting_hr": ("静息心率", "bpm", "line"),
    "sleep_rhr": ("Zepp 夜间静息心率", "bpm", "line"),
    "skin_temp_delta": ("皮肤温度偏差", "C", "line"),
    "respiratory_rate": ("每日呼吸频率", "brpm", "line"),
    "vo2max": ("Zepp 最大摄氧量估计", "ml/kg/min", "line"),
    "steps": ("每日步数", "steps", "bars"),
    "training_load": ("每日训练负荷", "load", "bars"),
    "stress": ("每日压力记录", "score", "line"),
    "hybrid_charge": ("每日 Charge 记录中位数", "score", "line"),
}
CHARGE = {"hybrid_charge": "总分", "physical_charge": "身体", "mental_charge": "心理"}
VENDOR = {"readiness", "sleep_score", "vo2max", "stress", *CHARGE, "vendor_load_7d"}


def _datetime(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else None


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _bounds(start: date, end: date, zone: str) -> tuple[datetime, datetime]:
    local = ZoneInfo(zone)
    return (datetime.combine(start, time.min, local).astimezone(UTC),
            datetime.combine(end + timedelta(days=1), time.min, local).astimezone(UTC))


def _identity(point) -> tuple:
    return (str(_get(point, "source", "unknown")), str(_get(point, "source_scope", "unknown")),
            _get(point, "device_id"), str(_get(point, "unit", "")))


def _key(metric: str, identity: tuple) -> str:
    return metric + ":" + sha256(repr(identity).encode()).hexdigest()[:12]


def _resolution(seconds: float, *, preview: bool = False) -> int:
    target = 16 if preview else 1500
    ladder = (60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400)
    return next((step for step in ladder if seconds / step <= target), 86400)


def _series(metric: str, label: str, points: list, start: datetime, end: datetime,
            cutoff: datetime, resolution: int) -> tuple[list[ChartSeries], bool]:
    groups = defaultdict(list)
    for point in points:
        stamp = _datetime(_get(point, "observed_at", _get(point, "timestamp")))
        value = _number(_get(point, "value"))
        if value is None or stamp is None or not start <= stamp < end or stamp > cutoff:
            continue
        if _get(point, "is_daily_feature", False) or _get(point, "is_daily_summary", False):
            continue
        groups[_identity(point)].append((stamp, value, point))
    output = []
    for identity, records in sorted(groups.items(), key=lambda p: str(p[0]))[:MAX_SERIES]:
        buckets = defaultdict(list)
        for stamp, value, _ in records:
            epoch = max(start.timestamp(), int(stamp.timestamp()) // resolution * resolution)
            buckets[epoch].append(value)
        timeline = []
        previous = None
        for epoch, values in sorted(buckets.items()):
            if previous is not None and epoch - previous > resolution:
                timeline.append(ChartPoint(timestamp=datetime.fromtimestamp(previous + resolution, UTC)))
            timeline.append(ChartPoint(timestamp=datetime.fromtimestamp(epoch, UTC), value=round(median(values), 3),
                                       sample_count=len(values), minimum=min(values), maximum=max(values)))
            previous = epoch
        source, scope, device, unit = identity
        source_field = _get(records[0][2], "source_field")
        output.append(ChartSeries(
            key=_key(metric, identity), label=f"{label} · {source}" + (f" · {device}" if device else ""),
            unit=unit, source=source, source_scope=scope, device_id=device, source_field=source_field,
            qualification=_get(records[0][2], "qualification") or ("vendor_reference" if metric in VENDOR else "observed"),
            fetched_at=max((stamp for _,_,p in records if (stamp := _datetime(_get(p,"fetched_at"))) is not None), default=None),
            aggregation="median", points=timeline, sample_count=len(records),
            observed_minutes=len({int(stamp.timestamp()) // 60 for stamp, _, _ in records}),
            first_observed_at=min(r[0] for r in records), last_observed_at=max(r[0] for r in records),
        ))
    return output, len(groups) > MAX_SERIES


def _annotations(raw: RawDailyProfile, start: datetime, end: datetime) -> list[ChartInterval]:
    output = []
    for row in getattr(raw, "chart_inputs", {}).get("annotations", []):
        created = _datetime(row.get("created_at"))
        if created and created > raw.as_of:
            continue
        left = _datetime(row.get("start") or row.get("started_at"))
        right = _datetime(row.get("end") or row.get("ended_at"))
        if left is None or right is None or right <= start or left >= end or left > raw.as_of:
            continue
        clipped_end = min(right, end, raw.as_of)
        if clipped_end <= max(left, start):
            continue
        output.append(ChartInterval(start=max(left, start), end=clipped_end, category="annotation",
            label=str(row.get("text") or "用户注释"), source="user", source_scope="timeline_annotation",
            qualification="user_reported"))
    return output[:300]


def _intraday(raw: RawDailyProfile, start: date, end: date) -> list[Chart]:
    zone = raw.timezone_name or "UTC"
    left, right = _bounds(start, end, zone)
    cutoff = _datetime(raw.as_of)
    resolution = _resolution((right-left).total_seconds())
    preview = _resolution((right-left).total_seconds(), preview=True)
    samples = getattr(raw, "chart_samples", {})
    charts = []
    for metric, (label, unit, kind) in INTRADAY.items():
        points = samples.get(metric, [])
        if not points and metric != "heart_rate":
            points = raw.series.get(metric, [])
        series, truncated = _series(metric, label, points, left, right, cutoff, resolution)
        if not series:
            continue
        compact, _ = _series(metric, label, points, left, right, cutoff, preview)
        notes = ["缺测区间留白；时间轴按实际时间间隔绘制。", "图中数值为有记录时段的分桶中位数，不代表连续监测。"]
        if metric == "stress":
            notes.append("压力来自 all_day_stress 的稀疏观测；不连接空白、不使用 Charge 内部压力模型。")
        charts.append(Chart(key=metric, title=label, kind=kind, unit=unit, timezone=zone,
            period_start=left, period_end=right, series=series, preview_series=compact,
            resolution_seconds=resolution, state="partial", truncated=truncated,
            intervals=_annotations(raw, left, right), notes=notes,
            summary=[f"{s.label}：{s.sample_count} 条观测，{s.observed_minutes} 个有记录的分钟" for s in series]))
    series, compact = [], []
    truncated = False
    for metric, label in CHARGE.items():
        points = samples.get(metric, []) or raw.series.get(metric, [])
        current, clipped = _series(metric, label, points, left, right, cutoff, resolution)
        reduced, _ = _series(metric, label, points, left, right, cutoff, preview)
        series.extend(current)
        compact.extend(reduced)
        truncated |= clipped
    if series:
        charts.append(Chart(key="charge", title="Zepp BioCharge · 总分、身体与心理", kind="line", unit="score",
            timezone=zone, period_start=left, period_end=right, series=series, preview_series=compact,
            resolution_seconds=resolution, state="partial", truncated=truncated,
            intervals=_annotations(raw, left, right),
            notes=["三个分项独立保留；总分缺失不删除有效分项。", "Zepp 厂商参考值，不替代 Vitalis 准备度或证明身心恢复。"],
            summary=[f"{s.label}：首个有记录时段 {next(p.value for p in s.points if p.value is not None):g}，末个有记录时段 {next(p.value for p in reversed(s.points) if p.value is not None):g}" for s in series]))
    return charts


def _sleep(raw: RawDailyProfile, start: date, end: date) -> Chart | None:
    zone = raw.timezone_name or "UTC"
    left, right = _bounds(start, end, zone)
    intervals, summary = [], []
    for day, row in sorted(raw.sleep_by_day.items()):
        if not start <= day <= end:
            continue
        duration = _number(row.get("sleep_duration"))
        score = _number(row.get("sleep_score"))
        summary.append(f"{day} 主睡眠" + (f" {duration:g} 分钟" if duration is not None else "") +
                       (f" · Zepp 评分 {score:g}" if score is not None else ""))
        for stage in row.get("stages") or []:
            a, b = _datetime(_get(stage, "start_time")), _datetime(_get(stage, "end_time"))
            if not a or not b or b <= a or a > raw.as_of:
                continue
            category = _get(stage, "stage", "unknown")
            intervals.append(ChartInterval(start=a, end=min(b, raw.as_of), category=category,
                label=f"{day} 主睡眠 · {category}", source=row.get("source", "zepp"),
                source_scope="sleep_stage", qualification="vendor_estimate"))
    naps = getattr(raw, "chart_naps", {})
    for day, sessions in sorted(naps.items()):
        if not start <= day <= end:
            continue
        if not sessions:
            summary.append(f"{day} 来源明确没有午睡记录")
        for nap in sessions:
            duration = _number(_get(nap, "sleep_minutes"))
            summary.append(f"{day} 午睡" + (f" {duration:g} 分钟" if duration is not None else "时长资格未知"))
            for stage in _get(nap, "stages", []):
                a, b = _datetime(_get(stage, "start_time")), _datetime(_get(stage, "end_time"))
                if not a or not b or b <= a or a > raw.as_of:
                    continue
                category = _get(stage, "stage", "unknown")
                intervals.append(ChartInterval(start=a, end=min(b, raw.as_of), category=category,
                    label=f"{day} 午睡 · {category}", source=_get(nap, "source", "zepp"),
                    source_scope="nap_stage", qualification="vendor_estimate"))
    if not intervals and not summary:
        return None
    if intervals:
        left = min(left, min(i.start for i in intervals))
        right = max(right, max(i.end for i in intervals))
    return Chart(key="sleep", title="睡眠分期与午睡", kind="stages", unit="stage", timezone=zone,
        period_start=left, period_end=right, intervals=intervals[:3000], summary=summary,
        state="partial" if not intervals or len(intervals) > 3000 else "available", truncated=len(intervals) > 3000,
        notes=["按实际分期区间绘制；主睡眠与午睡分别标记，未知阶段不推断为清醒。", "睡眠分期与评分来自设备估计。"])


def _series_qualification(points, metric: str) -> str:
    qualifications = {p.qualification for p in points if p.qualification}
    if len(qualifications) == 1:
        return next(iter(qualifications))
    if len(qualifications) > 1:
        return "mixed_qualification"
    return "vendor_reference" if metric in VENDOR or metric.startswith("vendor_") else "observed"


def _daily_series(raw: RawDailyProfile, metric: str, label: str, unit: str,
                  start: date, end: date) -> list[ChartSeries]:
    groups = defaultdict(lambda: defaultdict(list))
    for p in raw.series.get(metric, []):
        if start <= p.day <= end and p.unit == unit and _number(p.value) is not None:
            stamp = _datetime(p.observed_at)
            if stamp and stamp > raw.as_of:
                continue
            groups[_identity(p)][p.day].append(p)
    # Naps are an independent optional stream, not synthetic zero-duration nights.
    if metric == "nap_minutes":
        for day, naps in getattr(raw, "chart_naps", {}).items():
            if start <= day <= end and all(_number(_get(n, "sleep_minutes")) is not None for n in naps):
                groups[("zepp", "nap_stage", None, "min")][day] = [
                    SeriesPoint(metric, sum(_get(n, "sleep_minutes") for n in naps), "min", day, day, "zepp", "nap_stage")]
    output = []
    for identity, by_day in sorted(groups.items(), key=lambda p: str(p[0]))[:MAX_SERIES]:
        points = []
        for offset in range((end-start).days+1):
            day = start+timedelta(days=offset)
            rows = by_day.get(day, [])
            # Source daily summaries and samples from that source remain separate
            # observations; prefer actual samples/features when they exist.
            timed = [p for p in rows if isinstance(p.observed_at, datetime) and not p.is_daily_summary]
            chosen = timed or rows
            value = float(median(p.value for p in chosen)) if chosen else None
            stamp, _ = _bounds(day, day, raw.timezone_name or "UTC")
            points.append(ChartPoint(timestamp=stamp, value=value,
                sample_count=sum(p.sample_count for p in chosen)))
        source, scope, device, _ = identity
        output.append(ChartSeries(key=_key(metric, identity), label=label+f" · {source}"+(f" · {device}" if device else ""),
            unit=unit, source=source, source_scope=scope, device_id=device,
            source_field=next((p.source_field for rows in by_day.values() for p in rows if p.source_field), f"daily.{metric}"),
            fetched_at=max((stamp for rows in by_day.values() for p in rows if (stamp := _datetime(p.fetched_at)) is not None), default=None),
            qualification=_series_qualification([p for rows in by_day.values() for p in rows], metric),
            aggregation="median", points=points, sample_count=sum(p.sample_count for p in points)))
    return output


def _daily_charts(raw: RawDailyProfile, start: date, end: date) -> list[Chart]:
    zone = raw.timezone_name or "UTC"
    left, right = _bounds(start, end, zone)
    output = []
    for metric, (label, unit, kind) in DAILY.items():
        series = _daily_series(raw, metric, label, unit, start, end)
        if not series:
            continue
        output.append(Chart(key=f"daily_{metric}", title=label, kind=kind, unit=unit, timezone=zone,
            period_start=left, period_end=right, series=series,
            preview_series=series if (end-start).days < 32 else [], resolution_seconds=86400,
            state="partial" if any(p.value is None for s in series for p in s.points) else "available",
            notes=["按本地日期排列；空白日表示没有合资格记录，不代表零或休息日。", "不同来源与设备分别呈现；各曲线只与同口径历史比较。"],
            reference=_reference(raw, metric)))
    vendor = _daily_series(raw, "vendor_load_7d", "Zepp 7 日累计", "load", start, end)
    if vendor:
        low = _daily_series(raw, "vendor_load_optimal_min", "下界", "load", start, end)
        high = _daily_series(raw, "vendor_load_optimal_max", "上界", "load", start, end)
        over = _daily_series(raw, "vendor_load_overreaching", "Zepp 超负荷参考线", "load", start, end)
        bands = []
        for row in vendor:
            lo = next((s for s in low if (s.source, s.source_scope, s.device_id) == (row.source, row.source_scope, row.device_id)), None)
            hi = next((s for s in high if (s.source, s.source_scope, s.device_id) == (row.source, row.source_scope, row.device_id)), None)
            if lo or hi:
                lower = {p.timestamp: p.value for p in lo.points} if lo else {}
                upper = {p.timestamp: p.value for p in hi.points} if hi else {}
                bands.append(ChartBand(key=row.key+":range", label="Zepp 最佳范围（厂商参考）", source=row.source,
                    points=[ChartBandPoint(timestamp=p.timestamp, lower=lower.get(p.timestamp), upper=upper.get(p.timestamp)) for p in row.points]))
        output.append(Chart(key="vendor_load_7d", title="Zepp 7 日训练负荷与厂商参考区间", kind="line", unit="load",
            timezone=zone, period_start=left, period_end=right, series=vendor+over, bands=bands[:8],
            preview_series=(vendor+over) if (end-start).days < 32 else [], resolution_seconds=86400, state="partial",
            notes=["7 日累计与参考区间直接来自 Zepp；每天的累计值不再相加。",
                   "最佳与超负荷范围是 Zepp 厂商参考，不是伤病安全线，也不替代 Vitalis 训练决策。" ]))
    return output


def _reference(raw, metric) -> ChartReference | None:
    # Existing saved baseline qualification is reused; charts never redefine a
    # normal range or turn an out-of-band value into a health diagnosis.
    baselines = getattr(raw, "chart_baselines", {})
    candidates = [b for b in baselines.get(metric, []) if _get(b, "status") in {"AVAILABLE"} and _get(b, "transform", "identity") == "identity"]
    if len(candidates) != 1:
        return None
    b = candidates[0]
    return ChartReference(lower=_get(b, "percentile_25"), upper=_get(b, "percentile_75"),
        median=_get(b, "reference_value"), label=f"个人参照 · {_get(b, 'window_days')} 日")


def _workout_charts(raw: RawDailyProfile, start: date, end: date) -> list[Chart]:
    output = []
    zone = raw.timezone_name or "UTC"
    reviews = {(r.get("workout_source", r.get("source")), r.get("workout_id")): r for r in getattr(raw, "chart_inputs", {}).get("workout_reviews", [])}
    for workout in raw.workouts:
        data = workout.get("data") or {}
        left = _datetime(workout.get("started_at") or data.get("started_at"))
        right = _datetime(data.get("ended_at"))
        if left is None or not start <= left.astimezone(ZoneInfo(zone)).date() <= end or left > raw.as_of:
            continue
        source = workout.get("source", "zepp")
        identifier = str(workout.get("workout_id", ""))
        review = reviews.get((source, identifier), {})
        if review.get("action") == "ignore" or review.get("decision") == "ignored":
            continue
        if right is None and _number(data.get("duration")):
            right = left + timedelta(minutes=float(data["duration"]))
        if right is None or right <= left:
            continue
        right = min(right, raw.as_of)
        if right <= left:
            continue
        samples = [s for s in workout.get("samples", []) if _get(s, "metric") == "heart_rate"]
        samples = [{"observed_at": _get(s, "timestamp"), "value": _get(s, "value"), "unit": "bpm",
                    "source": source, "source_scope": _get(s, "source_scope", "workout_detail"), "device_id": _get(s, "device_id")} for s in samples]
        if not samples:
            samples = [p for p in getattr(raw, "chart_samples", {}).get("heart_rate", [])
                       if (stamp := _datetime(_get(p, "observed_at"))) and left <= stamp < right]
        resolution = _resolution((right-left).total_seconds())
        series, truncated = _series("heart_rate", "训练心率", samples, left, right, raw.as_of, resolution)
        preview, _ = _series("heart_rate", "训练心率", samples, left, right, raw.as_of, _resolution((right-left).total_seconds(), preview=True))
        title = str(review.get("title") or data.get("title") or data.get("sport_mode_label") or "训练")
        summary = [f"{left.astimezone(ZoneInfo(zone)):%m-%d %H:%M} · {source}"]
        if data.get("auto_recognized") is True:
            summary.append("设备自动识别记录" + (" · 待核对" if not review else " · 已有用户核对"))
        for key, label, unit in (("load", "设备负荷", ""), ("training_effect", "设备训练效果", " / 5")):
            value = _number(data.get(key))
            if value is not None:
                summary.append(f"{label} {value:g}{unit}")
        zones = data.get("vendor_heart_rate_zones") or []
        for item in zones:
            seconds, upper = _number(_get(item, "seconds")), _number(_get(item, "upper_bpm"))
            if seconds is not None and upper is not None:
                summary.append(f"设备心率区间上界 {upper:g} bpm：{seconds:g} 秒")
        external = [w for w in getattr(raw, "chart_inputs", {}).get("external_workouts", [])
                    if (w.get("linked_workout_id") == identifier and w.get("linked_workout_source", "zepp") == source)]
        for session in external:
            sets = session.get("sets") or []
            summary.append(f"Hevy 已关联 {len(sets)} 组；保留 Hevy 动作记录与 Zepp 生理观测的各自来源。")
            for item in sets[:40]:
                name = item.get("exercise") or item.get("exercise_name") or item.get("exercise_title") or "未命名动作"
                reps = item.get("reps", item.get("repetitions"))
                weight = item.get("weight_value", item.get("weight_kg"))
                unit = item.get("weight_unit", "kg" if "weight_kg" in item else "单位未知")
                summary.append(f"Hevy · {name} · {reps if reps is not None else '次数未知'} 次 · {weight if weight is not None else '重量未知'} {unit}")
        output.append(Chart(key="workout_"+sha256((source+":"+identifier).encode()).hexdigest()[:16], title=title+" · 心率与训练记录",
            kind="line", unit="bpm", timezone=zone, period_start=left, period_end=right,
            series=series, preview_series=preview, resolution_seconds=resolution, truncated=truncated,
            state="partial", intervals=_annotations(raw, left, right), summary=summary,
            notes=["训练详情心率优先；无详情时展示同时间窗设备日内心率，来源保留。", "心率区间、负荷和训练效果沿用设备记录，不自动等同于个人阈值或训练收益。"] ))
        if len(output) >= MAX_WORKOUT_CHARTS:
            break
    return output


def _muscle_charts(raw: RawDailyProfile, start: date, end: date) -> list[Chart]:
    from .strength import StrengthAnalyzer, _apply_session_capacity
    from .running import RunningAnalyzer
    grouped = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    known_sessions = 0
    unknown_sets = 0
    external = []
    for session in getattr(raw, "chart_inputs", {}).get("external_workouts", []):
        stamp = _datetime(session.get("started_at"))
        if stamp and start <= stamp.astimezone(ZoneInfo(raw.timezone_name or "UTC")).date() <= end and stamp <= raw.as_of:
            external.append(session)
    linked = {(session.get("linked_workout_source", "zepp"), session.get("linked_workout_id"))
              for session in external if session.get("linked_workout_id")}
    ignored = {(r.get("workout_source"), r.get("workout_id")) for r in getattr(raw, "chart_inputs", {}).get("workout_reviews", [])
               if r.get("decision") == "ignored"}
    for workout in RunningAnalyzer._available_workouts(raw):
        identity = (workout.get("source", "zepp"), workout.get("workout_id"))
        if identity in linked or identity in ignored:
            continue
        data = workout.get("data") or {}
        stamp = _datetime(workout.get("started_at") or data.get("started_at"))
        if stamp is None or not start <= stamp.astimezone(ZoneInfo(raw.timezone_name or "UTC")).date() <= end:
            continue
        if data.get("training_family") != "strength":
            continue
        session = _apply_session_capacity(StrengthAnalyzer()._session(raw, workout, None))
        known_sessions += 1
        any_doses = False
        for field, qualification in (("muscle_doses", "recorded_action_estimate"), ("reference_muscle_doses", "vendor_anatomy_reference")):
            doses = getattr(session, field, [])
            # Canonical action rows and lap_62 reference rows are disjoint in
            # session_capacity; retain both qualifications instead of dropping
            # unmapped lap rows merely because another row has a canonical name.
            for dose in doses:
                any_doses = True
                key = (dose.muscle_group, dose.muscle_group_label, getattr(dose, "side", "unknown"))
                group = (session.source, qualification)
                grouped[group][key][0] += dose.direct_sets
                grouped[group][key][1] += dose.indirect_sets
        if not any_doses:
            unknown_sets += session.vendor_reported_sets or len(session.observed_sets)
    from .strength import MUSCLE_LABELS
    for session in external:
        identity = (session.get("linked_workout_source", "zepp"), session.get("linked_workout_id"))
        if identity in ignored:
            continue
        known_sessions += 1
        for item in session.get("sets") or []:
            if item.get("set_type") == "warmup":
                continue
            if item.get("mapping_status") != "user_mapped":
                unknown_sets += 1
                continue
            for field, index in (("primary_muscles", 0), ("secondary_muscles", 1)):
                for muscle in set(item.get(field) or []):
                    key = (muscle, MUSCLE_LABELS.get(muscle, muscle), item.get("side", "unknown"))
                    grouped[("hevy", "user_confirmed_mapping")][key][index] += 1
    if not known_sessions:
        return []
    left, right = _bounds(start, end, raw.timezone_name or "UTC")
    regions = [ChartRegion(key=key, label=label, side=side, direct_sets=values[0], indirect_sets=values[1],
                observed_sets=sum(values), source=source, qualification=qualification)
               for (source, qualification), doses in sorted(grouped.items())
               for (key, label, side), values in sorted(doses.items())]
    return [Chart(key="muscle_volume", title="肌群训练接触 · 主要与辅助组数", kind="body_map", unit="sets",
        timezone=raw.timezone_name or "UTC", period_start=left, period_end=right, regions=regions[:128],
        state="partial", truncated=len(regions)>128,
        summary=[f"窗口内 {known_sessions} 场力量训练", f"{unknown_sets} 个记录组缺少可定位肌群依据"],
        notes=["颜色表示有依据的训练接触组数；主要与辅助分开，不是实测肌肉受力或有效增肌组。",
               "同一组可涉及多个部位，肌群组数不能相加作为训练总组数；重量容量不分摊到人体图。"])]


def build_chart_bundle(raw: RawDailyProfile, start: date, end: date, *, period: str = "daily",
                       analysis_run_id: str | None = None, include_intraday: bool | None = None) -> ChartBundle:
    """Build only from detached facts; missing optional charts are simply absent."""
    if start > end or (end-start).days > 365:
        raise ValueError("chart window must be increasing and at most 366 days")
    if include_intraday is None:
        include_intraday = period in {"daily", "evening", "morning"}
    charts = []
    if include_intraday:
        if (end-start).days > 6:
            raise ValueError("intraday charts cover at most seven days")
        charts.extend(_intraday(raw, start, end))
        sleep = _sleep(raw, start, end)
        if sleep:
            charts.append(sleep)
        charts.extend(_workout_charts(raw, start, end))
    else:
        charts.extend(_daily_charts(raw, start, end))
        if period == "weekly":
            sleep = _sleep(raw, start, end)
            if sleep:
                charts.append(sleep)
    charts.extend(_muscle_charts(raw, start, end))
    return ChartBundle(user_id=raw.user_id, analysis_run_id=analysis_run_id, as_of=raw.as_of,
        timezone=raw.timezone_name or "UTC", period_start=start, period_end=end, charts=charts,
        notes=["图表只描述已保存记录；缺少获取时间的旧记录不补造获取时刻。"])


def select_report_charts(charts: list[Chart], kind: str) -> list[Chart]:
    """Small, period-specific selection for push; complete charts stay in API."""
    priorities = {
        "morning": ("sleep", "hrv_rmssd"),
        "daily": ("heart_rate", "sleep", "stress", "charge"),
        "evening": ("charge", "stress", "heart_rate", "muscle_volume"),
        "weekly": ("daily_sleep_duration", "vendor_load_7d", "muscle_volume", "daily_sleep_hrv"),
        "monthly": ("daily_resting_hr", "vendor_load_7d", "muscle_volume", "daily_sleep_duration"),
    }
    by_key = {c.key: c for c in charts}
    return [by_key[key] for key in priorities.get(kind, ()) if key in by_key][:3]
