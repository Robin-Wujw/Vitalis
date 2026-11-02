"""Late vendor revisions cannot replace newer chart facts or lose source time."""
from datetime import date, datetime, timedelta, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.domain import DailyMetric, MetricSample


def test_daily_metric_preserves_protocol_field_and_rejects_old_revision():
    day = date(2026,10,7)
    current = datetime(2026,10,7,2,tzinfo=timezone.utc)
    with session_scope() as db:
        repo=HealthRepository(db)
        repo.upsert_user("chart-provenance")
        db.flush()
        for value,revision in ((80,current),(50,current-timedelta(hours=1))):
            repo.save_daily_metrics([DailyMetric(user_id="chart-provenance",date=day,metric="readiness",value=value,unit="score",
                source_field="rdnsScore",qualification="vendor_reference",observed_at=current-timedelta(hours=2),revision_at=revision)])
        row=repo.daily_metrics("chart-provenance",day,day,"readiness")[0]
        assert row.value==80
        assert row.source_field=="rdnsScore"
        assert row.qualification=="vendor_reference"
        assert row.revision_at==current.replace(tzinfo=None)


def test_sample_revisions_keep_one_observation_and_newest_value():
    current = datetime(2026,10,7,2,tzinfo=timezone.utc)
    observed = current-timedelta(hours=2)
    with session_scope() as db:
        repo=HealthRepository(db)
        repo.upsert_user("chart-sample-revision")
        db.flush()
        for value,revision in ((80,current),(50,current-timedelta(hours=1))):
            repo.save_metric_samples([MetricSample(user_id="chart-sample-revision",metric="readiness",timestamp=observed,value=value,unit="score",
                source_field="rdnsScore",qualification="vendor_reference",revision_at=revision)])
        rows=repo.metric_samples("chart-sample-revision","readiness",observed,observed)
        assert len(rows)==1 and rows[0].value==80
        assert rows[0].revision_at==current.replace(tzinfo=None)


def test_all_withdrawn_readiness_revision_removes_old_components_and_blocks_resurrection():
    from vitalis.adapters.zepp.parser import ZeppParser
    observed=datetime(2026,10,7,tzinfo=timezone.utc)
    old={"eventType":"readiness","timestamp":int((observed+timedelta(hours=1)).timestamp()*1000),
        "value":{"timestamp":int(observed.timestamp()*1000),"rdnsScore":80,"sleepHRV":60}}
    withdrawn={"eventType":"readiness","timestamp":int((observed+timedelta(hours=2)).timestamp()*1000),
        "value":{"timestamp":int(observed.timestamp()*1000),"rdnsScore":255,"sleepHRV":255}}
    user="readiness-withdrawn-synthetic"
    with session_scope() as db:
        repo=HealthRepository(db)
        repo.upsert_user(user)
        db.flush()
        for entry in (old,withdrawn,old):
            payload={"items":[entry]}
            daily=ZeppParser.parse_daily_metrics(payload,"UTC")
            samples=ZeppParser.parse_readiness_samples(payload,"UTC")
            for row in daily+samples:
                row.user_id=user
            repo.replace_readiness(user,ZeppParser.parse_readiness_revisions(payload,"UTC"),daily,samples,timezone_name="UTC")
        assert repo.daily_metrics(user,observed.date(),observed.date())==[]
        assert repo.metric_samples(user,"readiness",observed,observed)==[]


def test_sync_readiness_alias_without_revision_cannot_resurrect_withdrawn_value():
    from types import SimpleNamespace

    from vitalis.adapters.zepp.fetcher import FetchedRecord, RawRecord
    from vitalis.adapters.zepp.sync_manager import SyncManager

    observed = datetime(2026, 10, 7, tzinfo=timezone.utc)
    anchor = int(observed.timestamp() * 1000)
    user_id = "readiness-withdrawn-alias-synthetic"
    manager = SyncManager(SimpleNamespace(timezone_name="UTC"))
    events = [
        {"eventType": "readiness", "timestamp": anchor + 3_600_000,
         "value": {"timestamp": anchor, "rdnsScore": 80}},
        {"eventType": "readiness", "timestamp": anchor + 7_200_000,
         "value": {"timestamp": anchor, "rdnsScore": 255}},
        {"eventType": "readiness", "value": {"timestamp": anchor, "watchScore": 80}},
    ]
    # Separate transactions model independently completed sync requests.
    for index, event in enumerate(events):
        with session_scope() as db:
            repo = HealthRepository(db)
            repo.upsert_user(user_id)
            db.flush()
            record = FetchedRecord(RawRecord(
                stream="daily_summary", source_key="daily-summary:synthetic",
                start_utc=observed, end_utc=observed + timedelta(days=1),
                payload={"items": [event]},
            ))
            manager._write_stream_result(record, repo, SimpleNamespace(id=user_id))
            rows = repo.daily_metrics(user_id, observed.date(), observed.date(), "readiness")
            assert [row.value for row in rows] == ([80] if index == 0 else [])
            samples = repo.metric_samples(user_id, "readiness", observed, observed)
            assert [row.value for row in samples] == ([80] if index == 0 else [])
