"""Rank familiar muscle groups under observed constraints, with explicit gaps."""

from datetime import date, timedelta, timezone


GROUPS = {
    'PUSH': {'chest', 'shoulders', 'triceps', 'arm_back'},
    'PULL': {'back', 'biceps', 'arm_front'},
    'LEGS': {'quadriceps', 'hamstrings', 'glutes', 'thigh_front', 'thigh_back', 'calves'},
}
LABELS = {'PUSH': '推类', 'PULL': '拉类', 'LEGS': '腿部'}


def recommend_focus(raw, sessions):
    latest, blocked = {}, set()
    current, history = dict.fromkeys(GROUPS, 0.0), dict.fromkeys(GROUPS, 0.0)
    reference_only = False
    preferences = getattr(raw, 'training_preferences', None)
    if preferences and preferences.pain_or_injury_status == 'PRESENT':
        return None, None, ['已记录疼痛或伤病限制，先按该限制调整，不凭训练间隔推荐部位。'], 'PARTIAL'
    for session in sessions:
        doses = session.muscle_doses or session.reference_muscle_doses
        if not doses:
            continue
        reference_only |= not bool(session.muscle_doses)
        muscles = {d.muscle_group for d in doses if d.direct_sets + d.indirect_sets > 0}
        if session.muscle_soreness is not None and session.muscle_soreness >= 4:
            blocked |= muscles
        for focus, group in GROUPS.items():
            # Do not turn an unrecorded group into a zero-dose/rested group.
            direct = [d for d in doses if d.muscle_group in group and d.direct_sets > 0]
            if direct:
                stamp = session.started_at
                order = (session.date, stamp.replace(tzinfo=timezone.utc).timestamp() if stamp and stamp.tzinfo is None else stamp.timestamp() if stamp else 0)
                latest[focus] = max(latest.get(focus, order), order)
                amount = sum(d.direct_sets for d in direct)
                (current if session.date >= raw.day-timedelta(days=6) else history)[focus] += amount
    if not latest:
        return None, None, ['尚无可核验的肌群接触记录，先沿用熟悉动作并补齐部位信息。'], 'INSUFFICIENT_DATA'
    verified = set()
    for value in (raw.training_history_coverage or {}).get('verified_days', []):
        try:
            verified.add(value if type(value) is date else date.fromisoformat(str(value)))
        except ValueError:
            pass
    def complete_window(days):
        window = {raw.day-timedelta(days=i) for i in range(days)}
        observed = [s for s in sessions if s.date in window]
        return window <= verified and all(
            s.dose_coverage.detail_complete and s.dose_coverage.recorded_sets > 0
            and s.dose_coverage.unmapped_region_sets == 0
            and s.dose_coverage.mapped_sets + s.dose_coverage.reference_sets >= s.dose_coverage.recorded_sets
            for s in observed
        )
    complete_week = complete_window(7)
    if complete_week:
        for focus in GROUPS:
            if not current[focus] and focus not in latest:
                latest[focus] = (date.min, 0)
    leg_run = any(
        raw.day - timedelta(days=1) <= w.get('local_day', raw.day) <= raw.day
        and str((w.get('data') or {}).get('type')) == 'running'
        and ((w.get('data') or {}).get('duration') or 0) >= 60
        for w in raw.workouts
    )
    choices = [f for f in latest if not GROUPS[f] & blocked and not (f == 'LEGS' and leg_run)]
    if not choices:
        return None, None, ['已知训练部位与明显酸痛或近期长跑安排冲突，先选择恢复性活动。'], 'PARTIAL'
    # A known last exposure is only scheduling evidence, not proof of recovery.
    complete_history = complete_window(28)
    choices.sort(key=lambda f: (current[f] / (history[f]/3) if complete_history and history[f] > 0 else 1,
                                latest[f], f))
    first = choices[0]
    exposure = f'最近一次主要接触为 {latest[first][0].isoformat()}' if latest[first][0] != date.min else '完整核实的本周部位记录中未出现主要接触'
    reasons = [f'在已有部位记录中，{LABELS[first]}{exposure}；下一次可优先考虑该方向。',
               '间隔仅用于安排顺序；训练前结合局部不适与当天状态确认，不据此认定已经恢复。']
    if reference_only:
        reasons.append('部位来自厂商主次受力参考，仅给肌群方向，不照搬具体动作或重量。')
    if complete_history and history[first] > 0:
        reasons.append('排序参考该方向自身此前三周的周均接触，不与其他肌群的绝对组数比较。')
    if leg_run:
        reasons.append('近期记录了长跑，腿部力量不作为本次首选。')
    if len(choices) == 1 and latest[first][0] >= raw.day - timedelta(days=1):
        reasons.insert(0, '只有近期同一方向的记录，现有信息不足以推荐立即再次训练。')
        return None, None, reasons, 'PARTIAL'
    return first, choices[1] if len(choices) > 1 else None, reasons, 'PARTIAL'
