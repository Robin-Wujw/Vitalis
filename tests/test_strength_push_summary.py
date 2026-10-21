from copy import deepcopy

from vitalis.intelligence.report_presentation import _strength_display_section


def card(candidates):
    strength = {
        'recent_sessions': [{'date': '2026-08-20'}],
        'next_focus_candidates': candidates,
        'focus_history': [
            {'focus': 'PUSH', 'last_main_day': '2026-08-17'},
            {'focus': 'PULL', 'last_main_day': '2026-08-20'},
            {'focus': 'LEGS', 'last_main_day': '2026-08-18'},
        ],
        'next_focus_reasons': ['近 7 天主要参与 22 组、辅助参与 14 组。',
                               '历史中位数 17.5；不设补量任务。',
                               '近期有未核实日期或缺明细场次。'],
    }
    return {'date': '2026-08-20', 'features': {'training': {'strength': strength}}}


def test_strength_push_projects_short_advice_without_changing_evidence():
    payload = card(['PUSH'])
    before = deepcopy(payload)
    facts = _strength_display_section(payload)['facts']
    assert len(facts) == 3
    assert facts[0] == '下次力量建议：胸肩、三头肌。'
    assert '8月17日' in facts[1] and '8月20日' in facts[1]
    assert '热身' in facts[2]
    assert not any(word in ''.join(facts) for word in ('近 7 天', '中位数', '不设补量', '未核实'))
    assert payload == before


def test_strength_push_preserves_joint_options():
    facts = _strength_display_section(card(['PUSH', 'LEGS']))['facts']
    assert facts[0] == '下次力量建议：胸肩、三头肌或腿臀，择一。'


def test_strength_push_preserves_saved_restriction_without_inventing_candidate():
    payload = card([])
    reason = '已记录疼痛或伤病限制，先按该限制调整训练。'
    payload['features']['training']['strength']['next_focus_reasons'] = [reason]
    assert _strength_display_section(payload)['facts'] == [reason]
