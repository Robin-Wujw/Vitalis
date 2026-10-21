"""Experimental running capability with elapsed-time and cohort qualification.

All estimates remain shadow-only until time-held-out validation succeeds.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from math import exp, isfinite, log, sqrt
from statistics import median
from random import Random

from .contracts import CriticalSpeedEstimate, RunningPerformance, RunningPerformanceCohort


WINDOW_DAYS = 42
HALF_LIFE_DAYS = 21
TARGET_SECONDS = (180, 420, 720)


def composite_candidate(values, baseline):
    """Fixed experimental weights. Missing dimensions never change the scale."""
    keys = ('speed', 'efficiency', 'durability')
    for row in (values, baseline):
        if any(not isinstance(row.get(k), (int, float)) or isinstance(row[k], bool) or not isfinite(row[k]) or row[k] <= 0 for k in keys):
            return None
    ratios = [values[k] / baseline[k] if k != 'durability' else min(1, values[k]) / min(1, baseline[k]) for k in keys]
    return round(100 * exp(sum(w * log(r) for w, r in zip((.6, .25, .15), ratios))), 2)


def _epoch(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).timestamp()


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _fit(points, day):
    weights = [2 ** (-(day - p['day']).days / HALF_LIFE_DAYS) for p in points]
    total = sum(weights)
    mx = sum(w * p['seconds'] for w, p in zip(weights, points)) / total
    my = sum(w * p['distance_m'] for w, p in zip(weights, points)) / total
    variance = sum(w * (p['seconds'] - mx) ** 2 for w, p in zip(weights, points))
    if variance <= 0:
        return None
    slope = sum(w * (p['seconds'] - mx) * (p['distance_m'] - my) for w, p in zip(weights, points)) / variance
    intercept = my - slope * mx
    error = sqrt(sum(w * ((p['distance_m'] - slope * p['seconds'] - intercept) / p['distance_m']) ** 2 for w, p in zip(weights, points)) / total) * 100
    return slope, intercept, error


def fit_critical_speed(points, *, as_of_day):
    usable = [p for p in points if not p.get('paused') and type(p.get('day')) is date
              and as_of_day - timedelta(days=WINDOW_DAYS - 1) <= p['day'] <= as_of_day
              and isinstance(p.get('seconds'), (int, float)) and 180 <= p['seconds'] <= 720
              and isinstance(p.get('distance_m'), (int, float)) and isfinite(p['distance_m']) and p['distance_m'] > 0]
    unique = {(p.get('source'), p.get('workout_id'), p['seconds']): p for p in usable}
    usable = list(unique.values())
    sessions = {(p.get('source'), p.get('workout_id')) for p in usable}
    result = CriticalSpeedEstimate(sample_sessions=len(sessions), sample_points=len(usable),
        evidence_workouts=sorted(f"{s}:{w}" for s, w in sessions))
    if len(sessions) < 3 or len({p['seconds'] for p in usable}) < 3 or max(p['seconds'] for p in usable) / min(p['seconds'] for p in usable) < 2.5:
        result.limitations = ['至少需要三场独立训练和三个分散时长；当前证据不足。']
        return result
    # Select the observed upper envelope, retaining original contributing IDs.
    envelope = [max((p for p in usable if p['seconds'] == t), key=lambda p: p['distance_m']) for t in sorted({p['seconds'] for p in usable})]
    fitted = _fit(envelope, as_of_day)
    if fitted is None:
        return result
    speed, capacity, error = fitted
    if speed <= 0 or not 0 < capacity <= 1000 or error > 3 or any(p['distance_m'] / p['seconds'] <= speed for p in envelope):
        result.limitations = ['距离—时间关系不满足正参数或拟合质量要求，未给出临界速度。']
        return result
    changes = []
    for identity in sessions:
        remaining = [p for p in usable if (p.get('source'), p.get('workout_id')) != identity]
        alternative = _fit(remaining, as_of_day) if len({p['seconds'] for p in remaining}) >= 2 else None
        if alternative:
            changes.append(abs(alternative[0] / speed - 1) * 100)
    if not changes or max(changes) > 10:
        result.limitations = ['移除一场训练后参数不稳定，未给出临界速度。']
        return result
    result.critical_speed_mps = round(speed, 4)
    result.distance_capacity_m = round(capacity, 1)
    result.fit_error_percent = round(error, 2)
    result.leave_one_session_error_percent = round(max(changes), 2)
    result.qualification = 'test' if all(p.get('qualification') == 'test' for p in usable) else 'observed'
    result.limitations = ['日常训练上包络可能低估真实能力；当前模型未通过独立成绩验证。']
    if len(sessions) >= 6:
        grouped = {identity: [p for p in usable if (p.get('source'), p.get('workout_id')) == identity] for identity in sessions}
        identities, estimates, random = sorted(sessions), [], Random(0)
        for _ in range(200):
            draw = [p for _ in identities for p in grouped[random.choice(identities)]]
            durations = {p['seconds'] for p in draw}
            if len(durations) < 3:
                continue
            sampled_envelope = [max((p for p in draw if p['seconds'] == t), key=lambda p: p['distance_m']) for t in sorted(durations)]
            estimate = _fit(sampled_envelope, as_of_day)
            if estimate and estimate[0] > 0 and 0 < estimate[1] <= 1000 and estimate[2] <= 3:
                estimates.append(estimate[0])
        if len(estimates) >= 100:
            estimates.sort()
            result.critical_speed_interval_mps = (round(estimates[int(len(estimates)*.025)], 4), round(estimates[min(len(estimates)-1, int(len(estimates)*.975))], 4))
            result.uncertainty_method = 'session-bootstrap-200-95pct'
            result.limitations.append('区间仅反映按场次抽样的模型波动，不包含设备、天气或生理测量系统误差。')
    if result.critical_speed_interval_mps is None:
        result.limitations.append('独立场次或有效抽样不足，未给出不确定性区间。')
    return result


def _bins(samples, metric, unit):
    """Integrate valid observed intervals, never interpolate across long gaps."""
    stream = sorted((x for x in samples if x.metric == metric and x.unit == unit and isfinite(x.value)), key=lambda x: x.timestamp)
    integral, coverage = defaultdict(float), defaultdict(float)
    for current, following in zip(stream, stream[1:]):
        start, end = _epoch(current.timestamp), _epoch(following.timestamp)
        if not 0 < end - start <= 15 or current.value <= 0:
            continue
        if metric == 'speed' and current.value > 12:
            continue
        if metric == 'heart_rate' and not 35 <= current.value <= 230:
            continue
        while start < end:
            bucket = int(start // 10)
            finish = min(end, (bucket + 1) * 10)
            integral[bucket] += current.value * (finish - start)
            coverage[bucket] += finish - start
            start = finish
    return {b: integral[b] / coverage[b] for b in integral if 9 <= coverage[b] <= 10.01}


def _blocks(bins):
    blocks = []
    for key in sorted(bins):
        if not blocks or key != blocks[-1][-1] + 1:
            blocks.append([])
        blocks[-1].append(key)
    return blocks


def _session_points(workout, speed):
    details = workout.get('detail') or {}
    pauses = details.get('pauses', []) if isinstance(details, dict) else details.pauses
    # Conservative first version excludes paused sessions from capability fits.
    if pauses:
        return []
    distance = sorted((x for x in workout.get('samples') or [] if x.metric == 'distance' and x.unit == 'm'), key=lambda x: x.timestamp)
    for left, right in zip(distance, distance[1:]):
        dt = (right.timestamp - left.timestamp).total_seconds()
        if dt <= 0 or right.value < left.value or (right.value - left.value) / dt > 12:
            return []
    points = []
    for seconds in TARGET_SECONDS:
        n = seconds // 10
        best = None
        for block in _blocks(speed):
            if len(block) < n:
                continue
            values = [speed[k] for k in block]
            rolling = sum(values[:n])
            maximum = rolling
            for i in range(n, len(values)):
                rolling += values[i] - values[i - n]
                maximum = max(maximum, rolling)
            best = max(best or 0, maximum * 10)
        if best is not None:
            points.append({'seconds': seconds, 'distance_m': best,
                'day': workout['local_day'], 'workout_id': workout['workout_id'],
                'source': workout.get('source', 'zepp'), 'qualification': 'observed'})
    return points


def analyze_running_performance(raw):
    from .running import RunningAnalyzer
    if not getattr(raw, 'running_model_samples_complete', True):
        return RunningPerformance(limitations=['训练样本读取达到预算，无法验证模型窗口完整性，未计算能力维度。'])
    start = raw.day - timedelta(days=WINDOW_DAYS - 1)
    runs = RunningAnalyzer._runs(RunningAnalyzer._available_workouts(raw), start, raw.day)
    cohorts = defaultdict(list)
    excluded = 0
    for run in runs:
        data = run.get('data') or {}
        devices_by_metric = {m: {x.device_id for x in run.get('samples') or [] if x.device_id and x.metric == m} for m in ('speed', 'heart_rate')}
        if any(len(devices) > 1 for devices in devices_by_metric.values()):
            excluded += 1
            continue
        known_devices = set().union(*devices_by_metric.values())
        device = (next(iter(known_devices)) if len(known_devices) == 1 and all(devices_by_metric.values())
                  else ';'.join(f'{m}={next(iter(d)) if d else "unknown"}' for m, d in sorted(devices_by_metric.items())) if known_devices else None)
        mode = str(data.get('sport_mode') or 'unknown')
        cohorts[(str(run.get('source') or 'zepp'), device, mode)].append(run)
    output = []
    for (source, device, mode), workouts in sorted(cohorts.items(), key=lambda x: str(x[0])):
        identity = sha256(f'{source}|{device}|{mode}'.encode()).hexdigest()[:16]
        points, stable, durability_candidates, valid = [], [], [], 0
        limitations = ['地形和天气未完整调整；所有能力估计仅用于影子验证。']
        if device is None:
            limitations.append('设备身份未知，无法验证跨场设备一致性。')
        for workout in workouts:
            samples = workout.get('samples') or []
            speed, hr = _bins(samples, 'speed', 'm/s'), _bins(samples, 'heart_rate', 'bpm')
            valid += len(speed) * 10
            points.extend(_session_points(workout, speed))
            detail = workout.get('detail') or {}
            if detail.get('pauses') if isinstance(detail, dict) else detail.pauses:
                continue
            common = sorted(speed.keys() & hr.keys())
            # A fixed protocol: omit first and last five minutes, then require
            # a contiguous eight-minute block with <=10% speed MAD/range.
            if not common:
                continue
            cadence = _bins(samples, 'cadence', 'spm')
            coincident = set(common) & cadence.keys()
            if len(coincident) >= 48 and sum(abs(hr[k] - cadence[k]) <= 2 for k in coincident) / len(coincident) >= .7:
                limitations.append('心率与步频长期接近，存在传感器步频锁定疑点，未用于配速—心率比较。')
                continue
            selected = [k for k in common if common[0] + 30 <= k <= common[-1] - 30]
            eligible = [b for b in _blocks({k: speed[k] for k in selected}) if len(b) >= 48]
            if not eligible:
                continue
            block = max(eligible, key=len)
            vm = median(speed[k] for k in block)
            if max(abs(speed[k] / vm - 1) for k in block) > .1:
                continue
            stable.append((workout['local_day'], [(hr[k], speed[k]) for k in block]))
            if len(common) >= 270 and len(block) >= 180:
                early, late = block[:90], block[-90:]
                ratio = (sum(speed[k] for k in late) / sum(hr[k] for k in late)) / (sum(speed[k] for k in early) / sum(hr[k] for k in early))
                duration_band = int((workout.get('data') or {}).get('duration') or 0) // 15 * 15
                hr_band = round(median(hr[k] for k in block) / 10) * 10
                durability_candidates.append((workout['local_day'], duration_band, hr_band, ratio))
        baseline = (getattr(raw, 'running_model_baselines', {}) or {}).get(identity, {})
        protocol = baseline.get('durability_protocol') or {}
        if not protocol:
            groups = defaultdict(list)
            for day, duration_band, hr_band, ratio in sorted(durability_candidates):
                groups[(duration_band, hr_band)].append((day, ratio))
            eligible = [(entries[2][0], duration_band, hr_band) for (duration_band, hr_band), entries in groups.items() if len(entries) >= 3]
            if eligible:
                _, duration_band, hr_band = min(eligible)
                protocol = {'duration_band_start_minutes': duration_band, 'heart_rate_center_bpm': hr_band}
        durability = [ratio for _, duration_band, hr_band, ratio in durability_candidates
                      if duration_band == protocol.get('duration_band_start_minutes') and hr_band == protocol.get('heart_rate_center_bpm')]
        h0, version = baseline.get('reference_heart_rate_bpm'), baseline.get('baseline_version')
        if h0 is None and len(stable) >= 6 and (max(x[0] for x in stable) - min(x[0] for x in stable)).days >= 14:
            first = sorted(stable, key=lambda x: x[0])[:6]
            h0 = round(median(h for _, pairs in first for h, _ in pairs) / 5) * 5
            version = 'running-performance-1:' + sha256(f'{identity}|{first[0][0]}|{first[-1][0]}|{h0}|10s-5min-8min'.encode()).hexdigest()[:16]
        matched = [(day, [v for h, v in pairs if abs(h - h0) <= 2]) for day, pairs in stable] if h0 else []
        matched = [(day, median(values)) for day, values in matched if len(values) >= 48]
        efficiency = median(v for _, v in matched) if len(matched) >= 6 and (max(d for d, _ in matched)-min(d for d, _ in matched)).days >= 14 else None
        if efficiency is None:
            limitations.append('固定参考心率的稳定片段或独立场次不足，未给出配速—心率表现。')
        speed_estimate = fit_critical_speed(points, as_of_day=raw.day)
        values = {'speed': speed_estimate.critical_speed_mps, 'efficiency': efficiency,
                  'durability': min(1, median(durability)) if len(durability) >= 3 else None}
        baseline_values = baseline.get('baseline_values') or {}
        if not baseline_values and all(values.values()):
            baseline_values = values
            version = str(version) + ':full:' + sha256(f'{raw.day}|{values}|{protocol}'.encode()).hexdigest()[:12]
        observed = [_timestamp(w.get('started_at')) for w in workouts]
        fetched = [_timestamp(w.get('fetched_at') or (w.get('data') or {}).get('fetched_at')) for w in workouts]
        scopes = {sample.source_scope for w in workouts for sample in w.get('samples') or []}
        if any(stamp is None for stamp in fetched):
            limitations.append('部分训练的获取时间未知，不能据此声称完成严格历史回放。')
        output.append(RunningPerformanceCohort(
            cohort_id=identity, source=source, device_id=device, sport_mode=mode,
            source_scope='+'.join(sorted(scopes)) or 'unknown',
            observed_at=max((stamp for stamp in observed if stamp is not None), default=None),
            fetched_at=max(fetched) if all(stamp is not None for stamp in fetched) else None,
            window_start=start, window_end=raw.day, sample_sessions=len(workouts),
            valid_minutes=round(valid / 60, 1), speed=speed_estimate,
            reference_heart_rate_bpm=h0, baseline_version=version,
            efficiency_speed_mps=round(efficiency, 4) if efficiency else None,
            durability_ratio=round(median(durability), 4) if len(durability) >= 3 else None,
            durability_sessions=len(durability), limitations=limitations,
            durability_protocol=protocol,
            baseline_values=baseline_values, candidate_index=composite_candidate(values, baseline_values),
        ))
    limits = [
        '合成指数需独立比赛/测试的时间留出验证；未验证时不发布总值或据此指定训练配速。'
    ]
    if excluded:
        limits.append(f'{excluded} 场同一指标包含多个设备来源，未合并为能力证据。')
    return RunningPerformance(cohorts=output, limitations=limits)
