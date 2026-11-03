"""Only morning/evening reader changes; daily audit stays separate."""
from copy import deepcopy
from vitalis.intelligence.morning_briefing import MorningBriefingEngine
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.report_rendering import render_report
from vitalis.intelligence.report_presentation import daily_profile_presentation
from tests.test_report_content import synthetic_daily_fixture


def test_push_summaries_replace_generic_events_but_daily_keeps_its_projection():
    daily=synthetic_daily_fixture()
    daily['events']=[{'type':'RECOVERY_SUPPRESSED','lifecycle':'PERSISTING','summary':'多个恢复信号同时偏离个人基线。'},
        {'type':'ACTIVITY_SURGE','summary':'近7天步数多49%。'}]
    expected=daily_profile_presentation(deepcopy(daily))
    morning=MorningBriefingEngine().build_payload(daily)
    evening=EveningBriefingEngine().build_payload(daily)
    for briefing in (morning,evening):
        text=render_report(briefing,'markdown').content
        assert '多个恢复信号同时偏离个人基线' not in text
        assert '步数多49%' not in text
        assert '数据质量：数据完整' not in text
        assert briefing['alerts']
    assert daily_profile_presentation(daily)==expected
    assert daily['events'][0]['summary']=='多个恢复信号同时偏离个人基线。'


def test_morning_sleep_is_compact_and_qualified_without_charts():
    daily=synthetic_daily_fixture()
    sleep=daily['features']['sleep']
    sleep.update(deep_minutes=61,light_minutes=285,rem_minutes=89,awake_minutes=10,wake_count=7,vendor_sleep_score=74)
    report=MorningBriefingEngine().build_payload(daily)
    html=render_report(report,'html').content
    assert '昨夜睡眠' in html and '深睡' in html and '1 小时 1 分钟' in html
    assert 'Zepp 睡眠评分' in html
    assert 'data-quality' not in html
    assert '记录曲线' not in html


def test_unrenderable_account_chart_cannot_block_an_otherwise_valid_briefing():
    from tests.test_report_microcharts import _sleep
    chart=_sleep()
    chart['intervals'][1]['device_id']='conflicting-account-metadata'
    daily=synthetic_daily_fixture();daily['charts']=[chart]
    report=MorningBriefingEngine().build_payload(daily)
    html=render_report(report,'html').content
    assert '图暂未展开' in html and '关键记录' in html


def test_facts_only_morning_has_sleep_facts_but_no_new_advice():
    daily=synthetic_daily_fixture()
    report=MorningBriefingEngine().build_payload(daily,{'facts_only':True,'coverage_reason':'sleep_incomplete'})
    text=render_report(report,'markdown').content
    assert '热身正常' not in text and '不额外加量' not in text
    assert report.get('action_plan') is None
