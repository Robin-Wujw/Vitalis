"""Enforce one current owner per documentation topic."""

from pathlib import Path
import re

from vitalis.entrypoints.api.app import app


ROOT = Path(__file__).parents[2]
CANONICAL = {
    "docs/quickstart.md", "docs/architecture.md", "docs/data-contracts.md",
    "docs/zepp.md", "docs/agents.md", "docs/operations.md", "docs/reports.md",
    "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md",
}


def _read(relative: str) -> str:
    return ROOT.joinpath(relative).read_text(encoding="utf-8")


def test_documentation_navigation_has_one_owner_per_current_topic():
    hub = _read("docs/README.md")
    ownership = hub.split("## 主题归属\n", 1)[1]
    rows = re.findall(r"^\| ([^|]+) \| \[[^]]+\]\(([^)]+)\) \|$", ownership, re.M)
    assert len(rows) >= 10
    assert len(rows) == len({topic.strip() for topic, _ in rows})
    for relative in CANONICAL:
        assert (ROOT / relative).is_file()
        target = relative.removeprefix("docs/") if relative.startswith("docs/") else "../" + relative
        assert f"]({target})" in ownership
    assert "旧版迁移" not in hub


def test_first_use_and_developer_routes_are_short_and_current():
    hub = _read("docs/README.md")
    readme = _read("README.md")
    english = _read("README.en.md")
    rules = _read("AGENTS.md")
    assert "](quickstart.md)" in hub and "](architecture.md)" in hub
    assert "](docs/quickstart.md)" in readme
    assert "](docs/architecture.md)" in readme
    assert "](CONTRIBUTING.md)" in rules
    assert len(english.splitlines()) <= 20
    assert len(rules.splitlines()) <= 100
    assert len(_read("skills/vitalis/SKILL.md").splitlines()) <= 150


def test_runtime_examples_track_api_worker_and_authentication():
    paths = app.openapi()["paths"]
    assert "/api/data-status" in paths
    assert "/api/reports/{kind}" in paths
    assert not any(path.startswith("/api/v1") for path in paths)
    assert "start_scheduler()" in _read("src/vitalis/entrypoints/worker.py")
    assert "start_scheduler()" not in _read("src/vitalis/entrypoints/api/app.py")
    assert 'frozenset({"read", "analyze", "sync", "feedback", "manage"})' in _read(
        "src/vitalis/adapters/persistence/access_tokens.py"
    )
    assert "/api/reports/daily" in _read("docs/quickstart.md")
    assert "Bearer" in _read("docs/agents.md")
    assert "X-User-Id" in _read("docs/agents.md")


def test_repository_rules_and_product_skill_have_distinct_roles():
    rules = _read("AGENTS.md")
    skill = _read("skills/vitalis/SKILL.md")
    integration = _read("docs/agents.md")
    assert not rules.startswith("---\n")
    assert skill.startswith("---\nname: vitalis\n")
    assert "tools/check.py" in rules
    assert "tools/check.py" not in skill
    assert "../../" not in skill
    assert "仓库开发" in rules
    assert "根" in integration and "Skill" in integration
