"""Focused regressions: show usable facts without inserting chart walls."""
from datetime import datetime, timezone

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.domain import MetricSample
from vitalis.intelligence.evening_briefing import EveningBriefingEngine
from vitalis.intelligence.report_rendering import render_report
from tests.test_report_content import synthetic_daily_fixture


def test_sample_only_stress_survives_reading_projection_without_chart():
    daily=synthetic_daily_fixture()
    daily['features']['activity']['stress_summary']=[]
    daily['features']['activity']['stress']={'average':42,'sample_count':2,'observed_minutes':2,'truncated':False}
    report=EveningBriefingEngine().build(daily)
    for target in ('html','markdown'):
        text=render_report(report,target).content
        assert '已采样时段压力均值 42' in text
        assert '记录曲线' not in text


def test_unchanged_samples_do_not_rebuild_daily_features(monkeypatch):
    sample=MetricSample(user_id='unchanged-feature-synthetic',metric='stress',
        timestamp=datetime(2026,10,7,8,tzinfo=timezone.utc),value=42,unit='score')
    with session_scope() as db:
        repo=HealthRepository(db)
        repo.upsert_user(sample.user_id)
        db.flush()
        repo.save_metric_samples([sample])
        calls=[]
        original=repo.refresh_daily_features
        def tracked(*args,**kwargs):
            calls.append(args)
            return original(*args,**kwargs)
        monkeypatch.setattr(repo,'refresh_daily_features',tracked)
        repo.save_metric_samples([sample])
        assert calls==[]
        repo.save_metric_samples([sample.model_copy(update={'value':60})])
        assert len(calls)==1
