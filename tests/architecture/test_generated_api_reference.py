"""Product Skill routes must be an exact subset of current OpenAPI."""

from copy import deepcopy
import json

import pytest

from tools import generate_api_reference as reference
from vitalis.entrypoints.api.app import app


def test_reference_covers_every_skill_allowlisted_operation():
    schema = app.openapi()
    selected = reference.skill_operations(schema)
    lines = reference.render_reference(schema).splitlines()
    assert selected
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


def test_http_reason_phrase_aliases_are_stable_across_python_versions():
    first = deepcopy(app.openapi())
    second = deepcopy(first)
    for document, descriptions in ((first, {"413": "Request Entity Too Large", "422": "Unprocessable Entity"}),
                                   (second, {"413": "Content Too Large", "422": "Unprocessable Content"})):
        for methods in document["paths"].values():
            for operation in methods.values():
                if isinstance(operation, dict):
                    for code, description in descriptions.items():
                        if code in operation.get("responses", {}):
                            operation["responses"][code]["description"] = description
    assert reference.render_reference(first) == reference.render_reference(second)


@pytest.fixture
def detail_schema(monkeypatch):
    monkeypatch.setattr(reference, "BASE_OPERATIONS", frozenset({"read_example"}))
    monkeypatch.setattr(reference, "skill_operations", lambda schema: set())
    return {
        "paths": {
            "/api/example/{item_id}": {
                "parameters": [{"name": "item_id", "in": "path", "required": True,
                                "schema": {"type": "string", "pattern": "^[a-z]+$"}}],
                "get": {
                    "operationId": "read_example",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False,
                         "description": "Page | size\nwith a bound",
                         "schema": {"type": "integer", "default": 20, "minimum": 1,
                                    "maximum": 100, "enum": [20, 50]}},
                        {"$ref": "#/components/parameters/Revision"},
                    ],
                    "requestBody": {"$ref": "#/components/requestBodies/Input"},
                    "responses": {"200": {"$ref": "#/components/responses/Result"}},
                },
            },
            "/api/private": {"get": {"operationId": "private", "responses": {}}},
        },
        "components": {
            "parameters": {"Revision": {"name": "If-Match", "in": "header", "required": True,
                                         "schema": {"type": "string"}}},
            "requestBodies": {"Input": {"required": True, "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}},
            "responses": {"Result": {"description": "Result tree", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}},
            "schemas": {
                "Node": {"type": "object", "required": ["name"], "properties": {
                    "name": {"type": "string", "minLength": 1, "description": "Node label"},
                    "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                }},
                "Unused": {"description": "PRIVATE_SCHEMA_MUST_NOT_LEAK"},
            },
        },
    }


def test_reference_renders_parameter_contract_and_token_file(detail_schema):
    rendered = reference.render_reference(detail_schema)
    assert "VITALIS_ACCESS_TOKEN_FILE" in rendered
    assert "| limit | query | no |" in rendered
    assert '"default": 20' in rendered
    assert '"enum": [20, 50]' in rendered
    assert '"minimum": 1' in rendered and '"maximum": 100' in rendered
    assert "Page &#124; size with a bound" in rendered
    assert "| item_id | path | yes |" in rendered
    assert "| If-Match | header | yes |" in rendered
    assert "| If-Match | 200 |" in rendered
    assert "/api/private" not in rendered


def test_reference_resolves_bodies_responses_and_cycles_once(detail_schema):
    rendered = reference.render_reference(detail_schema)
    assert "Request body (required: yes)" in rendered
    assert "Response 200" in rendered
    assert "Result tree" in rendered
    assert rendered.count('"Node label"') == 1
    assert '"required": [' in rendered
    assert '"minLength": 1' in rendered
    assert "PRIVATE_SCHEMA_MUST_NOT_LEAK" not in rendered
    assert len(rendered) < 8000
    reordered = json.loads(json.dumps(detail_schema, sort_keys=True))
    assert reference.render_reference(reordered) == rendered
    assert reference.render_reference(deepcopy(detail_schema)) == rendered


def test_reference_deduplicates_shared_schema_graph_and_response_contracts(detail_schema):
    schemas = detail_schema["components"]["schemas"]
    for index in range(20):
        target = f"#/components/schemas/Layer{index + 1}" if index < 19 else "#/components/schemas/Node"
        schemas[f"Layer{index}"] = {"type": "object", "properties": {
            "left": {"$ref": target}, "right": {"$ref": target}}}
    schemas["Node"]["properties"]["layers"] = {"$ref": "#/components/schemas/Layer0"}
    operation = detail_schema["paths"]["/api/example/{item_id}"]["get"]
    operation["responses"]["201"] = {"description": "Created", "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}
    rendered = reference.render_reference(detail_schema)
    assert "Response 200, 201" in rendered
    assert "201: Created" in rendered
    assert rendered.count('"Node label"') == 1
    assert rendered.count("### #/components/schemas/Layer") == 20
    assert len(rendered) < 14000


@pytest.mark.parametrize("ref", ["https://example.invalid/schema", "#/components/schemas/Missing"])
def test_reference_rejects_broken_or_external_references(detail_schema, ref):
    node = detail_schema["components"]["schemas"]["Node"]
    node["properties"]["children"]["items"]["$ref"] = ref
    with pytest.raises(ValueError, match="reference"):
        reference.render_reference(detail_schema)


def test_reference_check_detects_drift(tmp_path, monkeypatch, detail_schema):
    output = tmp_path / "api.md"
    monkeypatch.setattr(reference, "OUTPUT", output)
    monkeypatch.setattr(app, "openapi", lambda: detail_schema)
    output.write_text(reference.render_reference(detail_schema), encoding="utf-8")
    assert reference.main(["--check"]) == 0
    output.write_text("stale", encoding="utf-8")
    assert reference.main(["--check"]) == 1
