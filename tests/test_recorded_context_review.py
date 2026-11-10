"""Regression cases for supplemental context publication and time semantics."""
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from vitalis.application.report_sources import report_source_view
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.recorded_context import build_recorded_context
from vitalis.intelligence.report_recorded_context import recorded_context_sections
from vitalis.intelligence.public_reports import to_public_report_view

DAY = date(2026, 11, 8)
AS_OF = datetime(2026, 11, 7, 23, 30, tzinfo=timezone.utc)


def _point(metric="sleep_latency_minutes", value=12, unit="min", **kwargs):
    return SeriesPoint(metric, value, unit, DAY, DAY, "zepp", "user_fused",
        fetched_at=AS_OF, qualification="verified_timing_identity", **kwargs)


def _raw():
    return RawDailyProfile(user_id="synthetic", day=DAY, as_of=AS_OF, timezone_name="Asia/Shanghai")


def test_date_only_sleep_fact_is_visible_before_utc_midnight():
    raw = _raw()
    raw.series = {"sleep_latency_minutes": [_point()]}
    assert build_recorded_context(raw, DAY).metrics[0].latest_value == 12


def test_report_projection_normalizes_json_fetch_time_without_promoting_qualification():
    raw = _raw()
    raw.sleep_by_day[DAY] = {"source": "zepp", "source_scope": "user_fused",
        "sleep_latency_minutes": 12, "fetched_at": AS_OF.isoformat()}
    context = build_recorded_context(report_source_view(raw), DAY)
    assert context.metrics[0].latest_fetched_at == AS_OF
    assert context.metrics[0].qualification == "unspecified"
    raw.sleep_by_day[DAY]["fetched_at"] = "not-a-timestamp"
    assert build_recorded_context(report_source_view(raw), DAY).metrics == []


def test_sparse_body_display_uses_observed_local_day_and_keeps_sources_distinct():
    raw = _raw()
    observed = datetime(2026, 11, 7, 17, tzinfo=timezone.utc)
    weight = replace(_point("weight", 70, "kg"), observed_at=observed,
        qualification="vendor_recorded", source_scope="user_summary")
    raw.series = {"weight": [weight, replace(weight, source_scope="user_fused", value=72)]}
    context = build_recorded_context(raw, DAY).model_dump(mode="json")
    lines = recorded_context_sections(context)[0]["facts"]
    assert all("2026-11-08" in line for line in lines)
    assert any("账户汇总" in line for line in lines)
    assert any("账户融合" in line for line in lines)


def test_conflicting_same_instant_records_do_not_choose_by_input_order():
    raw = _raw()
    raw.series = {"sleep_latency_minutes": [_point(), _point(value=30)]}
    assert build_recorded_context(raw, DAY).metrics == []


def test_goal_retains_actual_qualification_and_field_in_public_view():
    raw = _raw()
    raw.series = {
        "step_goal": [replace(_point("step_goal", 8000, "steps"), qualification="vendor_user_target", source_field="stepGoal")],
        "steps": [replace(_point("steps", 2000, "steps"), qualification="vendor_selected", source_field="activity.steps")],
        "vendor_tsb": [replace(_point("vendor_tsb", -6, "vendor_load"), qualification="vendor_estimate_unvalidated")],
    }
    context = build_recorded_context(raw, DAY).model_dump(mode="json")
    from tests.test_public_reports import _daily

    payload = _daily()
    payload["features"]["recorded_context"] = context
    public = to_public_report_view(payload, "daily")
    activity = next(block for block in public.blocks if block.section_id == "activity")
    goal = next(item for item in activity.facts if item["metric"] == "step_goal")
    assert goal["actual_qualification"] == "vendor_selected"
    assert goal["actual_source_field"] == "activity.steps"
    assert goal["qualification"] == "vendor_user_target"
    assert goal["actual_value"] == 2000
    assert goal["calendar_semantics"] == "activity_day"
    assert datetime.fromisoformat(goal["latest_fetched_at"].replace("Z", "+00:00")) == AS_OF
    training = next(block for block in public.blocks if block.section_id == "training")
    vendor = next(item for item in training.facts if item["metric"] == "vendor_tsb")
    assert vendor["value"] == -6 and vendor["decision_role"] == "no"


