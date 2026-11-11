"""Remaining reference aggregations use qualified saved observations only."""
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest

from vitalis.application.health_analytics import HealthAnalytics
from vitalis.application.ports import HealthDailyMetricRead, HealthMetricRead, HealthWorkoutRead
from tests.test_health_analytics_parity import reader


START, END = date(2026, 1, 1), date(2026, 1, 3)


def sleep_source():
    source = reader()
    source.sleep_records = lambda *a, **k: tuple({"date": day, "data": {
        "source": "zepp", "source_scope": "device", "device_id": device,
        "sleep_duration": duration, "deep_sleep": 60, "rem_sleep": rem,
        "sleep_score": 80, "sleep_latency_minutes": latency}}
        for day, device, duration, rem, latency in ((START, "one", 400, 80, 10),
            (END, "one", 500, 100, 20), (START, "two", 700, None, 0)))
    source.activity_records = lambda *a, **k: tuple({"date": day, "data": {
        "source": "zepp", "source_scope": "device", "device_id": device, "resting_hr": hr}}
        for day, device, hr in ((START, "one", 50), (END, "one", 60), (START, "two", 90),
            (START + timedelta(days=1), "one", 100)))
    return source


def test_sleep_range_has_qualified_totals_latency_and_matching_night_resting_hr():
    result = HealthAnalytics(sleep_source()).sleep("synthetic", START, END)
    totals = {g["device_id"]: g for g in result["summary"]["sleep_duration"]}
    assert totals["one"]["summary"]["total"] == 900
    assert totals["one"]["summary"]["total_qualification"] == "observed_days_only"
    assert totals["one"]["coverage"]["days_with_data"] == 2
    assert totals["one"]["coverage"]["days_requested"] == 3
    assert totals["two"]["summary"]["total"] == 700
    assert len(result["summary"]["rem_sleep"]) == 1
    assert result["summary"]["rem_sleep"][0]["summary"]["total"] == 180
    latency = {g["device_id"]: g["summary"]["mean"] for g in result["summary"]["sleep_latency_minutes"]}
    assert latency == {"one": 15, "two": 0}
    rhr = {g["device_id"]: g for g in result["summary"]["resting_hr"]}
    assert rhr["one"]["summary"]["mean"] == 55
    assert rhr["two"]["summary"]["mean"] == 90
    assert all(g["unit"] == "bpm" and g["source_field"] == "band_data.slp.rhr" for g in rhr.values())
    assert all("total" not in g["summary"] for g in result["summary"]["sleep_score"])


def test_duplicate_sleep_candidates_keep_total_unknown():
    source = sleep_source()
    original = source.sleep_records
    source.sleep_records = lambda *a, **k: (*original(*a, **k), original(*a, **k)[0])
    result = HealthAnalytics(source).sleep("synthetic", START, END)
    group = next(g for g in result["summary"]["sleep_duration"] if g["device_id"] == "one")
    assert group["summary"]["total"] is None
    assert group["summary"]["total_qualification"] == "ambiguous_daily_records"


def test_reported_sleep_latency_survives_missing_bed_window_evidence():
    from zoneinfo import ZoneInfo
    from vitalis.adapters.zepp.parsers.supplemental import sleep_timing

    result = sleep_timing({"is": 12, "st": 1767225600, "ed": 1767250800}, ZoneInfo("UTC"), 400)
    assert result == {"sleep_latency_minutes": 12}
    assert sleep_timing({"is": 0}, ZoneInfo("UTC"), 400) == {"sleep_latency_minutes": 0}
    assert sleep_timing({"is": -1}, ZoneInfo("UTC"), 400) == {}


def threshold_source():
    source = reader()
    rows = [HealthDailyMetricRead(date=day, metric="lactate_threshold_hr", value=value, unit="bpm",
        source="zepp", source_scope="device", device_id=device, source_field="lactateThresholdHr",
        qualification=qualification)
        for day, device, value, qualification in ((START, "one", 160, "vendor_reference"),
            (END, "one", 166, "vendor_reference"), (START, "two", 180, "vendor_reference"),
            (END, "two", 175, "vendor_reference"), (END, "one", 190, "unverified"))]
    source.daily_metrics = lambda _u, start, end, metric=None, **k: tuple(row for row in rows if row.metric == metric)
    source.workout_statistics_workouts = lambda *a, **k: tuple(HealthWorkoutRead("zepp", str(i), {
        "type": "running", "started_at": datetime.combine(day, datetime.min.time(), UTC).isoformat(),
        "vendor_summary_metrics": {"lactate_threshold_hr": {"value": value, "unit": "bpm",
            "source_scope": "workout_summary", "source_field": "lactateThresholdHr", "qualification": "vendor_reported"}}}, None, False)
        for i, day, value in ((1, START, 150), (2, END, 155)))
    return source, rows


