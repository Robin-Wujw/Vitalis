"""Public date-window contracts for bounded API reads."""

from datetime import UTC, datetime

import pytest


def assert_invalid_date(response):
    assert response.status_code == 422
    payload = response.json()
    assert payload["state"] == "failed"
    assert payload["failure_code"] == "validation_error"
    assert payload["retryable"] is False
    assert payload["next_action"] == "correct_request"


@pytest.mark.parametrize(
    "path",
    [
        "/api/workouts?from=2026-09-03&to=2026-09-01",
        "/api/workouts/statistics?from=2026-09-03&to=2026-09-01",
    ],
)
def test_workout_windows_reject_reversed_dates(client, issue_token, path):
    headers = {"Authorization": "Bearer " + issue_token("date-window-reversed", ["read"])}
    response = client.get(path, headers=headers)
    assert_invalid_date(response)


@pytest.mark.parametrize(
    "path",
    [
        "/api/workouts?to=0001-01-01",
        "/api/workouts/statistics?to=0001-01-01",
        "/api/intelligence/events?end=0001-01-01",
        "/api/workouts?from=9999-12-31&to=9999-12-31",
        "/api/workouts/statistics?from=9999-12-31&to=9999-12-31",
    ],
)
def test_date_overflow_is_a_validation_error(client, issue_token, path):
    headers = {"Authorization": "Bearer " + issue_token("date-window-underflow", ["read"])}
    response = client.get(path, headers=headers)
    assert_invalid_date(response)


@pytest.mark.parametrize("with_records", [False, True])
def test_raw_metadata_range_is_bounded_by_result_limit_not_730_day_window(client, issue_token, with_records):
    from vitalis.adapters.persistence import HealthRepository, session_scope
    from vitalis.adapters.persistence.source_journal import SourceJournalRepository, SourceRecordInput

    owner = f"raw-date-window-{with_records}"
    headers = {"Authorization": "Bearer " + issue_token(owner, ["source_raw"])}
    if with_records:
        with session_scope() as db:
            HealthRepository(db).bind_source_mode(owner, "mock")
            journal = SourceJournalRepository(db)
            for year in (2020, 2026):
                stamp = datetime(year, 6, 1, 12, tzinfo=UTC)
                journal.append(SourceRecordInput(
                    user_id=owner, source="zepp", source_mode="mock", stream="workouts",
                    record_id=f"synthetic-{year}", observed_start=stamp, observed_end=None,
                    fetched_at=stamp, payload={"synthetic": True},
                ))
    path = "/api/sources/zepp/raw-records?stream=workouts&from=2020-01-01&to=2026-11-11"
    response = client.get(
        path,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == ("ready" if with_records else "no_data")
    assert len(result["records"]) == (2 if with_records else 0)
    assert result["truncated"] is False
    if with_records:
        limited = client.get(path + "&limit=1", headers=headers)
        assert limited.status_code == 200
        assert len(limited.json()["records"]) == 1
        assert limited.json()["truncated"] is True


def test_raw_metadata_requires_utc_convertible_end_date(client, issue_token):
    headers = {"Authorization": "Bearer " + issue_token("raw-date-max", ["source_raw"])}
    response = client.get(
        "/api/sources/zepp/raw-records?stream=workouts&from=9999-12-30&to=9999-12-31",
        headers=headers,
    )
    assert_invalid_date(response)
