"""Device A/B and account data stay independent; absent routes stay absent."""
from datetime import date, datetime, timezone

from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint


def profile():
    raw=RawDailyProfile(user_id='route-synthetic', day=date(2026,11,1), as_of=datetime(2026,11,2,tzinfo=timezone.utc), timezone_name='Asia/Shanghai')
    raw.device_inventory=[{'source':'zepp','device_id':'WATCH0001','model':'Amazfit Balance 2','aliases':['WATCH0001','AABBCCDDEEFF']},
                          {'source':'zepp','device_id':'STRAP0002','model':'Amazfit Helio Strap','aliases':['STRAP0002']}]
    return raw


def sample(value,device=None,scope='device',metric='heart_rate',minute=0):
    return SeriesPoint(metric,value,'bpm' if metric=='heart_rate' else 'score',date(2026,11,1),datetime(2026,11,1,0,minute,tzinfo=timezone.utc),'zepp',scope,device)


def test_three_routes_are_visible_without_averaging():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile()
    raw.chart_samples={'heart_rate':[sample(60,'WATCH0001'),sample(90,'STRAP0002'),sample(70,None,'user_summary')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    assert {r.kind for r in chart.routes}=={'device','account'}
    assert len(chart.routes)==3
    assert all(r.state=='available' for r in chart.routes)
    assert {s.points[0].value for s in chart.series}=={60,90,70}
    assert all(s.route_id for s in chart.series)
    assert any('Balance' in r.label for r in chart.routes)


def test_missing_account_and_second_device_are_not_filled():
    from vitalis.intelligence.charts import build_chart_bundle
    from vitalis.intelligence.chart_routes import filter_chart
    raw=profile();raw.chart_samples={'heart_rate':[sample(60,'WATCH0001')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    account=next(r for r in chart.routes if r.kind=='account')
    strap=next(r for r in chart.routes if r.device_id=='STRAP0002')
    assert account.state==strap.state=='missing'
    filtered=filter_chart(chart,account.id)
    assert filtered.series==[] and filtered.state=='unavailable'
    assert filtered.selected_route==account.id
    assert len(filtered.routes)==3


def test_unknown_has_separate_route_not_account():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile();raw.chart_samples={'heart_rate':[sample(60,None,'unknown')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    assert next(r for r in chart.routes if r.kind=='account').state=='missing'
    assert next(r for r in chart.routes if r.kind=='unknown').state=='available'


def test_only_explicit_inventory_aliases_group_physical_device():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile();raw.chart_samples={'heart_rate':[sample(60,'WATCH0001'),sample(61,'AABBCCDDEEFF'),sample(95,'AABBCC0011DDEEFF')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    watch=next(r for r in chart.routes if r.device_id=='WATCH0001')
    assert {s.device_id for s in chart.series if s.route_id==watch.id}=={'WATCH0001','AABBCCDDEEFF'}
    assert len([r for r in chart.routes if r.kind=='device'])==3
    assert len(chart.series)==3  # aliases are grouped for navigation, not averaged


def test_explicit_fused_scope_beats_bookkeeping_device_id():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile();raw.chart_samples={'heart_rate':[sample(70,'WATCH0001','user_fused')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    assert next(r for r in chart.routes if r.kind=='account').state=='available'
    assert chart.series[0].device_id=='WATCH0001'
    assert chart.series[0].route_id=='zepp:account'


def test_sleep_routes_do_not_put_unknown_into_vendor_summary():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile();raw.sleep_by_day[raw.day]={'source':'zepp','source_scope':'unknown','sleep_duration':400,'stages':[{'stage':'light','start_time':'2026-10-31T16:00:00Z','end_time':'2026-10-31T17:00:00Z'}]}
    chart=next(c for c in build_chart_bundle(raw,raw.day,raw.day).charts if c.key=='sleep')
    assert next(r for r in chart.routes if r.kind=='unknown').state=='available'
    assert next(r for r in chart.routes if r.kind=='account').state=='missing'


def test_conflicting_alias_equal_to_primary_id_does_not_choose_that_device():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile()
    raw.device_inventory[0]['aliases'].append('STRAP0002')
    raw.chart_samples={'heart_rate':[sample(80,'STRAP0002')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    route=next(r for r in chart.routes if r.id==chart.series[0].route_id)
    assert route.kind=='unknown' and '冲突' in route.label
    assert next(r for r in chart.routes if r.device_id=='STRAP0002' and r.kind=='device').state=='missing'


def test_partial_band_refresh_does_not_remove_other_source_sleep():
    from vitalis.intelligence.charts import build_chart_bundle
    raw=profile()
    raw.series['sleep_duration']=[SeriesPoint('sleep_duration',420,'min',raw.day,raw.day,'zepp','user_summary',source_field='sleep.sleep_duration')]
    raw.chart_band_records=[{'date':raw.day,'kind':'sleep','data':{'source':'zepp','source_scope':'device','device_id':'WATCH0001','sleep_duration':300}}]
    chart=next(c for c in build_chart_bundle(raw,raw.day,raw.day,period='weekly').charts if c.key=='daily_sleep_duration')
    assert {s.points[0].value for s in chart.series}=={300,420}


def test_nap_duration_without_stages_remains_visible_in_own_route():
    from vitalis.intelligence.charts import build_chart_bundle
    from vitalis.intelligence.chart_routes import filter_chart
    raw=profile();raw.chart_naps={raw.day:[{'started_at':'2026-11-01T05:00:00Z','ended_at':'2026-11-01T05:30:00Z',
        'sleep_minutes':30,'stages':[],'source':'zepp','source_scope':'device','device_id':'WATCH0001'}]}
    chart=next(c for c in build_chart_bundle(raw,raw.day,raw.day).charts if c.key=='sleep')
    route=next(r for r in chart.routes if r.device_id=='WATCH0001')
    assert route.state=='available'
    assert any('30 分钟' in line for line in filter_chart(chart,route.id).summary)


def test_unknown_route_id_rejected():
    import pytest
    from vitalis.intelligence.charts import build_chart_bundle
    from vitalis.intelligence.chart_routes import filter_chart
    raw=profile();raw.chart_samples={'heart_rate':[sample(60,'WATCH0001')]}
    chart=build_chart_bundle(raw,raw.day,raw.day).charts[0]
    with pytest.raises(ValueError):filter_chart(chart,'not-owned-device')
