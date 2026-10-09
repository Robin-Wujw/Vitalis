"""Public projections carry the same qualified facts to every consumer."""

from datetime import date, datetime, timezone

import pytest

from vitalis.intelligence.public_reports import PublicReportView, to_public_report_view


AS_OF = datetime(2026, 10, 7, 13, 20, tzinfo=timezone.utc)


def _daily():
    return {
        "analysis_run_id": "synthetic-public-run", "user_id": "synthetic-public-user",
        "date": "2026-10-07", "generated_at": AS_OF.isoformat(),
        "report_context": {
            "as_of": AS_OF.isoformat(), "timezone": "Asia/Shanghai", "source_mode": "mock",
            "report_state": {"state": "current"},
            "signals": {"sleep_duration": [{
                "metric": "sleep_duration", "label": "睡眠时长", "value": 420,
                "unit": "min", "source": "zepp", "source_scope": "device", "device_id": "watch-a",
                "status": "AVAILABLE", "calendar_semantics": "sleep_day", "observed_at": "2026-10-07",
                "as_of": AS_OF.isoformat(), "sample_count": 1, "distinct_days": 1,
                "expected_days": 1, "coverage_ratio": 1.0, "baseline": 440,
                "deviation": -20, "baseline_distinct_days": 28,
            }]},
        },
        "data_quality": {"status": "SUFFICIENT"},
        "facts": {}, "features": {"sleep": {"duration_minutes": 420}},
        "events": [], "trends": [], "decision": {"action": "INSUFFICIENT_DATA"},
    }


def test_public_report_groups_qualified_facts_with_provenance():
    report = to_public_report_view(_daily(), "daily")
    assert isinstance(report, PublicReportView)
    assert report.analysis_run_id == "synthetic-public-run"
    assert 1 <= len(report.blocks) <= 5
    fact = next(fact for block in report.blocks for fact in block.facts if fact.get("metric") == "sleep_duration")
    assert fact["value"] == 420 and fact["unit"] == "min"
    assert fact["source"] == "zepp" and fact["device_id"] == "watch-a"
    assert fact["calendar_semantics"] == "sleep_day"
    assert fact["coverage_ratio"] == 1 and fact["baseline"] == 440
    assert report.as_of == AS_OF
    assert report.source_mode == "mock"
    assert report.model_dump(mode="json")["period_start"] == "2026-10-07"


def test_public_fact_exposes_freshness_coverage_and_shadow_only_role():
    payload = _daily()
    payload["report_context"]["signals"]["spo2"] = [{
        **_signal("spo2", 96, "%", decision_role="shadow", freshness="PARTIAL",
                  target_day_coverage={"status": "PARTIAL", "sample_count": 4, "distinct_days": 1,
                                      "expected_days": 1, "coverage_ratio": 1.0}),
    }]
    report = to_public_report_view(payload, "daily")
    fact = next(fact for block in report.blocks for fact in block.facts if fact.get("metric") == "spo2")
    assert fact["freshness"] == "PARTIAL"
    assert fact["coverage"]["sample_count"] == 4
    assert fact["shadow_only"] is True
    assert fact["decision_role_label"] == "shadow-only"


def test_missing_report_dates_cannot_be_replaced_with_current_clock():
    with pytest.raises(ValueError, match="date"):
        to_public_report_view({"user_id": "synthetic", "features": {}}, "daily")


def test_partial_late_facts_only_report_has_no_interpretation_or_actions():
    payload = _daily()
    payload["period"] = "evening"
    payload["period_start"] = payload["period_end"] = payload["date"]
    payload["findings"] = ["synthetic interpretation"]
    payload["suggestions"] = ["synthetic training suggestion"]
    payload["report_context"]["delivery_metadata"] = {"facts_only": True, "partial": True, "late": True}
    report = to_public_report_view(payload, "evening")
    assert report.facts_only is True and report.late is True
    assert not report.findings and not report.suggestions
    assert all(not block.interpretation and block.action is None for block in report.blocks)
    assert any(fact.get("value") == 420 for block in report.blocks for fact in block.facts)


def test_public_report_retains_same_last_good_identity_and_state():
    payload = _daily()
    payload["report_context"]["report_state"] = {
        "state": "queued", "job_id": "synthetic-job", "next_action": "wait_for_analysis",
    }
    report = to_public_report_view(payload, "daily")
    assert report.state == "queued"
    assert report.report_context["report_state"]["job_id"] == "synthetic-job"
    assert report.analysis_run_id == payload["analysis_run_id"]


def _signal(metric, value, unit, *, days=1, expected=1, status="AVAILABLE", **updates):
    result = {
        "metric": metric, "value": value, "unit": unit,
        "source": "zepp", "source_scope": "device", "device_id": "watch-a",
        "observed_at": "2026-10-07", "as_of": AS_OF.isoformat(),
        "sample_count": days, "distinct_days": days, "expected_days": expected,
        "coverage_ratio": days / expected, "status": status,
        "calendar_semantics": "sleep_day" if metric.startswith("sleep") else "activity_day",
        "baseline": None, "deviation": None,
    }
    result.update(updates)
    return result


