"""Whole-range health statistics stay source-qualified and resource-bounded."""
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from vitalis.application.health_analytics import HealthAnalytics
from vitalis.application.ports import HealthDailyMetricRead, HealthMetricRead
from tests.test_health_analytics_parity import reader


START = date(2019, 1, 1)
END = START + timedelta(days=1600)


def history_reader():
    source = reader()
    daily = [HealthDailyMetricRead(date=START + timedelta(days=offset), metric="stress", value=value,
        unit="score", source="zepp", source_scope="device", device_id=device,
        source_field="stress", qualification="vendor_recorded")
        for offset, left, right in ((0, 10, 100), (800, 30, 80), (1600, 50, 60))
        for device, value in (("watch-a", left), ("watch-b", right))]

    def daily_rows(_owner, start, end, metric=None, **kwargs):
        return tuple(row for row in daily if start <= row.date <= end and (metric is None or row.metric == metric))

    source.daily_metrics = daily_rows
    source.activity_records = lambda _owner, start, end, **kwargs: tuple(
        {"date": row.date, "data": {"steps": row.value * 100, "source": row.source,
            "source_scope": row.source_scope, "device_id": row.device_id}}
        for row in daily if start <= row.date <= end)

    @contextmanager
    def metric_rows(_owner, metric, begin, stop):
        yield (HealthMetricRead(datetime.combine(row.date, datetime.min.time(), UTC), row.value,
            row.unit, row.source, row.source_scope, row.device_id, None, None,
            source_field=row.source_field, qualification=row.qualification)
            for row in daily if metric == "stress" and begin.date() <= row.date < stop.date())

    source.metric_sample_rows = metric_rows
    return source


@pytest.mark.parametrize("endpoint", ["daily-summary", "period-summary", "summary/stress"])
def test_more_than_1240_days_use_one_whole_range_trend_per_source(client, issue_token, monkeypatch, endpoint):
    from vitalis.entrypoints.api.routes import health_analytics

    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(history_reader(), timezone_name="UTC"))
    response = client.get(f"/api/health/{endpoint}", params={"from": START.isoformat(), "to": END.isoformat()},
        headers={"Authorization": "Bearer " + issue_token("long-health-" + endpoint.replace("/", "-"), ["read"])})
    assert response.status_code == 200
    result = response.json()
    if endpoint == "summary/stress":
        groups = result["signals"]["stress"]["daily"]
        scale = 1
    else:
        activity = result["activity"] if endpoint == "period-summary" else result
        groups = activity["summary"]["steps"]
        scale = 100
        assert {g["device_id"]: g["summary"]["total"] for g in groups} == {"watch-a": 9000, "watch-b": 24000}
    assert {g["device_id"]: g["trend"]["change_per_30_days"] for g in groups} == {
        "watch-a": pytest.approx(.75 * scale), "watch-b": pytest.approx(-.75 * scale)}
    assert all(g["coverage"]["days_requested"] == 1601 and g["coverage"]["days_with_data"] == 3 for g in groups)
    assert all(g["trend"]["from"] == START.isoformat() and g["trend"]["to"] == END.isoformat() for g in groups)


@pytest.mark.parametrize("endpoint", ["sleep", "training-thresholds"])
def test_sparse_health_history_has_no_calendar_span_limit(client, issue_token, monkeypatch, endpoint):
    from vitalis.entrypoints.api.routes import health_analytics

    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(reader(), timezone_name="UTC"))
    response = client.get(f"/api/health/{endpoint}?from=1900-01-01&to=2026-01-01",
        headers={"Authorization": "Bearer " + issue_token("long-empty-" + endpoint, ["read"])})
    assert response.status_code == 200
    assert response.json()["state"] == "no_data"


def test_long_range_does_not_relax_minute_activity_limit(client, issue_token):
    headers = {"Authorization": "Bearer " + issue_token("long-minutes", ["read"])}
    for end in ("2026-02-01", "2030-01-01"):
        response = client.get(f"/api/health/daily-summary?from=2026-01-01&to={end}&include_minutes=true", headers=headers)
        assert response.status_code == 422


