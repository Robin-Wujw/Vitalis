import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "vitalis"


def test_skill_is_renderer_only_and_uses_current_intelligence_contracts():
    skill = (SKILL / "SKILL.md").read_text()
    daily = (SKILL / "tools" / "daily.py").read_text()
    weekly = (SKILL / "tools" / "weekly.py").read_text()
    monthly = (SKILL / "tools" / "monthly.py").read_text()
    explain = (SKILL / "tools" / "explain.py").read_text()
    assert "Never reproduce those calculations" in skill
    assert "INSUFFICIENT_DATA" in skill
    assert 'request("GET", "daily"' in daily
    assert 'request("GET", "weekly"' in weekly
    assert 'request("GET", "monthly"' in monthly
    assert 'request("GET", "explain"' in explain
    assert not (SKILL / "tools" / "daily_profile.py").exists()
    analyze = (SKILL / "tools" / "analyze.py").read_text()
    assert "request(" in analyze and '"POST"' in analyze and '"analyze"' in analyze
    assert not (SKILL / "tools" / "health_query.py").exists()
    assert "All user-visible content must be Chinese" in skill
    assert "decision.action_plan" in skill


def test_skill_has_all_workflows_and_valid_schema():
    for name in ("morning.md", "evening.md", "weekly.md", "monthly.md", "on_demand.md"):
        assert (SKILL / "workflows" / name).is_file()
    schema = json.loads((SKILL / "schemas" / "daily_profile.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == "10.0"
    assert "analysis_run_id" in schema["required"]
    assert "model_version" not in schema["required"]
    actions = schema["properties"]["decision"]["properties"]["action"]["enum"]
    assert "INSUFFICIENT_DATA" in actions
    decision_required = schema["properties"]["decision"]["required"]
    assert {"action_label", "confidence_label", "action_plan"} <= set(decision_required)
    assert "prescriptions" not in decision_required
    workout_required = (
        schema["properties"]["features"]["properties"]["training"]["properties"]
        ["recent_workouts"]["items"]["required"]
    )
    assert {"sport_mode_label", "recognition_confidence_label"} <= set(workout_required)
    assert {"trends", "events"} <= set(schema["required"])
    assert "open_health_insights" in schema["required"]
    weekly = json.loads((SKILL / "schemas" / "weekly_profile.json").read_text())
    assert {"facts", "inferences", "actions"} <= set(weekly["required"])
    monthly = json.loads((SKILL / "schemas" / "monthly_profile.json").read_text())
    assert {"facts", "inferences", "actions"} <= set(monthly["required"])
    assert "open_health_insights" not in weekly["required"]
    assert "open_health_insights" not in monthly["required"]
    assert "open_health_period_summary" in weekly["required"]
    assert "open_health_period_summary" in monthly["required"]
    context = json.loads((SKILL / "schemas" / "context.json").read_text())
    assert {"open_health_summary", "insights_stale"} <= set(context["required"])
    for name in (
        "trends.json", "health_events.json", "context.json",
        "training_responses.json", "personal_model.json", "personal_associations.json",
        "timeline.json", "training_preferences.json", "strength_exercises.json",
        "profile.json",
    ):
        json.loads((SKILL / "schemas" / name).read_text())


def test_skill_exposes_read_analyze_and_act_tools():
    expected = {
        "daily.py", "weekly.py", "monthly.py", "trends.py", "events.py", "explain.py", "context.py",
        "sync.py", "analyze.py", "feedback.py", "acknowledge_event.py",
        "training_responses.py", "personal_model.py", "personal_associations.py", "timeline.py",
        "complete_recommendation.py",
        "training_preferences.py", "strength_exercises.py", "profile.py",
        "daily_push.py",
    }
    assert expected <= {path.name for path in (SKILL / "tools").glob("*.py")}
    complete = (SKILL / "tools" / "complete_recommendation.py").read_text()
    feedback = (SKILL / "tools" / "feedback.py").read_text()
    strength = (SKILL / "tools" / "strength_exercises.py").read_text()
    preferences = (SKILL / "tools" / "training_preferences.py").read_text()
    assert 'add_argument("--workout-source", required=True)' in complete
    assert '"workout_source": args.workout_source' in complete
    assert 'add_argument("--workout-source")' in feedback
    assert '"workout_source": args.workout_source' in feedback
    assert 'add_argument("--source", required=True)' in strength
    assert 'params={"source": args.source}' in strength
    assert 'request("PATCH", "training-preferences"' in preferences
    assert 'choices=(' in preferences and '"pain_or_injury_notes"' in preferences
    assert 'body[field] = None' in preferences
    assert "exclude_none" not in preferences


def test_skill_runtime_requires_explicit_user_and_uses_loopback_default(monkeypatch):
    monkeypatch.delenv("VITALIS_API", raising=False)
    monkeypatch.delenv("VITALIS_USER", raising=False)
    spec = importlib.util.spec_from_file_location(
        "vitalis_skill_client_test", SKILL / "tools" / "_client.py"
    )
    client = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(client)

    assert client.API == "http://localhost:8000"
    assert client.configured_user() is None
    monkeypatch.setenv("VITALIS_USER", "explicit-local-user")
    assert client.configured_user() == "explicit-local-user"


def test_skill_client_passes_exact_runtime_identity(monkeypatch):
    monkeypatch.setenv("VITALIS_API", "http://127.0.0.1:8765/")
    spec = importlib.util.spec_from_file_location(
        "vitalis_skill_client_request_test", SKILL / "tools" / "_client.py"
    )
    client = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(client)
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return Response()

    monkeypatch.setattr(client.httpx, "request", fake_request)
    assert client.request("GET", "daily", "explicit-local-user") == {"status": "ok"}
    assert captured == {
        "method": "GET",
        "url": "http://127.0.0.1:8765/api/v1/intelligence/daily",
        "headers": {"X-User-Id": "explicit-local-user"},
        "timeout": 60.0,
    }
