"""Deterministic JSON Schema exports consumed by the Vitalis skill."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import DecisionExplanation


SCHEMA_ROOT = Path(__file__).parents[2] / "skills" / "vitalis" / "schemas"
DECISION_EXPLANATION_SCHEMA = SCHEMA_ROOT / "decision_explanation.json"


def decision_explanation_schema() -> dict:
    schema = DecisionExplanation.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://vitalis.local/schemas/decision-explanation-1.0.json"
    schema["title"] = "Vitalis DecisionExplanation 1.0"
    return schema


def export_skill_schemas() -> None:
    DECISION_EXPLANATION_SCHEMA.write_text(
        json.dumps(decision_explanation_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    export_skill_schemas()