def test_threshold_change_partitions_events_and_workout_estimates_and_qualifications():
    source, _ = threshold_source()
    result = HealthAnalytics(source).thresholds("synthetic", START, END)
    groups = result["event_changes"]["lactate_threshold_hr"]
    one = next(g for g in groups if g["device_id"] == "one" and g["qualification"] == "vendor_reference")
    assert one["change"] == 6 and one["from"] == START.isoformat() and one["to"] == END.isoformat()
    assert one["sample_count"] == 2 and one["coverage"]["days_with_data"] == 2
    assert one["coverage"]["days_requested"] == 3
    assert next(g for g in groups if g["qualification"] == "unverified")["change"] is None
    assert next(g for g in groups if g["device_id"] == "two")["change"] == -5
    fallback = result["workout_changes"]["lactate_threshold_hr"]
    assert len(fallback) == 1 and fallback[0]["change"] == 5
    assert fallback[0]["source_scope"] == "workout_summary"


def test_threshold_same_day_endpoint_candidates_are_not_arbitrarily_selected():
    source, rows = threshold_source()
    rows.append(HealthDailyMetricRead(date=END, metric="lactate_threshold_hr", value=170, unit="bpm",
        source="zepp", source_scope="device", device_id="one", source_field="lactateThresholdHr",
        qualification="vendor_reference"))
    result = HealthAnalytics(source).thresholds("synthetic", START, END)
    one = next(g for g in result["event_changes"]["lactate_threshold_hr"]
        if g["device_id"] == "one" and g["qualification"] == "vendor_reference")
    assert one["change"] is None and one["comparison_qualification"] == "ambiguous_endpoint_records"
    assert {row["value"] for row in result["current"]["lactate_threshold_hr"]
        if row["device_id"] == "one" and row["qualification"] == "vendor_reference"} == {166, 170}


@pytest.mark.parametrize("altered", [{"value": "155"}, {"value": None}, {"value": float("nan")},
    {"value": True}, {"value": float("inf")}, {"unit": "unknown"}, {"qualification": "vendor_unit_unverified"}])
def test_workout_threshold_changes_require_finite_numeric_values_and_known_qualifications(altered):
    source, _ = threshold_source()
    records = list(source.workout_statistics_workouts())
    records[-1].data["vendor_summary_metrics"]["lactate_threshold_hr"].update(altered)
    source.workout_statistics_workouts = lambda *a, **k: records
    result = HealthAnalytics(source).thresholds("synthetic", START, END)
    changes = result["workout_changes"]["lactate_threshold_hr"]
    assert not changes or all(group["change"] is None for group in changes)


def test_activity_mode_totals_keep_codes_sources_missing_values_and_overlap_qualification():
    source = reader()
    def activity(device, periods):
        return {"date": START, "data": {"source": "zepp", "source_scope": "device", "device_id": device,
            "band_activity": {"activity_periods": periods}}}
    def period(start, stop, steps, distance, mode=3):
        return {"start_minute": start, "end_minute_inclusive": stop, "duration_minutes": stop-start+1,
            "steps": steps, "distance_meters": distance, "mode_code": mode, "qualification": "vendor_period_unnamed_mode"}
    source.activity_records = lambda *a, **k: (
        activity("one", [period(0, 9, 100, 80), period(10, 19, 200, None), period(20, 29, 0, 0, 99)]),
        activity("two", [period(0, 9, 999, 900)]))
    result = HealthAnalytics(source).daily_summary("synthetic", START, END)
    groups = result["activity_modes"]
    one = next(g for g in groups if g["device_id"] == "one" and g["mode_code"] == 3)
    assert one["period_count"] == 2 and one["minutes"]["total"] == 20 and one["steps"]["total"] == 300
    assert one["distance_meters"]["total"] == 80
    assert one["distance_meters"]["observed_periods"] == 1
    assert one["distance_meters"]["total_qualification"] == "observed_periods_only"
    unknown = next(g for g in groups if g["mode_code"] == 99)
    assert unknown["steps"]["total"] == 0 and "name" not in unknown
    source.activity_records = lambda *a, **k: (activity("one", [period(0, 9, 100, 80), period(5, 14, 200, 160)]),)
    group = HealthAnalytics(source).daily_summary("synthetic", START, START)["activity_modes"][0]
    assert group["minutes"]["total"] is None and group["steps"]["total"] is None
    assert group["minutes"]["total_qualification"] == "overlapping_periods"


