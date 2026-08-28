import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "vitalis"


def test_skill_is_renderer_only_and_uses_daily_profile_contract():
    skill = (SKILL / "SKILL.md").read_text()
    tool = (SKILL / "tools" / "daily_profile.py").read_text()
    assert "Never reproduce those calculations" in skill
    assert "INSUFFICIENT_DATA" in skill
    assert "/api/v1/intelligence/daily-profile" in tool
    assert not (SKILL / "tools" / "analyze.py").exists()
    assert not (SKILL / "tools" / "health_query.py").exists()
    assert "All user-visible content must be Chinese" in skill
    assert "decision.prescriptions" in skill


def test_skill_has_all_workflows_and_valid_schema():
    for name in ("morning.md", "evening.md", "weekly.md", "on_demand.md"):
        assert (SKILL / "workflows" / name).is_file()
    schema = json.loads((SKILL / "schemas" / "daily_profile.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == "1.0"
    actions = schema["properties"]["decision"]["properties"]["action"]["enum"]
    assert "INSUFFICIENT_DATA" in actions
    decision_required = schema["properties"]["decision"]["required"]
    assert {"action_label", "confidence_label", "prescriptions"} <= set(decision_required)
    workout_required = (
        schema["properties"]["features"]["properties"]["training"]["properties"]
        ["recent_workouts"]["items"]["required"]
    )
    assert {"sport_mode_label", "recognition_confidence_label"} <= set(workout_required)
