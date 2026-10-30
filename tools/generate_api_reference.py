"""Generate the product Skill's allowlisted HTTP reference from current OpenAPI."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "skills" / "vitalis" / "references" / "api.md"
BASE_OPERATIONS = frozenset({
    "get_data_status", "get_report", "get_public_report_view", "get_report_state", "create_analysis_run", "get_job",
    "create_sync_job", "list_workouts", "get_workout", "create_feedback",
    "get_coach_policy", "create_coach_operation", "get_strength_review", "get_race_equivalence",
})
SKILL_SCRIPT = ROOT / "skills" / "vitalis" / "scripts" / "vitalis_api.py"
_PATH_PARAM = re.compile(r"\{[^{}]+\}")


def skill_operations(schema: dict) -> set[str]:
    """Resolve the Skill's literal allowlist against the actual HTTP schema."""
    source = ast.parse(SKILL_SCRIPT.read_text(encoding="utf-8"))
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in source.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
        and target.id in {"READ_OPERATIONS", "WRITE_OPERATIONS"}
    }
    if set(assignments) != {"READ_OPERATIONS", "WRITE_OPERATIONS"}:
        raise ValueError("Skill operation allowlists are missing or not literals")
    requested = [("get", relative) for relative in assignments["READ_OPERATIONS"].values()]
    requested.extend((method.lower(), relative) for method, relative in assignments["WRITE_OPERATIONS"].values())
    found = set()
    for method, relative in requested:
        normalized = _PATH_PARAM.sub("{}", "/api/" + relative)
        matches = [
            operation["operationId"]
            for path, methods in schema["paths"].items()
            if _PATH_PARAM.sub("{}", path) == normalized
            for route_method, operation in methods.items()
            if route_method == method
        ]
        if len(matches) != 1:
            raise ValueError(f"Skill route must match exactly one current API operation: {method} {relative}")
        found.add(matches[0])
    return found


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("&", "&amp;").replace("|", "&#124;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")


def _json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2 if pretty else None)


class _References:
    """Keep local component references symbolic; emit each reachable target once."""

    def __init__(self, schema: dict):
        self.schema = schema
        self.targets: dict[str, object] = {}

    def target(self, ref: str) -> object:
        if not ref.startswith("#/components/"):
            raise ValueError(f"unsupported OpenAPI reference: {ref}")
        value = self.schema
        try:
            for part in ref[2:].split("/"):
                value = value[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unresolved OpenAPI reference: {ref}") from exc
        return value

    def resolve(self, value: dict) -> dict:
        """Resolve parameter/body/response wrappers, not recursive schema fields."""
        seen = set()
        while "$ref" in value:
            ref = value["$ref"]
            if ref in seen:
                raise ValueError(f"cyclic OpenAPI wrapper reference: {ref}")
            seen.add(ref)
            target = self.target(ref)
            if not isinstance(target, dict):
                raise ValueError(f"non-object OpenAPI reference: {ref}")
            value = {**target, **{key: item for key, item in value.items() if key != "$ref"}}
        return value

    def collect(self, value: object) -> None:
        # Iterative traversal and a visited map make cycles and diamond graphs linear.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if "$ref" in item:
                    ref = item["$ref"]
                    if ref not in self.targets:
                        self.targets[ref] = self.target(ref)
                        pending.append(self.targets[ref])
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)

    def block(self, value: object) -> list[str]:
        self.collect(value)
        return ["```json", _json(value, pretty=True), "```", ""]


def _parameters(refs: _References, methods: dict, operation: dict) -> list[dict]:
    merged = {}
    for item in [*methods.get("parameters", []), *operation.get("parameters", [])]:
        item = refs.resolve(item)
        merged[(item["in"], item["name"])] = item
    return [item for key, item in sorted(merged.items()) if key[0] in {"query", "path", "header"}]


