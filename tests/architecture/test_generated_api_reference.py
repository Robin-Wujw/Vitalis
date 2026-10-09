"""Product Skill routes must be an exact subset of current OpenAPI."""

import pytest

from tools import generate_api_reference as reference
from vitalis.entrypoints.api.app import app


def test_reference_covers_every_skill_allowlisted_operation():
    schema = app.openapi()
    selected = reference.skill_operations(schema)
    lines = reference.render_reference(schema).splitlines()
    assert len(selected) == 26
    assert len([line for line in lines if line.startswith("| `")]) == len(
        reference.BASE_OPERATIONS | selected
    )
    assert "revoke_source_account" not in selected
    assert "/api/sources/{source}/revoke" not in "\n".join(lines)


def test_unknown_skill_route_fails_generation(tmp_path, monkeypatch):
    script = tmp_path / "vitalis_api.py"
    script.write_text(
        'READ_OPERATIONS = {"missing": "intelligence/absent"}\nWRITE_OPERATIONS = {}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(reference, "SKILL_SCRIPT", script)
    with pytest.raises(ValueError, match="exactly one current API operation"):
        reference.skill_operations(app.openapi())
