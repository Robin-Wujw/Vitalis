"""Explainable scheduling from observed sets, independent weeks and personal history.

Three sets and 40% of the largest direction identify a substantial session for
scheduling only. They are not a physiological effective-set/recovery threshold.
"""
from datetime import date, timedelta
from statistics import median

from .contracts import MuscleTrainingHistory, StrengthFocusHistory, StrengthFocusRecommendation
from .strength_dose import FOCUS_GROUPS

LABELS = {'PUSH': '胸肩及上臂后侧', 'PULL': '背部及上臂前侧', 'LEGS': '腿臀'}


def _complete(session):
    c = session.dose_coverage
    return (c.detail_complete and c.recorded_sets > 0 and c.unmapped_region_sets == 0
            and c.mapped_sets + c.reference_sets >= c.recorded_sets)


def _main(session, focus):
    largest = max((d.direct_sets for d in session.focus_doses), default=0)
    own = next((d.direct_sets for d in session.focus_doses if d.focus == focus), 0)
    return own >= 3 and own >= largest * .4


def _amount(session, focus, weight=.5):
    return sum(d.direct_sets + weight*d.indirect_sets for d in session.focus_doses if d.focus == focus)


def recommend_focus(raw, sessions):
    day = raw.day
    sessions = sorted((s for s in sessions if day-timedelta(days=179) <= s.date <= day), key=lambda s: s.date)
    verified = set()
    for value in (raw.training_history_coverage or {}).get('verified_days', []):
        try:
            verified.add(value if type(value) is date else date.fromisoformat(str(value)))
        except ValueError:
            pass
    def complete(start, end):
        days = {start+timedelta(days=i) for i in range((end-start).days+1)}
        return days <= verified and all(_complete(s) for s in sessions if start <= s.date <= end)

    recent_start, baseline_end = day-timedelta(days=6), day-timedelta(days=7)
    current = [s for s in sessions if s.date >= recent_start]
    recent_complete = complete(recent_start, day)
    histories, baseline_by_focus = [], {}
    for focus in FOCUS_GROUPS:
        known = [s for s in sessions if _amount(s, focus) > 0]
        main = [s for s in known if _main(s, focus)]
        baseline_start = day-timedelta(days=90)
        baseline = [s for s in sessions if baseline_start <= s.date <= baseline_end]
        usable = [s for s in baseline if _complete(s) and _main(s, focus)]
        if len(usable) < 4:
            baseline_start = day-timedelta(days=179)
            baseline = [s for s in sessions if baseline_start <= s.date <= baseline_end]
            usable = [s for s in baseline if _complete(s) and _main(s, focus)]
        week_ranges = []
        end = baseline_end
        while end-timedelta(days=6) >= baseline_start:
            start = end-timedelta(days=6)
            if complete(start, end):
                week_ranges.append((start, end))
            end -= timedelta(days=7)
        # Verified weeks may contain genuine zero-dose weeks; unknown days never do.
        weekly = [sum(_amount(s, focus) for s in baseline if start <= s.date <= end)
                  for start, end in week_ranges]
        base = median(weekly) if len(weekly) >= 3 else None
        # A long gap changes the routine: prefer the newest six complete weeks.
        if len(week_ranges) >= 6:
            weekly = weekly[:6]
            week_ranges = week_ranges[:6]
            base = median(weekly)
        baseline_by_focus[focus] = (baseline, week_ranges)
        main_days = sorted({s.date for s in usable})
        intervals = [(b-a).days for a,b in zip(main_days, main_days[1:]) if complete(a,b)]
        direct = sum(d.direct_sets for s in current for d in s.focus_doses if d.focus == focus)
        auxiliary = sum(d.indirect_sets for s in current for d in s.focus_doses if d.focus == focus)
        h = StrengthFocusHistory(
            focus=focus, label=LABELS[focus], last_main_day=main[-1].date if main else None,
            last_contact_day=known[-1].date if known else None,
            recent_direct_sets=direct, recent_indirect_sets=auxiliary, recent_complete=recent_complete,
            recent_start=recent_start, recent_end=day, baseline_start=baseline_start, baseline_end=baseline_end,
            baseline_sessions=len(usable), baseline_missing_sessions=sum(not _complete(s) for s in baseline),
            baseline_complete_weeks=len(week_ranges),
            baseline_verified_days=sum(baseline_start <= d <= baseline_end for d in verified),
            typical_session_sets=median([_amount(s,focus) for s in usable]) if usable else None,
            typical_interval_days=median(intervals[-6:]) if len(intervals) >= 3 else None,
            baseline_weekly_sets=base, estimated_equivalent_sets=direct+.5*auxiliary,
            weekly_ratio=round((direct+.5*auxiliary)/base,3) if recent_complete and base and base > 0 else None,
            sources=sorted({s.source for s in known}),
            limitations=[] if recent_complete else ['近期组数为已记录量；有缺失时不判断欠量。'],
        )
        histories.append(h)
    result = StrengthFocusRecommendation(history=histories)
    preferences = getattr(raw, 'training_preferences', None)
    if preferences and preferences.pain_or_injury_status == 'PRESENT':
        result.reasons = ['已记录疼痛或伤病限制，先按该限制调整训练。']
        return result
    blocked = set()
    for session in sessions:
        if session.date >= day-timedelta(days=2) and ((session.muscle_soreness is not None and session.muscle_soreness >= 4) or (session.session_rpe is not None and session.session_rpe >= 9)):
            blocked.update(d.focus for d in session.focus_doses)
    recent_lower_back = any(
        s.date >= day-timedelta(days=1) and sum(d.direct_sets for d in [*s.muscle_doses, *s.reference_muscle_doses]
                                              if d.muscle_group == 'lower_back') >= 3
        for s in sessions)
    if recent_lower_back:
        blocked.add('LEGS')
    long_run = any(
        isinstance(w.get('local_day'), date) and day-timedelta(days=1) <= w['local_day'] <= day
        and (w.get('data') or {}).get('type') == 'running'
        and ((w.get('data') or {}).get('duration') or 0) >= 60 for w in raw.workouts)
    if long_run:
        blocked.add('LEGS')
    # Missing history never creates an untrained/rested direction. Unseen groups
    # can be joint options only with a verified complete current week.
    choices = [h for h in histories if h.focus not in blocked
               and (h.last_main_day is not None or (recent_complete and h.last_contact_day is None))
               and (h.last_main_day is None or h.last_main_day < day-timedelta(days=1))]
    if not choices:
        result.reasons = ['近期训练或已知不适限制了候选；先安排恢复性活动，不机械重复刚练过的部位。']
        result.status = 'PARTIAL' if sessions else 'INSUFFICIENT_DATA'
        return result

    def rank(weight):
        ratios = {}
        for h in choices:
            baseline, ranges = baseline_by_focus[h.focus]
            values = [sum(_amount(s,h.focus,weight) for s in baseline if a <= s.date <= b) for a,b in ranges]
            base = median(values) if len(values) >= 3 else None
            ratios[h.focus] = sum(_amount(s,h.focus,weight) for s in current)/base if recent_complete and base else None
        # Only compare history-derived tiers when all candidates have that evidence.
        comparable = all(ratios[h.focus] is not None for h in choices)
        usual_intervals = all(h.typical_interval_days for h in choices)
        def key(h):
            tier = int(ratios[h.focus] > 1.3) if comparable else 0
            elapsed = (day-h.last_main_day).days if h.last_main_day else 180
            priority = elapsed/h.typical_interval_days if usual_intervals else elapsed
            return tier, -priority
        keys = {h.focus:key(h) for h in choices}
        best = min(keys.values())
        # Close historical scheduling evidence is a tie, not an arbitrary winner.
        return {f for f,k in keys.items() if k[0] == best[0] and abs(k[1]-best[1]) <= (abs(best[1])*.1 if usual_intervals else 0)}

    winners = set.union(*(rank(w) for w in (.25,.5,.75)))
    result.candidates = [h.focus for h in histories if h.focus in winners]
    others = [h.focus for h in choices if h.focus not in winners]
    result.alternative = others[0] if len(others) == 1 else None
    names = '、'.join(LABELS[f] for f in result.candidates)
    result.reasons.append(f'下次候选：{names}。' if len(winners)==1 else f'下次并列候选：{names}；现有依据接近，不强行排序。')
    for h in histories:
        if h.last_main_day:
            text = f'{h.label}上次集中训练 {h.last_main_day.isoformat()}'
            if h.last_contact_day and h.last_contact_day > h.last_main_day:
                text += f'，之后 {h.last_contact_day.isoformat()} 另有少量或辅助参与'
            text += f'；近 7 天主要参与 {h.recent_direct_sets:g} 组、辅助参与 {h.recent_indirect_sets:g} 组。'
            result.reasons.append(text)
        if h.focus in winners and h.typical_session_sets is not None:
            result.reasons.append(f'{h.label}历史 {h.baseline_sessions} 次明细完整训练，每次估算组数中位数 {h.typical_session_sets:g}；当前已记录量用于比较，不设补量任务。')
    if recent_lower_back:
        result.reasons.append('近两天腰背已有较多主要参与组，腿部训练可能重叠用力，本次先不列为首选。')
    if long_run:
        result.reasons.append('近两天有至少 60 分钟跑步，腿部不列为本次首选；跑步不折算力量组数。')
    if not recent_complete:
        result.reasons.append('近期有未核实日期或缺明细场次；保留已知训练顺序和可用历史，不把缺失当休息。')
    result.reasons.append('肌群组数按动作关系估算；未记录不适仍可给候选，训练前按实际状态调整，不凭间隔认定恢复。')
    return result


