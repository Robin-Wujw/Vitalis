"""Failure propagation and scope of the unified verification runner."""

from io import BytesIO
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

from tools import check


def test_child_failure_preserves_exit_code(tmp_path):
    step = check.Check("failing", (sys.executable, "-c", "import sys; sys.exit(23)"), tmp_path, 10)
    assert check.run_check(step, env=check.check_environment()) == 23


def test_missing_executable_is_not_success(tmp_path, capsys):
    step = check.Check("missing", (str(tmp_path / "absent-tool"),), tmp_path, 10)
    assert check.run_check(step, env=check.check_environment()) == 127
    assert "BLOCKED" in capsys.readouterr().out


def test_timeout_is_not_success(tmp_path, capsys):
    step = check.Check("slow", (sys.executable, "-c", "import time; time.sleep(3)"), tmp_path, 0.05)
    assert check.run_check(step, env=check.check_environment()) == 124
    assert "timed out" in capsys.readouterr().out


def test_all_ci_runs_every_mandatory_target_despite_failure(monkeypatch):
    visited = []

    def record(target, *, ci, env):
        visited.append((target, ci))
        return 7 if target == "quick" else 0

    monkeypatch.setattr(check, "run_target", record)
    assert check.main(["all", "--ci"]) == 7
    assert visited == [
        (target, True) for target in ("quick", "backend", "clients", "docs", "package", "e2e")
    ]


def test_ci_clients_checks_browser_extension_and_skill(monkeypatch):
    seen = []
    monkeypatch.setattr(check, "run_steps", lambda checks, *, env: seen.extend(checks) or 0)
    assert check.run_target("clients", ci=True, env={}) == 0
    assert any(step.command == ("node", "--check", "background.js") for step in seen)
    assert any(step.name == "clients:skill-syntax" for step in seen)


def test_docs_missing_checker_fails_explicitly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    monkeypatch.setattr(check, "docs_checks", lambda: [])
    assert check.run_target("docs", ci=True, env={}) != 0
    assert "missing mandatory checker" in capsys.readouterr().out


def test_all_ci_cannot_skip_missing_offline_acceptance(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    assert check.run_target("e2e", ci=True, env={}) != 0
    assert "test_offline_acceptance.py" in capsys.readouterr().out


def test_commands_match_existing_manifests_and_are_bounded():
    quick = check.quick_checks()
    backend = check.backend_checks()
    clients = check.clients_checks(ci=True)
    assert all(step.timeout > 0 for step in quick + backend + clients)
    assert any("ruff" in step.command for step in quick)
    assert any("tests/test_intelligence_contracts.py" in step.command for step in quick)
    assert any("tests/test_api.py" not in step.command and "tests" in step.command for step in backend)
    assert any(step.command == ("node", "--check", "popup.js") for step in clients)
    assert any("tests/test_vitalis_skill.py" in step.command for step in clients)


def test_wheel_contents_require_source_resources_and_reject_private_data(tmp_path):
    wheel = tmp_path / "vitalis-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("vitalis/__init__.py", "")
        archive.writestr("vitalis/data/embedded.json", "{}")
        archive.writestr("vitalis-0.1.0.dist-info/entry_points.txt", "[console_scripts]\n")
    sources = {"vitalis/__init__.py", "vitalis/data/embedded.json"}
    check.inspect_wheel(wheel, sources)
    with pytest.raises(ValueError, match="omits package sources/resources"):
        check.inspect_wheel(wheel, sources | {"vitalis/data/missing.json"})
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("vitalis/unknown-note.txt", "synthetic private material")
    with pytest.raises(ValueError, match="sensitive"):
        check.inspect_wheel(wheel, sources)


def test_sdist_contents_reject_private_workspace_files(tmp_path):
    sdist = tmp_path / "vitalis-0.1.0.tar.gz"

    def create_archive(paths):
        with tarfile.open(sdist, "w:gz") as archive:
            for path in paths:
                payload = b"synthetic fixture"
                member = tarfile.TarInfo(f"vitalis-0.1.0/{path}")
                member.size = len(payload)
                archive.addfile(member, BytesIO(payload))

    expected = {"pyproject.toml", "src/vitalis/__init__.py", "THIRD_PARTY_NOTICES.en.md"}
    create_archive(expected)
    check.inspect_sdist(sdist, expected)
    with pytest.raises(ValueError, match="omits source files"):
        check.inspect_sdist(sdist, expected | {"README.md"})
    create_archive(expected | {"docs/local-material.apkm"})
    with pytest.raises(ValueError, match="sensitive"):
        check.inspect_sdist(sdist, expected)


def test_ci_workflow_invokes_strict_aggregate():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    assert "python tools/check.py all --ci" in workflow
    assert "permissions:" in workflow
