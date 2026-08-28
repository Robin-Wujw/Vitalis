import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "vitalis"


def test_skill_is_renderer_only_and_uses_intelligence_v2_contracts():
    skill = (SKILL / "SKILL.md").read_text()
    daily = (SKILL / "tools" / "daily.py").read_text()
    weekly = (SKILL / "tools" / "weekly.py").read_text()
    explain = (SKILL / "tools" / "explain.py").read_text()
    assert "Never reproduce those calculations" in skill
    assert "INSUFFICIENT_DATA" in skill
    assert 'request("GET", "daily"' in daily
    assert 'request("GET", "weekly"' in weekly
    assert 'request("GET", "explain"' in explain
    assert not (SKILL / "tools" / "daily_profile.py").exists()
    assert not (SKILL / "tools" / "analyze.py").exists()
    assert not (SKILL / "tools" / "health_query.py").exists()
    assert "All user-visible content must be Chinese" in skill
    assert "decision.prescriptions" in skill


def test_skill_has_all_workflows_and_valid_schema():
    for name in ("morning.md", "evening.md", "weekly.md", "on_demand.md"):
        assert (SKILL / "workflows" / name).is_file()
    schema = json.loads((SKILL / "schemas" / "daily_profile.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == "2.0"
    actions = schema["properties"]["decision"]["properties"]["action"]["enum"]
    assert "INSUFFICIENT_DATA" in actions
    decision_required = schema["properties"]["decision"]["required"]
    assert {"action_label", "confidence_label", "prescriptions"} <= set(decision_required)
    workout_required = (
        schema["properties"]["features"]["properties"]["training"]["properties"]
        ["recent_workouts"]["items"]["required"]
    )
    assert {"sport_mode_label", "recognition_confidence_label"} <= set(workout_required)
    assert {"trends", "events"} <= set(schema["required"])
    weekly = json.loads((SKILL / "schemas" / "weekly_profile.json").read_text())
    assert {"facts", "inferences", "actions"} <= set(weekly["required"])
    for name in ("trends.json", "health_events.json"):
        json.loads((SKILL / "schemas" / name).read_text())


def test_skill_exposes_read_analyze_and_act_tools():
    expected = {
        "daily.py", "weekly.py", "trends.py", "events.py", "explain.py", "context.py",
        "sync.py", "feedback.py", "acknowledge_event.py",
    }
    assert expected <= {path.name for path in (SKILL / "tools").glob("*.py")}