def test_saved_signals_take_precedence_and_preserve_unknown_gaps_and_multiple_sources():
    payload = _daily()
    payload["report_context"]["signals"].update({
        "steps": [_signal("steps", None, "steps", days=0, status="UNKNOWN")],
        "sleep_hrv": [
            _signal("sleep_hrv", 64, "ms", baseline=60, deviation={"percent": 6.7, "direction": "above"}),
            _signal("sleep_hrv", 51, "ms", device_id="strap-b", source="other_device", status="PARTIAL"),
        ],
    })
    payload["features"]["activity"] = {"steps": {"value": 99999}}
    report = to_public_report_view(payload, "daily")
    facts = [item for block in report.blocks for item in block.facts]
    steps = [item for item in facts if item.get("metric") == "steps"]
    assert len(steps) == 1 and steps[0]["value"] is None and steps[0]["status"] == "UNKNOWN"
    recovery = next(block for block in report.blocks if block.section_id == "recovery")
    streams = [item for item in recovery.facts if item.get("metric") == "sleep_hrv"]
    assert {item["device_id"] for item in streams} == {"watch-a", "strap-b"}
    assert recovery.status == "PARTIAL"
    assert len(recovery.coverage["sleep_hrv"]) == 2


def test_saved_deviations_produce_cross_domain_findings_without_recalculating():
    payload = _daily()
    payload["report_context"]["signals"] = {
        "sleep_duration": [_signal("sleep_duration", 420, "min", baseline=430, deviation={"percent": -17.5, "direction": "below"})],
        "steps": [_signal("steps", 8300, "steps", baseline=8000, deviation={"percent": 22.2, "direction": "above"})],
        "sleep_hrv": [_signal("sleep_hrv", 65, "ms", baseline=60, deviation={"percent": 4.2, "direction": "above"})],
        "training_load": [_signal("training_load", 80, "load", status="INSUFFICIENT", deviation={"percent": 999, "direction": "above"})],
    }
    report = to_public_report_view(payload, "daily")
    assert any("17.5%" in item and "睡眠" in item for item in report.findings)
    assert any("22.2%" in item and "步数" in item for item in report.findings)
    assert any("4.2%" in item and "HRV" in item for item in report.findings)
    assert all("999" not in item for item in report.findings)
    assert 3 <= len(report.blocks) <= 5


def test_facts_only_clears_metric_and_exercise_comparisons_and_factual_headline():
    from vitalis.intelligence.evening_briefing import EveningBriefingEngine
    from vitalis.intelligence.report_rendering import render_report
    from tests.test_report_content import synthetic_daily_fixture

    payload = synthetic_daily_fixture()
    payload["report_context"]["delivery_metadata"] = {
        "facts_only": True, "partial": True, "late": True,
        "delivered_as_of": AS_OF.isoformat(), "missing_signals": ["workouts"],
    }
    payload["features"]["training"]["strength"] = {"recent_sessions": [{
        "date": payload["date"], "source": "zepp", "workout_id": "synthetic-strength",
        "explicit_exercises": [{"exercise_name": "弯举", "sets": 3, "repetitions": 10, "weight_kg": 10}],
        "comparisons": [{"exercise_name": "弯举", "comparable": True,
                         "delta_total_repetitions": 6, "reference_workout_date": "2026-09-02"}],
    }]}
    briefing = EveningBriefingEngine().build(payload)
    report = to_public_report_view(briefing, "evening")
    assert report.facts_only and report.partial and report.late
    assert not report.findings and not report.suggestions
    assert "增加" not in report.title and "优先" not in report.title
    assert all(not block.comparisons and not block.interpretation and block.action is None for block in report.blocks)
    assert all(fact.get("baseline") is None and fact.get("deviation") is None for block in report.blocks for fact in block.facts)
    assert all(not exercise.get("comparison") for block in report.blocks for workout in block.workouts for exercise in workout.get("exercises", []))
    for target in ("markdown", "html"):
        content = render_report(report, target).content
        assert "延迟" in content and "部分" in content
        assert "总次数增加" not in content and "较个人" not in content
        assert "主要安排" not in content and "下次训练重点" not in content