def muscle_history(day, sessions):
    """Keep side- and mapping-specific observed dose; never impute missing weights."""
    grouped = {}
    for s in sessions:
        if not day-timedelta(days=179) <= s.date <= day:
            continue
        for d in [*s.muscle_doses, *s.reference_muscle_doses]:
            grouped.setdefault((d.muscle_group, d.side), []).append((s,d))
    output = []
    for (group,side), rows in sorted(grouped.items()):
        # Aggregate within a session before computing a typical session; several
        # sources/rows for the same muscle are not independent training sessions.
        def amounts(selected):
            values = {}
            for s,d in selected:
                key = (s.source,s.workout_id)
                values[key] = values.get(key,0)+d.equivalent_sets
            return list(values.values())
        recent = [(s,d) for s,d in rows if s.date >= day-timedelta(days=6)]
        baseline_start = day-timedelta(days=90)
        baseline = [(s,d) for s,d in rows if baseline_start <= s.date < day-timedelta(days=6) and _complete(s)]
        if len(amounts(baseline)) < 4:
            baseline_start = day-timedelta(days=179)
            baseline = [(s,d) for s,d in rows if baseline_start <= s.date < day-timedelta(days=6) and _complete(s)]
        values = amounts(baseline)
        direct = sum(d.direct_sets for s,d in recent)
        auxiliary = sum(d.indirect_sets for s,d in recent)
        output.append(MuscleTrainingHistory(
            muscle_group=group, muscle_group_label=rows[0][1].muscle_group_label, side=side,
            last_main_day=max((s.date for s,d in rows if d.direct_sets >= 3),default=None),
            last_contact_day=max(s.date for s,d in rows),
            direct_sets_7d=direct, indirect_sets_7d=auxiliary, estimated_sets_7d=direct+.5*auxiliary,
            typical_session_sets=median(values) if values else None,
            baseline_sessions=len(values), recent_sessions=len(amounts(recent)),
            history_start=baseline_start, history_end=day,
            sources=sorted({d.source for s,d in rows}),
        ))
    return output