def render_reference(schema: dict) -> str:
    allowed = BASE_OPERATIONS | skill_operations(schema)
    rows = []
    details = {}
    refs = _References(schema)
    found = set()
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                continue
            operation_id = operation.get("operationId")
            if operation_id not in allowed:
                continue
            if operation_id in found:
                raise ValueError(f"duplicate product operation: {operation_id}")
            found.add(operation_id)
            parameters = _parameters(refs, methods, operation)
            details[operation_id] = (operation, parameters)
            required = sorted(
                item["name"] for item in parameters
                if item.get("required") and item.get("in") == "header"
            )
            responses = ", ".join(sorted(operation.get("responses", {})))
            rows.append((operation_id, method.upper(), path, ", ".join(required) or "-", responses))
    missing = allowed - found
    if missing:
        raise ValueError(f"missing product operations in OpenAPI: {sorted(missing)}")
    lines = [
        "<!-- Generated by tools/generate_api_reference.py; do not edit manually. -->",
        "# Vitalis HTTP operations",
        "",
        "Set `VITALIS_API_BASE_URL` to the service origin (no path). Prefer",
        "`VITALIS_ACCESS_TOKEN_FILE`: an absolute path to a private bearer-token file,",
        "re-read for each request. Only when file mode is not configured, use the",
        "private `VITALIS_ACCESS_TOKEN` environment value. Invalid file configuration",
        "fails closed; it does not fall back to the environment token. Never put tokens",
        "in command arguments, URLs, logs or conversation. Authentication and write",
        "scopes are enforced by the server.",
        "",
        "Only allowlisted operations are included. Schemas retain OpenAPI types,",
        "defaults, enums, constraints, descriptions and required fields. Local `$ref`",
        "targets appear once in the schema appendix, including recursive references;",
        "follow their exact component keys rather than expanding them repeatedly.",
        "An empty schema `{}` means OpenAPI does not specify fields, not an empty response.",
        "The complete service contract remains available from `GET /openapi.json`.",
        "",
        "| Operation | Method | Path | Required headers | Responses |",
        "| --- | --- | --- | --- | --- |",
    ]
    for operation_id, method, path, required, responses in sorted(rows):
        lines.append(f"| `{operation_id}` | {method} | `{path}` | {required} | {responses} |")
    lines.extend(["", "Read requests do not start a sync or analysis job.", "", "## Operation details", ""])
    for operation_id, method, path, _, _ in sorted(rows):
        operation, parameters = details[operation_id]
        lines.extend([f"### {operation_id}", "", f"`{method} {path}`", ""])
        for key in ("summary", "description"):
            if operation.get(key):
                lines.extend([_cell(operation[key]), ""])
        lines.extend(["#### Parameters", ""])
        if parameters:
            lines.extend(["| Name | In | Required | Schema (type / default / enum / constraints) | Description |",
                          "| --- | --- | --- | --- | --- |"])
            for item in parameters:
                contract = item.get("schema", item.get("content", {}))
                refs.collect(contract)
                required = "yes" if item.get("required") else "no"
                lines.append(f"| {_cell(item['name'])} | {item['in']} | {required} | "
                             f"{_cell(_json(contract))} | {_cell(item.get('description', ''))} |")
            lines.append("")
        else:
            lines.extend(["None.", ""])
        if "requestBody" in operation:
            body = refs.resolve(operation["requestBody"])
            required = "yes" if body.get("required") else "no"
            lines.extend([f"#### Request body (required: {required})", ""])
            lines.extend(refs.block(body))
        else:
            lines.extend(["#### Request body", "", "None.", ""])
        # Group identical error responses instead of repeating their schema per status.
        groups: dict[str, list[tuple[str, str]]] = {}
        for status, response in sorted(operation.get("responses", {}).items()):
            response = refs.resolve(response)
            contract = {key: value for key, value in response.items() if key != "description"}
            description = response.get("description", "")
            # Python 3.13 renamed HTTPStatus phrases; normalize only exact
            # standard aliases, preserving all application-specific descriptions.
            aliases = {"413": {"Request Entity Too Large", "Content Too Large"},
                       "422": {"Unprocessable Entity", "Unprocessable Content"}}
            if description in aliases.get(status, set()):
                description = {"413": "Content Too Large", "422": "Unprocessable Content"}[status]
            groups.setdefault(_json(contract), []).append((status, description))
        for response, statuses in groups.items():
            lines.extend([f"#### Response {', '.join(status for status, _ in statuses)}", ""])
            lines.extend(f"- {status}: {_cell(description)}" for status, description in statuses)
            lines.append("")
            lines.extend(refs.block(json.loads(response)))
    lines.extend(["## Schema appendix", "",
                  "Each reachable component is rendered once; `$ref` values name the headings below.", ""])
    for ref, target in sorted(refs.targets.items()):
        lines.extend([f"### {ref}", "", "```json", _json(target, pretty=True), "```", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when generated API reference differs")
    args = parser.parse_args(argv)
    from vitalis.entrypoints.api.app import app

    try:
        expected = render_reference(app.openapi())
    except ValueError as exc:
        print(f"API reference: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != expected:
            print("Skill API reference is missing or out of date", file=sys.stderr)
            return 1
        print("Skill API reference matches current OpenAPI")
        return 0
    OUTPUT.write_text(expected, encoding="utf-8")
    print(f"generated {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