def test_period_uses_saved_series_summary_and_does_not_publish_target_day_shadow():
    from tests.test_report_content import synthetic_period_fixture

    payload = synthetic_period_fixture("weekly")
    payload["report_context"]["signals"] = {
        "sleep_duration": [_signal("sleep_duration", 412, "min", days=6, expected=7, aggregation="median", period_start=payload["period_start"], period_end=payload["period_end"], baseline=400, deviation={"percent": 3.0, "direction": "above"})],
        "vo2_max": [_signal("vo2_max", 47.1, "ml/kg/min", days=4, expected=7, aggregation="latest", baseline=46, deviation={"percent": 2.4, "direction": "above"}, status="PARTIAL")],
        "pai": [_signal("pai", 115, "score", days=7, expected=7, aggregation="median")],
        "lactate_threshold": [_signal("lactate_threshold", 165, "bpm", days=2, expected=7, aggregation="latest", status="PARTIAL")],
    }
    payload["open_health_period_summary"] = {"target_day": "2026-10-07", "shadow_only": True, "training_state": "FAKE-PERIOD-SHADOW"}
    report = to_public_report_view(payload, "weekly")
    sleep = next(fact for block in report.blocks for fact in block.facts if fact.get("metric") == "sleep_duration")
    assert sleep["value"] == 412 and sleep["distinct_days"] == 6 and sleep["expected_days"] == 7
    assert sleep["aggregation"] == "median"
    assert {fact.get("metric") for block in report.blocks for fact in block.facts} >= {"vo2_max", "pai", "lactate_threshold"}
    assert "FAKE-PERIOD-SHADOW" not in report.model_dump_json()
    assert report.period_start == date.fromisoformat(payload["period_start"])


def test_period_projects_long_term_trends_and_next_experiment_without_inventing_actions():
    from tests.test_report_content import synthetic_period_fixture

    payload = synthetic_period_fixture("weekly")
    payload["report_context"]["long_term_trends"] = [{
        "metric": "steps", "metric_label": "步数", "window_days": 28,
        "source": "zepp", "source_scope": "user_fused", "unit": "steps",
        "status": "AVAILABLE", "comparison_available": True,
        "current_distinct_days": 26, "expected_days": 28, "coverage_ratio": 26 / 28,
        "previous_distinct_days": 25, "previous_expected_days": 28,
        "previous_coverage_ratio": 25 / 28, "current_value": 8200,
        "previous_median": 7800, "change_percent": 5.1,
        "change_absolute": 400, "period_start": "2026-08-01",
        "period_end": "2026-08-28", "as_of": AS_OF.isoformat(),
    }]
    payload["report_context"]["product_summary"] = {
        "as_of": "2026-10-07", "next_experiment": {
            "title": "保持相同跑量并记录次日疲劳", "accepted": False, "source": "user",
        },
    }
    report = to_public_report_view(payload, "weekly")
    facts = [fact for block in report.blocks for fact in block.facts]
    trend = next(fact for fact in facts if fact.get("metric") == "long_term_trend_steps_28")
    experiment = next(fact for fact in facts if fact.get("metric") == "next_experiment")
    assert trend["value"] == 8200 and trend["baseline"] == 7800
    assert trend["coverage_ratio"] == pytest.approx(26 / 28)
    assert "近 28 日" in trend["text"] and "2026-08-01—2026-08-28" in trend["text"]
    assert "待用户确认" in experiment["text"]


def test_period_training_windows_and_associations_keep_denominators_and_q():
    from vitalis.intelligence.report_rendering import render_report
    from tests.test_report_content import synthetic_period_fixture

    payload = synthetic_period_fixture("monthly")
    payload["report_context"]["training_response_summary"] = {
        "response_count": 4, "as_of": AS_OF.isoformat(),
        "window_summaries": [{"day_offset": offset, "observed": 2, "expected": 3,
                              "not_due_count": 1, "missing_count": 1, "confounded_count": 1,
                              "coverage": 2 / 3, "as_of": AS_OF.isoformat()}
                             for offset in range(4)],
    }
    payload["report_context"]["personal_associations"] = [{
        "id": "synthetic-association", "status": "AVAILABLE", "summary": "睡眠与次日活动呈正向观测关联",
        "predictor_metric": "sleep_duration", "outcome_metric": "steps",
        "predictor_day_semantics": "sleep_day", "outcome_day_semantics": "activity_day",
        "paired_days": 42, "sample_count": 36, "expected_pair_days": 59, "coverage_ratio": 42 / 59,
        "coefficient": 0.42, "q_value": 0.04, "fdr_method": "BENJAMINI_HOCHBERG",
        "confounded_ratio": 0.14, "confidence_label": "中等", "window_days": 60,
        "as_of": AS_OF.isoformat(), "association_only": True,
    }]
    report = to_public_report_view(payload, "monthly")
    assert any(item.get("q_value") == 0.04 for block in report.blocks for item in block.comparisons)
    for target in ("markdown", "html"):
        content = render_report(report, target).content
        assert all(f"T+{offset}" in content for offset in range(4))
        assert "2/3" in content and "尚未到达" in content and "混杂" in content
        assert "42" in content and "59" in content and "0.04" in content
        assert "不表示因果" in content