def test_activity_projection_selects_latest_qualified_records_before_decoding(tmp_path):
    from vitalis.adapters.persistence.band_sources import BandSourceRecord
    from vitalis.adapters.persistence.database import _create_engine, init_db
    from vitalis.adapters.persistence.health_reader import SqlHealthReader
    from vitalis.adapters.persistence.repositories import HealthRepository

    engine = _create_engine(f"sqlite:///{(tmp_path / 'long-projection.db').as_posix()}")
    init_db(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    owner, stamp = "synthetic-long-projection", datetime(2026, 1, 1, tzinfo=UTC)
    periods = [{"start": 0, "stop": 59, "steps": 60, "mode": 3}]
    band = {"minute_steps": [1] * 1440, "raw_minutes": [[0, 0, 1]] * 1440,
        "hourly_steps": [60] * 24, "activity_periods": periods,
        "qualification": "reconciled_day_and_stages", "layout": "three_bytes"}
    try:
        with sessions.begin() as db:
            HealthRepository(db).upsert_user(owner)
            db.flush()
            for version, days in (("old", -1), ("current", 0), ("future", 1)):
                db.add(BandSourceRecord(id=version, user_id=owner, date=START, kind="activity",
                    source="zepp", source_scope="device", device_id="synthetic-watch",
                    fetched_at=(stamp + timedelta(days=days)).replace(tzinfo=None),
                    data={"version": version, "steps": 1440, "band_activity": band}))
        statements = []

        def capture(_conn, _cursor, statement, params, _context, _many):
            statements.append((statement, params))

        event.listen(engine, "before_cursor_execute", capture)
        records = SqlHealthReader(sessions).activity_records(owner, START, END, as_of=stamp,
            include_minutes=False, include_hourly=False, limit=10)
        event.remove(engine, "before_cursor_execute", capture)
        assert len(records) == 1 and records[0]["data"]["version"] == "current"
        result = records[0]["data"]["band_activity"]
        assert result == {"activity_periods": periods, "qualification": "reconciled_day_and_stages", "layout": "three_bytes"}
        sql, params = statements[0]
        assert len(statements) == 1 and "json_remove(" in sql and "LIMIT" in sql
        assert "row_number()" in sql.lower() or "not exists" in sql.lower()
        assert "$.band_activity.minute_steps" in params and "$.band_activity.raw_minutes" in params
        hourly = SqlHealthReader(sessions).activity_records(owner, START, END, as_of=stamp,
            include_minutes=False, include_hourly=True, limit=10)
        assert hourly[0]["data"]["band_activity"]["hourly_steps"] == [60] * 24
        assert "minute_steps" not in hourly[0]["data"]["band_activity"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("endpoint", ["daily-summary", "summary/stress", "period-summary"])
def test_actual_record_budget_fails_explicitly_without_partial_statistics(client, issue_token, monkeypatch, endpoint):
    from vitalis.application import health_query_budget
    from vitalis.entrypoints.api.routes import health_analytics

    monkeypatch.setattr(health_query_budget, "MAX_RECORDS", 2)
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(history_reader(), timezone_name="UTC"))
    response = client.get(f"/api/health/{endpoint}", params={"from": START.isoformat(), "to": END.isoformat()},
        headers={"Authorization": "Bearer " + issue_token("long-budget-" + endpoint.replace("/", "-"), ["read"])})
    assert response.status_code == 413
    assert response.json()["state"] == "failed"
    assert response.json()["failure_code"] == "payload_too_large"
    assert response.json()["retryable"] is False
    assert response.json()["next_action"] == "correct_request"
    assert "summary" not in response.json()


def test_budget_is_reset_after_failure_and_isolated_between_concurrent_queries(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from vitalis.application import health_query_budget

    monkeypatch.setattr(health_query_budget, "MAX_RECORDS", 2)
    source = history_reader()
    service = HealthAnalytics(source, timezone_name="UTC")
    with pytest.raises(health_query_budget.HealthQueryBudgetExceeded):
        service.daily_summary("synthetic", START, END)
    for _ in range(2):
        assert service.daily_summary("synthetic", START, START)["sample_count"] == 2
    gate = Barrier(2, timeout=10)
    original = source.activity_records

    def coordinated(*args, **kwargs):
        gate.wait()
        return original(*args, **kwargs)

    source.activity_records = coordinated
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.daily_summary("synthetic", START, START), range(2)))
    assert [result["sample_count"] for result in results] == [2, 2]


@pytest.mark.parametrize("budget,maximum", [("MAX_SAMPLES", 2), ("MAX_INPUT_BYTES", 100), ("MAX_RESULT_BYTES", 100)])
def test_dense_sample_and_payload_budgets_use_the_same_stable_413(client, issue_token, monkeypatch, budget, maximum):
    from vitalis.application import health_query_budget
    from vitalis.entrypoints.api.routes import health_analytics

    monkeypatch.setattr(health_query_budget, budget, maximum)
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: HealthAnalytics(history_reader(), timezone_name="UTC"))
    response = client.get("/api/health/summary/stress", params={"from": START.isoformat(), "to": END.isoformat()},
        headers={"Authorization": "Bearer " + issue_token("long-" + budget.lower(), ["read"])})
    assert response.status_code == 413
    assert response.json()["failure_code"] == "payload_too_large"


def test_period_sections_share_budget_instead_of_each_restarting_it(client, issue_token, monkeypatch):
    from vitalis.application import health_query_budget
    from vitalis.entrypoints.api.routes import health_analytics

    monkeypatch.setattr(health_query_budget, "MAX_RECORDS", 5)
    service = HealthAnalytics(history_reader(), timezone_name="UTC")
    assert service.daily_summary("synthetic", START, START)["sample_count"] == 2
    assert service.metrics("synthetic", "stress", START, START)["state"] == "ready"
    monkeypatch.setattr(health_analytics, "get_health_analytics", lambda: service)
    response = client.get("/api/health/period-summary", params={"from": START.isoformat(), "to": START.isoformat()},
        headers={"Authorization": "Bearer " + issue_token("shared-period-budget", ["read"])})
    assert response.status_code == 413


def test_sample_budget_stops_stream_without_exhausting_long_history(monkeypatch):
    from vitalis.application import health_query_budget

    monkeypatch.setattr(health_query_budget, "MAX_SAMPLES", 2)
    source = reader()
    consumed, closed = [], []

    @contextmanager
    def samples(_owner, metric, begin, stop):
        def generate():
            for index in range(100_000 if metric == "stress" else 0):
                consumed.append(index)
                yield HealthMetricRead(datetime(2020, 1, 1, tzinfo=UTC), index,
                    "score", "zepp", "device", "synthetic", None, None)
        try:
            yield generate()
        finally:
            closed.append(metric)

    source.metric_sample_rows = samples
    with pytest.raises(health_query_budget.HealthQueryBudgetExceeded):
        HealthAnalytics(source).metrics("synthetic", "stress", START, END)
    assert consumed == [0, 1, 2]
    assert "stress" in closed


@pytest.mark.parametrize("query", ["from=2026-02-01&to=2026-01-01", "to=0001-01-01", "from=9999-12-31&to=9999-12-31"])
def test_invalid_or_unrepresentable_date_windows_remain_422(client, issue_token, query):
    response = client.get("/api/health/daily-summary?" + query,
        headers={"Authorization": "Bearer " + issue_token("long-invalid-range", ["read"])})
    assert response.status_code == 422
