"""Explicit Zepp merged flags outrank the contributing device's serial."""
import base64
import json
import pytest
from vitalis.adapters.zepp.parser import ZeppParser


def payload(merged=1, sleep_merged=None):
    sleep={'st':1793462400,'ed':1793491200,'wk':0,'dp':90,'lt':390,'ss':85,'stage':[],'odd_stage':[]}
    if sleep_merged is not None:sleep['isMerged']=sleep_merged
    summary={'tz':28800,'sn':'WATCHSERIAL0001','isMerged':merged,'slp':sleep,'stp':{'ttl':5000}}
    return {'data':{'items':[{'date_time':'2026-11-01','device_id':'WATCHSERIAL0001',
        'summary':base64.b64encode(json.dumps(summary).encode()).decode(),
        'data_hr':base64.b64encode(bytes([60,61,0])).decode()}]}}


def test_explicit_merged_band_is_account_result_not_device_result():
    sleeps,activities=ZeppParser().parse_band(payload(1,1),timezone_name='Asia/Shanghai')
    heart=ZeppParser.parse_band_heart_rate(payload(1,1),timezone_name='Asia/Shanghai')
    for item in [*sleeps.values(),*activities.values(),*heart]:
        assert item.source_scope=='user_fused'
        assert item.device_id is None
    assert next(iter(sleeps.values())).contributing_device_id=='WATCHSERIAL0001'


@pytest.mark.parametrize('flag',[0,False,'1',255,None])
def test_only_explicit_boolean_or_integer_one_establishes_band_merge(flag):
    rows=ZeppParser.parse_band_heart_rate(payload(flag),'Asia/Shanghai')
    assert all(row.source_scope=='device' for row in rows)


def test_readiness_app_algorithm_result_stays_separate_from_two_devices():
    events=[{'eventType':'readiness','subType':'watch_score','value':{'timestamp':1793491200000,
        'deviceId':device,'deviceType':kind,'deviceSource':source,'sleepHRV':hrv}} for device,kind,source,hrv in
        [('WATCH0001',0,9568512,40),('STRAP0002',0,10289410,70),('app',2,2,55)]]
    rows=ZeppParser.parse_readiness_samples({'items':events},'Asia/Shanghai')
    assert {(r.device_id,r.source_scope,r.value) for r in rows}=={
        ('WATCH0001','device',40),('STRAP0002','device',70),(None,'user_summary',55)}
    malformed={**events[-1],'value':{**events[-1]['value'],'deviceType':0}}
    assert ZeppParser.parse_readiness_samples({'items':[malformed]},'Asia/Shanghai')[0].source_scope=='unknown'


def test_app_daily_stress_header_is_contributing_device_not_whole_day_identity():
    day,samples=ZeppParser.parse_wellness({'items':[{'eventType':'all_day_stress','timestamp':1793491200000,
        'deviceId':'WATCH0001','avgStress':30,'data':'[{"time":1793491260000,"value":25}]'}]},
        'wellness:all_day_stress:user:2026-11-01:2026-11-01')
    assert {(r.source_scope,r.device_id) for r in [*day,*samples]}=={('user_summary',None)}
    assert all(r.contributing_device_id=='WATCH0001' for r in [*day,*samples])
    assert all(r.qualification=='vendor_selected' for r in [*day,*samples])


def test_top_level_activity_merge_does_not_establish_sleep_merge():
    raw=payload(1,0)
    sleeps,_=ZeppParser().parse_band(raw,'Asia/Shanghai')
    assert next(iter(sleeps.values())).source_scope=='device'
    assert ZeppParser.parse_band_heart_rate(raw,'Asia/Shanghai')[0].source_scope=='user_fused'


def test_nested_sleep_merged_does_not_relabel_unmerged_heart_rate():
    raw=payload(0,1)
    sleeps,_=ZeppParser().parse_band(raw,'Asia/Shanghai')
    assert next(iter(sleeps.values())).source_scope=='user_fused'
    assert ZeppParser.parse_band_heart_rate(raw,'Asia/Shanghai')[0].source_scope=='device'