def test_charge_daily_endpoints_and_peak_trend_are_distinct_from_mean_trend():
    source = reader()
    @contextmanager
    def samples(_owner, metric, begin, stop):
        rows = []
        if metric == "hybrid_charge":
            for index, values in enumerate(((10, 90), (30, 70), (50, 50))):
                for hour, value in enumerate(values):
                    rows.append(HealthMetricRead(datetime(2026, 1, index+1, hour, tzinfo=UTC), value,
                        "score", "zepp", "device", "one", None, None,
                        source_field="Charge.total", qualification="vendor_reference"))
        yield iter(reversed(rows))
    source.metric_sample_rows = samples
    result = HealthAnalytics(source).metrics("synthetic", "body_charge", START, END)
    group = result["signals"]["hybrid_charge"]["intraday"][0]
    assert group["trend"]["change_per_30_days"] == 0
    assert group["daily_max_trend"]["change_per_30_days"] == -600
    assert group["daily_max_summary"]["mean"] == 70
    first = group["days"][0]
    assert first["first"] == 10 and first["last"] == 90
    assert first["first_observed_at"] == "2026-01-01T00:00:00+00:00"
    assert first["last_observed_at"] == "2026-01-01T01:00:00+00:00"


def test_raw_odi_validity_code_is_projected_without_averaging_or_trend():
    source = reader()
    source.daily_metrics = lambda _u, start, end, metric=None, **k: tuple(HealthDailyMetricRead(
        date=day, metric="spo2_odi_validity_code", value=value, unit="vendor_code", source="zepp",
        source_scope="device", device_id="synthetic", source_field="odi.valid", qualification="vendor_semantics_unverified")
        for day, value in ((START, 0), (END, 99))) if metric == "spo2_odi_validity_code" else ()
    result = HealthAnalytics(source).metrics("synthetic", "spo2", START, END)
    group = result["signals"]["spo2_odi_validity_code"]["daily"][0]
    assert [day["observations"][0]["value"] for day in group["days"]] == [0, 99]
    assert set(group["summary"]) == {"sample_count"}
    assert "trend" not in group and "mean" not in str(group)


def test_charge_conflicting_values_at_same_endpoint_time_stay_unknown():
    from vitalis.intelligence.observation_statistics import grouped_observations

    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [{"timestamp": stamp, "value": value, "source": "zepp", "unit": "score"} for value in (20, 30, 20)]
    day = grouped_observations(rows, start=START, end=START, timezone_name="UTC",
        include_daily_endpoints=True)[0]["days"][0]
    assert day["first"] is None and day["last"] is None
    assert day["first_ambiguous"] is True and day["last_ambiguous"] is True


def test_sleep_same_day_odi_enum_stays_raw():
    source = sleep_source()
    source.daily_metrics = lambda *a, **k: (HealthDailyMetricRead(date=START,
        metric="spo2_odi_validity_code", value=7, unit="vendor_code", source="zepp",
        source_scope="device", device_id="one", source_field="odi.valid", qualification="vendor_semantics_unverified"),)
    result = HealthAnalytics(source).sleep("synthetic", START, END)
    group = result["nights"][0]["same_day_observations"]["overnight_spo2"]["spo2_odi_validity_code"][0]
    assert group["days"][0]["observations"][0]["value"] == 7
    assert "mean" not in group["summary"] and "trend" not in group


def test_sleep_and_threshold_aggregations_are_exposed_through_read_only_http(client, issue_token, monkeypatch):
    from vitalis.entrypoints.api.routes import health_analytics

    headers = {"Authorization": "Bearer " + issue_token("qualified-aggregate-api", ["read"])}
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(sleep_source()))
    result = client.get("/api/health/sleep?from=2026-01-01&to=2026-01-03", headers=headers)
    assert result.status_code == 200
    assert result.json()["summary"]["sleep_duration"][0]["summary"]["total"] == 900
    source, _ = threshold_source()
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(source))
    result = client.get("/api/health/training-thresholds?from=2026-01-01&to=2026-01-03", headers=headers)
    assert result.status_code == 200
    assert result.json()["workout_changes"]["lactate_threshold_hr"][0]["change"] == 5
