"""Deterministic JSON Schema exports consumed by the Vitalis skill."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .contracts import (
    AgentContext,
    DailyProfile,
    DecisionExplanation,
    MonthlyProfile,
    MorningBriefing,
    ReportBriefing,
    WeeklyProfile,
)


SCHEMA_ROOT = Path(__file__).parents[2] / "skills" / "vitalis" / "schemas"
DECISION_EXPLANATION_SCHEMA = SCHEMA_ROOT / "decision_explanation.json"
PROFILE_SCHEMAS = {
    "daily_profile.json": (DailyProfile, "daily-profile"),
    "weekly_profile.json": (WeeklyProfile, "weekly-profile"),
    "monthly_profile.json": (MonthlyProfile, "monthly-profile"),
    "context.json": (AgentContext, "agent-context"),
    "morning_briefing.json": (MorningBriefing, "morning-briefing"),
    "report_briefing.json": (ReportBriefing, "report-briefing"),
}


def _model_schema(model: type[BaseModel], slug: str) -> dict:
    schema = model.model_json_schema(mode="serialization")
    version_field = schema["properties"]["schema_version"]
    version = version_field.get("const") or version_field.get("default")
    if version is None:
        raise ValueError(f"{model.__name__} has no declared schema version")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://vitalis.local/schemas/{slug}-{version}.json"
    schema["title"] = f"Vitalis {model.__name__} {version}"
    return schema


def decision_explanation_schema() -> dict:
    return _model_schema(DecisionExplanation, "decision-explanation")


def skill_schemas() -> dict[str, dict]:
    """Keep local $refs and field presence identical to the runtime models."""
    return {
        **{name: _model_schema(model, slug) for name, (model, slug) in PROFILE_SCHEMAS.items()},
        "decision_explanation.json": decision_explanation_schema(),
    }


def export_skill_schemas() -> None:
    for name, schema in skill_schemas().items():
        (SCHEMA_ROOT / name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    export_skill_schemas()