def test_morning_public_view_keeps_sleep_metadata_and_removes_facts_only_comparisons():
    raw = _raw()
    raw.series = {"sleep_latency_minutes": [_point()]}
    context = build_recorded_context(raw, DAY).model_dump(mode="json")
    context["metrics"][0].update(previous_average=20, change=-8)
    from tests.test_public_reports import _daily

    from vitalis.intelligence.morning_briefing import MorningBriefingEngine
    payload = MorningBriefingEngine().build_payload(_daily())
    payload["report_context"]["recorded_context"] = context
    payload["delivery_metadata"] = {"facts_only": True}
    public = to_public_report_view(payload, "morning")
    sleep = next(block for block in public.blocks if block.section_id == "sleep")
    fact = next(item for item in sleep.facts if item["metric"] == "sleep_latency_minutes")
    assert fact["value"] == 12 and fact["qualification"] == "verified_timing_identity"
    assert "change" not in fact and "previous_average" not in fact
    assert public.report_context["recorded_context"]["metrics"][0].get("change") is None


def test_weekly_public_context_uses_saved_average_and_keeps_comparison_metadata():
    raw = _raw()
    raw.series = {"sleep_latency_minutes": [_point()]}
    context = build_recorded_context(raw, DAY).model_dump(mode="json")
    context["metrics"][0].update(average=16, latest_value=12, previous_average=20, change=-4,
        previous_available_days=6, previous_sample_count=6, previous_coverage_ratio=6/7,
        reference_start="2026-10-26", reference_end="2026-11-01")
    payload = {"analysis_run_id": "synthetic-week", "user_id": "synthetic", "date": "2026-11-08",
        "period": "weekly", "period_start": "2026-11-02", "period_end": "2026-11-08",
        "report_context": {"as_of": AS_OF.isoformat()}, "facts": {"recorded_context": context}, "metrics": []}
    public = to_public_report_view(payload, "weekly")
    fact = next(item for block in public.blocks for item in block.facts if item["metric"] == "sleep_latency_minutes")
    assert fact["value"] == 16 and fact["latest_value"] == 12
    assert fact["aggregation"] == "mean_per_observed_day"
    assert fact["previous_available_days"] == 6 and fact["change"] == -4


@pytest.mark.parametrize("kind", ["morning", "daily", "evening", "weekly"])
@pytest.mark.parametrize("target", ["markdown", "html"])
def test_vendor_load_stays_queryable_without_cluttering_readable_reports(kind, target):
    from tests.test_public_reports import _daily
    from vitalis.intelligence.evening_briefing import EveningBriefingEngine
    from vitalis.intelligence.morning_briefing import MorningBriefingEngine
    from vitalis.intelligence.report_rendering import render_report
    from vitalis.intelligence.weekly_briefing import WeeklyBriefingEngine

    raw = _raw()
    vendor_metrics = ("vendor_atl", "vendor_ctl", "vendor_tsb",
                      "vendor_atl_total", "vendor_ctl_total", "vendor_tsb_total")
    raw.series = {metric: [replace(_point(metric, value, "vendor_load"),
        qualification="vendor_estimate_unvalidated")]
        for metric, value in zip(vendor_metrics, (32, 30, -2, 39, 36, -3))}
    raw.series["weight"] = [replace(_point("weight", 70, "kg"), qualification="vendor_recorded")]
    context = build_recorded_context(raw, DAY).model_dump(mode="json")
    daily = _daily()
    daily["features"]["recorded_context"] = context
    if kind == "weekly":
        saved = {"analysis_run_id": "synthetic-week", "user_id": "synthetic", "date": "2026-11-08",
            "period": "weekly", "period_start": "2026-11-02", "period_end": "2026-11-08",
            "report_context": {"as_of": AS_OF.isoformat()}, "facts": {"recorded_context": context}, "metrics": []}
        document = WeeklyBriefingEngine().build(saved)
    else:
        saved = daily
        if kind == "morning":
            document = MorningBriefingEngine().build_payload(daily)
        elif kind == "evening":
            document = EveningBriefingEngine().build_payload(daily)
        else:
            document = daily
    rendered = render_report(document, target=target).content
    assert ("最近记录体重" if kind in {"daily", "weekly"} else "睡眠时长") in rendered
    assert all(term not in rendered for term in ("ATL", "CTL", "TSB", "Zepp 负荷参考", "基础口径", "总口径"))
    public = to_public_report_view(saved, "weekly" if kind == "weekly" else "daily")
    retained = {fact["metric"]: fact for block in public.blocks for fact in block.facts
                if fact.get("metric") in vendor_metrics}
    assert retained.keys() == set(vendor_metrics)
    assert retained["vendor_tsb"]["value"] == -2
    assert all(fact["decision_role"] == "no" for fact in retained.values())
