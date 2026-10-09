"""Product inputs and provenance reach saved reports through the analysis use case."""

from datetime import date, datetime, timezone
from uuid import uuid4

from vitalis.adapters.persistence import HealthRepository, session_scope
from vitalis.bootstrap import get_intelligence_command, get_intelligence_query
from vitalis.entrypoints.api.app import app


DAY = date(2026, 9, 7)
AS_OF = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)


def test_product_endpoints_are_composed_into_current_api():
    paths = app.openapi()["paths"]
    assert "/api/product/goals" in paths
    assert "/api/product/feedback" in paths
    assert "/api/product/metrics" in paths


def test_all_saved_periods_include_qualified_public_analysis_context():
    user_id = f"report-context-{uuid4().hex}"
    with session_scope() as db:
        repo = HealthRepository(db)
        repo.upsert_user(user_id)
        repo.bind_source_mode(user_id, "mock")
    result = get_intelligence_command(
        today_factory=lambda: DAY, now_factory=lambda: AS_OF,
    ).analyze(user_id, DAY)
    query = get_intelligence_query()
    for profile in (query.daily(user_id, DAY), query.weekly(user_id, DAY), query.monthly(user_id, DAY)):
        context = profile.report_context
        assert context["source_mode"] == "mock"
        assert context["signals"]
        assert "personal_associations" in context
        assert "product_summary" in context
        summary = context["training_response_summary"]
        assert {item["day_offset"] for item in summary["window_summaries"]} == {0, 1, 2, 3}
        assert all(item["observed"] == 0 for item in summary["window_summaries"])
    assert result.run.input_manifest
    assert len(result.run.input_manifest_hash) == 64
    with session_scope() as db:
        row = HealthRepository(db).analysis_run(user_id, result.run.id)
        assert row.input_manifest == result.run.input_manifest
        assert row.input_manifest_hash == result.run.input_manifest_hash
