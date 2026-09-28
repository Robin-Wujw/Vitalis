"""Product Skill boundaries; detailed HTTP behavior lives in contract tests."""

from pathlib import Path

from vitalis.entrypoints.api.app import app
from vitalis.intelligence.contracts import StrengthExerciseInput, StrengthExerciseRecord


SKILL = Path(__file__).parents[1] / "skills" / "vitalis"


def test_single_runtime_skill_is_bearer_scoped_and_does_not_compute_health_facts():
    document = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert document.startswith("---\nname: vitalis\n")
    assert not (SKILL / "SKILL.en.md").exists()
    assert "scripts/vitalis_api.py" in document
    assert "references/api.md" in document
    assert "VITALIS_API_BASE_URL" in document
    assert "VITALIS_ACCESS_TOKEN" in document
    assert "Idempotency-Key" in document
    assert "status=snapshot_missing" in document
    assert "INSUFFICIENT_DATA" in document
    assert "不要自行计算趋势" in document
    assert "不得诊断疾病" in document
    assert "不同用户、设备、来源或单位" in document
    assert "tools/analyze.py" not in document
    assert "../../" not in document


def test_generated_skill_reference_matches_current_openapi():
    reference = (SKILL / "references" / "api.md").read_text(encoding="utf-8")
    paths = app.openapi()["paths"]
    for path, method, operation in (
        ("/api/data-status", "get", "get_data_status"),
        ("/api/reports/{kind}", "get", "get_report"),
        ("/api/analysis-runs", "post", "create_analysis_run"),
        ("/api/sync-jobs", "post", "create_sync_job"),
        ("/api/feedback", "post", "create_feedback"),
    ):
        assert paths[path][method]["operationId"] == operation
        assert f"`{path}`" in reference and f"`{operation}`" in reference
    assert "/api/v1" not in reference


def test_confirmed_repetitions_are_numeric_not_training_prescription_text():
    assert StrengthExerciseInput.model_fields["repetitions"].annotation == int | None
    assert StrengthExerciseRecord.model_fields["repetitions"].annotation == int | None
    description = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "负重" in description and "组数" in description
