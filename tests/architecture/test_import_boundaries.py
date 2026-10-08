"""Enforce inward dependencies for the domain and application packages."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src" / "vitalis"
DOMAIN_FORBIDDEN = (
    "fastapi", "starlette", "flask", "requests", "httpx", "aiohttp",
    "http.client", "urllib.request", "sqlalchemy", "sqlmodel", "peewee",
    "django.db", "apscheduler", "smtplib", "anthropic", "claude_agent_sdk",
    "openai", "zepp", "zepp_sdk", "huami", "vitalis.entrypoints.api", "vitalis.adapters",
    "vitalis.bootstrap", "vitalis.connectors", "vitalis.scheduler", "vitalis.services",
    "vitalis.adapters.persistence",
)
APPLICATION_FORBIDDEN = DOMAIN_FORBIDDEN


def _imported_names(source: str, package: str):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
            yield module, node.lineno
            for alias in node.names:
                yield f"{module}.{alias.name}", node.lineno
        elif isinstance(node, ast.Call) and node.args:
            # Literal dynamic imports must not silently bypass the boundary.
            dynamic = (isinstance(node.func, ast.Name) and node.func.id == "__import__") or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "importlib"
            )
            if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                yield node.args[0].value, node.lineno


def _violations(source: str, package: str, forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"line {line}: {name}"
        for name, line in _imported_names(source, package)
        if name and any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]


@pytest.mark.parametrize("layer,forbidden", [
    ("domain", DOMAIN_FORBIDDEN),
    ("application", APPLICATION_FORBIDDEN),
])
def test_inward_imports(layer, forbidden):
    root = SOURCE / layer
    violations = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        package = ".".join(("vitalis", layer, *relative.parts[:-1]))
        violations.extend(
            f"{path.relative_to(SOURCE)}: {problem}"
            for problem in _violations(path.read_text(encoding="utf-8"), package, forbidden)
        )
    assert not violations, "Forbidden dependencies:\n" + "\n".join(violations)


@pytest.mark.parametrize("layer,source", [
    ("domain", "from sqlalchemy import select"),
    ("domain", "import sqlmodel"),
    ("domain", "from vitalis import connectors"),
    ("application", "from ..adapters.persistence import database"),
    ("application", "import vitalis.adapters.persistence.analysis_jobs"),
    ("application", "import importlib; importlib.import_module('sqlalchemy.orm')"),
    ("application", "from vitalis.adapters.zepp import ZeppConnector"),
    ("application", "from vitalis.services.aggregation_service import AggregationService"),
])
def test_deliberate_forbidden_import_is_caught(layer, source):
    forbidden = DOMAIN_FORBIDDEN if layer == "domain" else APPLICATION_FORBIDDEN
    assert _violations(source, f"vitalis.{layer}", forbidden)


def test_no_old_runtime_shadow_packages_remain():
    for old in ("models", "storage", "connectors"):
        assert not list((SOURCE / old).glob("*.py")), f"old {old} implementation remains"


def test_daily_delivery_has_one_pure_policy_and_one_concrete_adapter():
    policy = (SOURCE / "application" / "delivery_policy.py").read_text(encoding="utf-8")
    adapter = (SOURCE / "adapters" / "daily_push.py").read_text(encoding="utf-8")
    scheduler = (SOURCE / "scheduler" / "jobs.py").read_text(encoding="utf-8")

    for forbidden in (
        "vitalis.adapters", "vitalis.config", "httpx", "sqlalchemy", "pathlib",
        "import os", "import fcntl", "import msvcrt",
    ):
        assert forbidden not in policy, f"delivery policy imports concrete dependency: {forbidden}"
    assert "prepare_delivery" in policy
    assert "from vitalis.application.delivery_policy import" in adapter
    assert "PushService" in adapter and "HealthRepository" in adapter
    assert "_delivery_marker" not in adapter
    assert "vitalis.adapters.daily_push" in scheduler
    assert "vitalis.services.daily_push" not in scheduler


def test_legacy_daily_push_modules_are_removed():
    assert not (SOURCE / "services" / "daily_push.py").exists()
    assert not (SOURCE / "services" / "__init__.py").exists()


def test_api_routes_never_import_concrete_storage_or_vendor_logic():
    forbidden = (
        "sqlalchemy", "vitalis.adapters.persistence", "vitalis.adapters.zepp",
        "vitalis.adapters.notifications", "vitalis.services",
    )
    routes = SOURCE / "entrypoints" / "api" / "routes"
    violations = []
    for path in routes.glob("*.py"):
        package = "vitalis.entrypoints.api.routes"
        violations.extend(
            f"{path.name}: {problem}"
            for problem in _violations(path.read_text(encoding="utf-8"), package, forbidden)
        )
    assert not violations, "Concrete route imports:\n" + "\n".join(violations)
    assert _violations(
        "from vitalis.adapters.persistence import HealthRepository",
        "vitalis.entrypoints.api.routes", forbidden,
    )


def test_raw_health_routes_use_application_query_boundary():
    health = (SOURCE / "entrypoints" / "api" / "routes" / "health.py").read_text(
        encoding="utf-8"
    )
    current = (SOURCE / "entrypoints" / "api" / "routes" / "current.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("HealthRepository", "session_scope", "metric_sample_rows"):
        assert forbidden not in health
    assert "return get_health_query().data_status(user_id)" in current
    assert "return get_health_query().list_workouts(" in current
    assert "health_data_health(" not in current
    assert "health_workouts(" not in current
    assert "health_workout_detail(" not in current
