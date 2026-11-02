"""A day containing two devices must retain both normalized source records."""
from datetime import date,datetime,timezone
from vitalis.adapters.persistence import HealthRepository,session_scope


def test_band_source_records_keep_both_devices_and_account_separately():
    day=date(2026,11,1);at=datetime(2026,11,2,tzinfo=timezone.utc)
    with session_scope() as db:
        repo=HealthRepository(db);repo.upsert_user('band-three-routes');db.flush()
        for scope,device,value in [('device','WATCH0001',400),('device','STRAP0002',440),('user_summary',None,420)]:
            payload={'date':day.isoformat(),'source':'zepp','source_scope':scope,'device_id':device,'sleep_duration':value,'stages':[]}
            repo.save_band_source('band-three-routes',day,'sleep',payload,fetched_at=at)
        rows=repo.band_source_records('band-three-routes',day,day,as_of=at)
        assert len(rows)==3
        assert {r['data']['sleep_duration'] for r in rows}=={400,420,440}
        assert repo.band_source_records('other-user',day,day,as_of=at)==[]


def test_day_charts_include_all_saved_sleep_sources():
    from vitalis.intelligence.profile import RawDailyProfile
    from vitalis.intelligence.charts import build_chart_bundle
    raw=RawDailyProfile(user_id='synthetic',day=date(2026,11,1),as_of=datetime(2026,11,2,tzinfo=timezone.utc),timezone_name='Asia/Shanghai')
    raw.sleep_by_day[raw.day]={'sleep_duration':440,'source':'zepp','source_scope':'device','device_id':'STRAP0002'}
    raw.chart_band_records=[{'date':raw.day,'kind':'sleep','data':{'source':'zepp','source_scope':'device','device_id':dev,'sleep_duration':v,'stages':[{'stage':'light','start_time':'2026-10-31T16:00:00Z','end_time':'2026-10-31T17:00:00Z'}]}} for dev,v in [('WATCH0001',400),('STRAP0002',440)]]
    chart=next(c for c in build_chart_bundle(raw,raw.day,raw.day).charts if c.key=='sleep')
    assert len(chart.intervals)==2
    assert {i.device_id for i in chart.intervals}=={'WATCH0001','STRAP0002'}
