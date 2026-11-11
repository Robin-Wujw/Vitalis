"""Complete, owner-scoped scale records across stable backwards pages."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert

from vitalis.adapters.persistence import session_scope
from vitalis.adapters.persistence.models import MetricSample


def save_records(owner, records):
    rows = []
    for record in records:
        for metric, value, unit in record.get("metrics", (("weight", 72, "kg"), ("body_fat_percent", 21, "%"))):
            rows.append(dict(user_id=owner, source=record.get("source", "zepp"), metric=metric,
                timestamp=record["timestamp"].replace(tzinfo=None), value=value, unit=unit,
                source_scope=record.get("source_scope", "user_summary"), device_id=record.get("device_id", ""),
                source_record_id=record["id"], sample_ordinal=record.get("ordinal", 0),
                source_field="synthetic." + metric, qualification="community_schema",
                fetched_at=datetime(2026, 11, 1)))
    with session_scope() as db:
        db.execute(insert(MetricSample), rows)


def test_same_timestamp_pages_never_split_records_or_skip_sources(client, issue_token):
    owner = "body-pages-same-time"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["read"])}
    outsider = {"Authorization": "Bearer " + issue_token("body-pages-other", ["read"])}
    stamp = datetime(2026, 1, 3, 1, tzinfo=UTC)
    records = [dict(timestamp=stamp, id="same-record", source=source, device_id=device, ordinal=ordinal)
        for source, device, ordinal in (("zepp", "a", 0), ("zepp", "a", 1), ("zepp", "b", 0),
                                       ("other", "a", 0), ("other", "a", 1))]
    save_records(owner, records)
    params = {"from": "2026-01-03", "to": "2026-01-03", "limit": 2}
    first = client.get("/api/health/body-composition", params=params, headers=headers)
    assert first.status_code == 200, first.text
    assert len(first.json()["records"]) == 2
    cursor = first.json()["next_cursor"]
    rejected = client.get("/api/health/body-composition", params={**params, "cursor": cursor}, headers=outsider)
    assert rejected.status_code == 422
    assert client.get("/api/health/body-composition", params=params, headers=outsider).json()["state"] == "no_data"
    seen = []
    result = first.json()
    while True:
        for row in result["records"]:
            assert set(row["measurements"]) == {"weight", "body_fat_percent"}
            assert row["measurements"]["weight"]["value"] == 72
            assert row["measurements"]["body_fat_percent"]["qualification"] == "community_schema"
            assert row["measurements"]["body_fat_percent"]["fetched_at"]
            seen.append((row["source"], row["device_id"], row["sample_id"], row["sample_ordinal"]))
        if not result["has_more"]:
            assert result["next_cursor"] is None
            break
        assert result["next_cursor"]
        result = client.get("/api/health/body-composition", params={**params, "cursor": result["next_cursor"]}, headers=headers).json()
    assert len(seen) == len(set(seen)) == 5


def test_before_is_inclusive_local_date_and_default_reads_old_history(client, issue_token):
    owner = "body-pages-before"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["read"])}
    save_records(owner, [
        dict(timestamp=datetime(2020, 1, 2, 15, 59, tzinfo=UTC), id="last-before"),
        dict(timestamp=datetime(2020, 1, 2, 16, tzinfo=UTC), id="next-local-day"),
        dict(timestamp=datetime(2019, 1, 1, tzinfo=UTC), id="older")])
    result = client.get("/api/health/body-composition", headers=headers, params={"before": "2020-01-02"}).json()
    assert [r["sample_id"] for r in result["records"]] == ["last-before", "older"]
    assert result["range"]["from"] is None and result["range"]["to"] == "2020-01-02"
    assert result["limit"] == 20
    result = client.get("/api/health/body-composition", headers=headers,
        params={"from": "2020-01-02", "to": "2020-01-04", "before": "2020-01-02"}).json()
    assert [r["sample_id"] for r in result["records"]] == ["last-before"]
    assert result["coverage"]["scope"] == "returned_page_records_only"


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"before": "not-a-date"}, {"cursor": "not-a-cursor"},
    {"from": "2026-01-04", "before": "2026-01-03"},
    {"from": "2026-01-04", "to": "2026-01-03"},
])
def test_invalid_body_page_returns_stable_422(client, issue_token, params):
    headers = {"Authorization": "Bearer " + issue_token("body-pages-invalid", ["read"])}
    response = client.get("/api/health/body-composition", headers=headers, params=params)
    assert response.status_code == 422
    assert response.json()["failure_code"] == "validation_error"


def test_body_cursor_rejects_tampering_and_changed_range(client, issue_token):
    owner = "body-pages-cursor"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["read"])}
    save_records(owner, [dict(timestamp=datetime(2026, 1, 3, hour, tzinfo=UTC), id=str(hour)) for hour in (1, 2)])
    params = {"from": "2026-01-03", "to": "2026-01-03", "limit": 1}
    first = client.get("/api/health/body-composition", headers=headers, params=params).json()
    cursor = first["next_cursor"]
    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]
    for altered in ({**params, "cursor": tampered}, {**params, "cursor": cursor, "from": "2026-01-02"}):
        response = client.get("/api/health/body-composition", headers=headers, params=altered)
        assert response.status_code == 422
        assert response.json()["state"] == "failed"


def test_signed_cursor_still_requires_timezone_and_position_inside_window(client, issue_token):
    from vitalis.application.body_pagination import BodyPageCursor
    from vitalis.config import settings
    from datetime import date

    owner = "body-pages-cursor-boundary"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["read"])}
    scope = [owner, "2026-01-03", "2026-01-03", None, settings.timezone]
    codec = BodyPageCursor(settings.token_encryption_key)
    for stamp in ("2026-01-03T01:00:00", "2025-01-03T01:00:00+00:00", "2026-01-04T01:00:00+00:00"):
        cursor = codec.encode(scope, date(2026, 1, 3), (stamp, "zepp", "user_summary", "", "synthetic", 0))
        response = client.get("/api/health/body-composition", headers=headers,
            params={"from": "2026-01-03", "to": "2026-01-03", "cursor": cursor})
        assert response.status_code == 422


def test_no_date_cursor_freezes_cutoff_across_midnight():
    from vitalis.application.health_analytics import HealthAnalytics
    from vitalis.application.ports import HealthBodyRecordRead, HealthMetricRead
    from types import SimpleNamespace

    first = datetime(2026, 1, 3, 23, tzinfo=UTC)
    older = datetime(2026, 1, 2, 1, tzinfo=UTC)
    records = tuple(HealthBodyRecordRead(stamp, "zepp", "user_summary", "", str(i), 0,
        (("weight", HealthMetricRead(stamp, 72, "kg", "zepp", "user_summary", None, str(i), 0)),))
        for i, stamp in enumerate((first, older)))
    calls = []

    def read(_user, _metrics, start, end, **options):
        calls.append((start, end, options))
        return records if options["after"] is None else records[1:]

    reader = SimpleNamespace(body_composition_records=read)
    initial = HealthAnalytics(reader, now_factory=lambda: first).body_composition("synthetic", limit=1)
    resumed = HealthAnalytics(reader, now_factory=lambda: first + timedelta(days=1)).body_composition(
        "synthetic", limit=1, cursor=initial["next_cursor"])
    assert initial["range"] == resumed["range"]
    assert calls[0][1] == calls[1][1]
    assert resumed["records"][0]["sample_id"] == "1"


def test_body_pages_reach_records_past_old_3000_limit_with_indexed_bounded_reads(client, issue_token):
    owner = "body-pages-large"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["read"])}
    start = datetime(2026, 1, 3, tzinfo=UTC)
    save_records(owner, [dict(timestamp=start + timedelta(seconds=i), id=f"r-{i:04d}",
        metrics=(("weight", 72, "kg"),)) for i in range(3002)])
    params = {"from": "2026-01-03", "to": "2026-01-03", "limit": 200}
    with session_scope() as db:
        engine = db.get_bind()
    statements = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "metric_samples" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/health/body-composition", params=params, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert statements
    with engine.connect() as db:
        plans = [detail for statement, values in statements for *_, detail in db.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + statement, values).all()]
    assert all("SCAN metric_samples" not in detail for detail in plans), plans
    searches = [detail for detail in plans if detail.startswith("SEARCH metric_samples")]
    assert len(searches) == len(statements)
    assert all("timestamp" in detail for detail in searches), searches
    assert all("LIMIT" in statement or " IN " in statement for statement, _ in statements)
    seen = []
    result = response.json()
    for _ in range(17):
        seen.extend(row["sample_id"] for row in result["records"])
        if not result["has_more"]:
            break
        response = client.get("/api/health/body-composition", params={**params, "cursor": result["next_cursor"]}, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
    assert len(seen) == len(set(seen)) == 3002
    assert seen[0] == "r-3001" and seen[-1] == "r-0000"
    assert not result["has_more"]
