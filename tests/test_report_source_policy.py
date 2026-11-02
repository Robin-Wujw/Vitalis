"""Synthetic-only report source policy; raw query/chart inputs remain intact."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from vitalis.application.report_sources import report_source_view
from vitalis.intelligence.contracts import (
    EnergyObservation,
    Provenance,
    SampleWindowSummary,
)
from vitalis.intelligence.profile import RawDailyProfile, SeriesPoint
from vitalis.intelligence.readiness import select_analysis_stream

DAY = date(2026, 10, 13)


def point(metric="sleep_hrv", value=47, *, scope="user_summary", device=None, source="zepp", day=DAY, unit="ms"):
    return SeriesPoint(metric, value, unit, day, day, source, scope, device)


def profile(**kwargs):
    return RawDailyProfile(user_id="synthetic-source-policy", day=DAY,
        as_of=datetime(2026, 10, 14, tzinfo=UTC), timezone_name="UTC", **kwargs)


def workout(source, identifier, load=12, duration=30):
    return {"source": source, "workout_id": identifier, "local_day": DAY,
        "started_at": datetime(2026, 10, 13, 8, tzinfo=UTC),
        "detail": {"strength_sets": [{"repetitions": 8}]},
        "data": {"duration": duration, "load": load, "calories": 100}}


def test_account_values_keep_contributing_device_without_mutating_raw():
    points = [point(value=20, scope="device", device="A"),
        point(value=80, scope="device", device="B"),
        point(value=47, scope="user_fused", device="B"),
        point(value=99, scope="unknown"), point(value=100, source="other")]
    raw = profile(series={"sleep_hrv": points}, chart_samples={"sleep_hrv": points},
        chart_band_records=[{"kind": "activity", "date": DAY, "data": {"source": "zepp", "source_scope": "device", "device_id": "A", "steps": 20}}],
        chart_inputs={"annotations": [{"text": "synthetic"}]}, chart_naps={DAY: []}, chart_baselines={"kept": []})
    before = deepcopy(raw)
    result = report_source_view(raw)
    assert result.series["sleep_hrv"] == [points[2]]
    assert result.facts["sleep_hrv"][0].provenance.device_id == "B"
    assert raw == before
    for name in ("chart_samples", "chart_band_records", "chart_inputs", "chart_naps", "chart_baselines"):
        assert getattr(result, name) == getattr(raw, name)
    assert result is not raw and result.series is not raw.series


def test_missing_current_account_never_falls_back_to_device_or_unknown():
    raw = profile(series={"sleep_hrv": [point(day=DAY-timedelta(days=1)),
        point(value=80, scope="device", device="A"), point(value=99, scope="unknown")]})
    result = report_source_view(raw)
    selected, provenance = select_analysis_stream(result.series["sleep_hrv"], DAY-timedelta(days=7), DAY, metric="sleep_hrv")
    assert all(item.day < DAY for item in selected)
    assert provenance["source_scope"] == "user_summary"
    assert "sleep_hrv" not in result.facts
    assert report_source_view(profile(series={"sleep_hrv": raw.series["sleep_hrv"][1:]})).series.get("sleep_hrv", []) == []


def test_explicit_merged_band_survives_but_unverified_device_does_not():
    merged = {"date": DAY, "source": "zepp", "source_scope": "device", "device_id": "A", "isMerged": 1,
        "sleep_duration": 450, "bedtime": "23:00", "wake_time": "07:00", "deep_sleep": 60}
    other = {"date": DAY, "source": "zepp", "source_scope": "device", "device_id": "B", "sleep_duration": 300}
    raw = profile(sleep_by_day={DAY: other}, chart_band_records=[{"date": DAY, "kind": "sleep", "data": merged}],
        activity_by_day={DAY: {"source": "zepp", "source_scope": "device", "device_id": "B", "steps": 9999}})
    result = report_source_view(raw)
    assert result.sleep_by_day[DAY]["sleep_duration"] == 450
    assert result.sleep_by_day[DAY]["source_scope"] == "user_fused"
    assert result.sleep_by_day[DAY]["device_id"] == "A"
    assert result.series["sleep_duration"][0].value == 450
    assert result.series["sleep_duration"][0].source_scope == "user_fused"
    assert result.activity_by_day == {}
    assert raw.sleep_by_day[DAY] == other and merged["source_scope"] == "device"
    for unverified in (0, "1", "true", None):
        candidate = {**merged, "isMerged": unverified}
        assert report_source_view(profile(sleep_by_day={DAY: candidate})).sleep_by_day == {}


def test_zepp_workouts_rebuild_mixed_training_without_dropping_feedback():
    zepp = workout("zepp", "z")
    other = workout("other", "o", load=900, duration=500)
    feedback = {("zepp", "z"): [{"session_rpe": 7}]}
    raw = profile(workouts=[zepp, other], feedback_by_workout=feedback,
        training_by_day={DAY: {"source": "canonical_workouts", "total_load": 912, "total_duration": 530, "workout_count": 2}},
        training_history_coverage={"verified_days": [DAY.isoformat()]})
    result = report_source_view(raw)
    assert result.workouts == [zepp]
    assert result.workouts[0]["detail"] == zepp["detail"]
    assert result.feedback_by_workout == feedback
    assert result.training_by_day[DAY]["total_load"] == 12
    assert result.training_by_day[DAY]["total_duration"] == 30
    assert result.training_by_day[DAY]["workout_count"] == 1
    assert result.series["training_load"][0].value == 12
    assert raw.training_by_day[DAY]["total_load"] == 912
    assert report_source_view(profile(workouts=[workout("zepp", "missing", load=None)])).training_by_day[DAY]["total_load"] is None


def test_precomputed_energy_and_sample_windows_cannot_leak_device_facts():
    device = Provenance(source="zepp", source_scope="device", device_id="A")
    account = Provenance(source="zepp", source_scope="user_summary")
    def energy(provenance, value):
        return EnergyObservation(metric="calories", value=value, unit="kcal", observed_at=DAY,
            provenance=provenance, role="unspecified", source_field="daily.calories")
    raw = profile(heart_rate_samples=[point("heart_rate", 70, scope="device", device="A", unit="bpm")],
        energy_observations=[energy(device, 9999), energy(account, 200)],
        sample_window_summaries={"heart_rate": SampleWindowSummary(metric="heart_rate", unit="bpm", provenance=device, sample_count=60, observed_minutes=60),
            "stress": SampleWindowSummary(metric="stress", unit="score", provenance=account, sample_count=60, observed_minutes=60)})
    result = report_source_view(raw)
    assert result.heart_rate_samples == []
    assert [item.value for item in result.energy_observations] == [200]
    assert set(result.sample_window_summaries) == {"stress"}


def test_ambiguous_band_account_results_abstain_and_missing_quality_is_local():
    rows = [{"date": DAY, "kind": "sleep", "data": {"source": "zepp", "source_scope": "user_fused",
        "device_id": device, "sleep_duration": duration}} for device, duration in (("A", 450), ("B", 300))]
    raw = profile(chart_band_records=rows, series={"hybrid_charge": [point("hybrid_charge", 65, unit="score")]})
    result = report_source_view(raw)
    assert result.sleep_by_day == {}
    assert result.facts["hybrid_charge"][0].value == 65
    assert "sleep_duration" in result.data_quality.missing_required_signals
    assert result.report_context["source_policy"]["device_fallback"] is False
    assert report_source_view(result) == result


def test_as_of_rejects_future_or_late_fetched_accounts_and_workouts():
    cutoff = datetime(2026, 10, 13, 12, tzinfo=UTC)
    eligible = point()
    future = replace(point(value=99), observed_at=cutoff + timedelta(minutes=1))
    late = replace(point(value=98), fetched_at=cutoff + timedelta(minutes=1))
    future_workout = {**workout("zepp", "future"), "started_at": cutoff + timedelta(minutes=1)}
    raw = replace(profile(series={"sleep_hrv": [eligible, future, late]}, workouts=[future_workout]), as_of=cutoff)
    result = report_source_view(raw)
    assert result.series["sleep_hrv"] == [eligible]
    assert result.workouts == [] and result.training_by_day == {}
    assert len(raw.series["sleep_hrv"]) == 3 and len(raw.workouts) == 1


def test_weekly_monthly_and_body_signals_use_account_history_only():
    from vitalis.intelligence.readiness import body_signal_inputs
    from vitalis.intelligence.signals import build_period_signals
    account = [point("sleep_rhr", 65 if offset < 2 else 50 + (offset % 2) * 4,
        day=DAY-timedelta(days=offset), unit="bpm") for offset in range(62)]
    devices = [replace(item, value=20, source_scope="device", device_id="A") for item in account]
    view = report_source_view(profile(series={"sleep_rhr": devices + account}))
    for days in (7, 30):
        states = build_period_signals(view, DAY-timedelta(days=days-1), DAY)["sleep_rhr"]
        assert len(states) == 1 and states[0].source_scope == "user_summary"
        assert states[0].effective_days == days
    assert body_signal_inputs(view)["resting_hr"]["direction"] == "above"
    sparse = report_source_view(profile(series={"sleep_rhr": devices + account[:2]}))
    assert body_signal_inputs(sparse)["resting_hr"]["status"] == "INSUFFICIENT"


def test_merged_activity_observed_zero_and_independent_zepp_training_summary_survive():
    activity = {"source": "zepp", "source_scope": "unknown", "is_merged": True, "steps": 0,
        "observed_fields": ["steps"], "resting_hr": 0}
    sleep = {"source": "zepp", "source_scope": "user_summary", "sleep_duration": 450,
        "rem_sleep": 0, "wake_count": 0, "awake": 0}
    training = {"source": "zepp", "total_load": 0, "total_duration": 0, "workout_count": 0}
    result = report_source_view(profile(activity_by_day={DAY: activity}, sleep_by_day={DAY: sleep}, training_by_day={DAY: training}))
    assert result.facts["steps"][0].value == 0
    assert result.facts["rem_sleep"][0].value == 0 and result.facts["sleep_wake_count"][0].value == 0
    assert "resting_hr" not in result.facts and "awake" not in result.facts
    assert result.training_by_day[DAY]["total_load"] == 0
