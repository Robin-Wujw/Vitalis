"""Offline-oriented verification commands for the current Vitalis checkout.

Dependencies are provisioned explicitly by the caller or CI, never by this runner.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "clients" / "zepp_os" / "balance2_bridge"
PYTHON = sys.executable
NPM = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
UV = shutil.which("uv") or "uv"
PYTEST = (PYTHON, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q")


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path = ROOT
    timeout: int = 180


def check_environment(ci: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": "sqlite:///:memory:",
        "ZEPP_MOCK": "true",
        "VITALIS_ENV": "test",
        "ZEPP_APP_ID": "",
        "ZEPP_APP_SECRET": "",
        "ZEPP_ACCESS_TOKEN": "",
        "PUSHPLUS_TOKEN": "",
        "VITALIS_PUSH_USER": "",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if ci:
        env["CI"] = "true"
    return env


def run_check(check: Check, *, env: dict[str, str]) -> int:
    pytest_step = check.command[:4] == (PYTHON, "-B", "-m", "pytest")
    context = tempfile.TemporaryDirectory(prefix="vitalis-pytest-") if pytest_step else nullcontext(None)
    with context as temp:
        command = check.command + ((f"--basetemp={temp}",) if temp else ())
        display = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        print(f"[{check.name}] $ {display} (cwd={check.cwd}, timeout={check.timeout}s)", flush=True)
        try:
            result = subprocess.run(
                command, cwd=check.cwd, env=env, timeout=check.timeout, check=False,
            )
        except FileNotFoundError as exc:
            print(f"[{check.name}] BLOCKED: missing executable or directory: {exc}", flush=True)
            return 127
        except subprocess.TimeoutExpired:
            print(f"[{check.name}] FAILED: timed out after {check.timeout}s", flush=True)
            return 124
        except OSError as exc:
            print(f"[{check.name}] FAILED: could not launch command: {exc}", flush=True)
            return 1
        print(f"[{check.name}] {'PASSED' if result.returncode == 0 else 'FAILED'}: exit={result.returncode}", flush=True)
        return result.returncode


def run_steps(checks: list[Check], *, env: dict[str, str]) -> int:
    first_failure = 0
    for check in checks:
        status = run_check(check, env=env)
        if status and not first_failure:
            first_failure = status
    return first_failure


def quick_checks() -> list[Check]:
    return [
        # This repository has no configured type checker and pre-existing broad
        # Ruff findings. E4/E9 are its current baseline-safe import/syntax gate.
        Check("quick:lint", (PYTHON, "-B", "-m", "ruff", "check", "--select", "E4,E9", "src/vitalis", "tests", "tools"), timeout=60),
        Check("quick:contracts", PYTEST + (
            "tests/architecture/test_check_runner.py",
            "tests/architecture/test_import_boundaries.py",
            "tests/unit/test_config.py",
            "tests/unit/test_workout_semantics.py",
            "tests/unit/test_time_windows.py",
            "tests/test_intelligence_contracts.py",
            "tests/test_baseline_engine.py",
            "tests/test_parser.py",
        ), timeout=180),
    ]


def backend_checks() -> list[Check]:
    return [Check("backend:pytest", PYTEST + (
        "tests",
        "--ignore=tests/architecture",
        "--ignore=tests/e2e",
        "--ignore=tests/test_browser_extension.py",
        "--ignore=tests/test_zepp_os_bridge.py",
        "--ignore=tests/test_vitalis_skill.py",
        "--ignore=tests/test_bilingual_markdown.py",
    ), timeout=600)]


def clients_checks(ci: bool) -> list[Check]:
    checks = [
        Check("clients:contracts", PYTEST + (
            "tests/test_browser_extension.py",
            "tests/test_zepp_os_bridge.py",
            "tests/test_vitalis_skill.py",
            "tests/contracts/test_skill_bundle.py",
            "tests/contracts/test_skill_http_client.py",
        ), timeout=180),
    ]
    for name in ("background.js", "popup.js", "page_credential.js"):
        checks.append(Check(f"clients:extension:{name}", ("node", "--check", name), ROOT / "clients" / "browser_extension", 30))
    for name in (
        "app.js", "app-side/index.js", "app-service/heart_rate_service.js",
        "setting/index.js", "page/index.js", "shared/queue.js", "shared/queue_core.mjs",
    ):
        checks.append(Check(f"clients:bridge:{name}", ("node", "--check", name), BRIDGE, 30))
    checks.extend([
        Check("clients:bridge-tests", (NPM, "test"), BRIDGE, 90),
        Check("clients:bridge-package", (NPM, "pack", "--dry-run", "--offline", "--ignore-scripts", "--json"), BRIDGE, 90),
        Check("clients:skill-syntax", (
            PYTHON, "-B", "-c",
            "from pathlib import Path; files = sorted(Path('skills/vitalis/scripts').glob('*.py')); assert files, 'no Skill client'; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in files]",
        ), timeout=30),
    ])
    checks.append(Check("clients:bridge-build", (NPM, "run", "build"), BRIDGE, 240))
    return checks


def docs_checks() -> list[Check]:
    return [Check("docs:layout-and-links", PYTEST + (
        "tests/test_bilingual_markdown.py",
        "tests/architecture/test_documentation_layout.py",
        "tests/architecture/test_doc_checks.py",
        "tests/architecture/test_generated_api_reference.py",
    ), timeout=120)]


def package_sources() -> set[str]:
    package_dir = ROOT / "src" / "vitalis"
    sources = {
        path.relative_to(ROOT / "src").as_posix()
        for path in package_dir.rglob("*.py") if path.is_file()
    }
    sources.add("vitalis/adapters/zepp/data/strength_exercises.json")
    extension = ROOT / "clients" / "browser_extension"
    for name in (
        "README.md", "README.en.md", "background.js", "manifest.json",
        "page_credential.js", "popup.css", "popup.html", "popup.js",
    ):
        if not (extension / name).is_file():
            raise ValueError(f"required extension file missing: {name}")
        sources.add(f"vitalis/static/browser_extension/{name}")
    sources.update({"vitalis/THIRD_PARTY_NOTICES.md", "vitalis/THIRD_PARTY_NOTICES.en.md"})
    return sources


def inspect_wheel(wheel: Path, expected: set[str]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = expected - names
    if missing:
        raise ValueError(f"wheel omits package sources/resources: {sorted(missing)}")
    if not any(name.endswith(".dist-info/entry_points.txt") for name in names):
        raise ValueError("wheel has no console entry points")
    forbidden = [
        name for name in names
        if (not name.split("/")[0].endswith(".dist-info") and name not in expected)
        or name.lower().endswith((".db", ".sqlite", ".apkm", ".apk", ".pem", ".key", ".env"))
        or "/__pycache__/" in name
        or name.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    if forbidden:
        raise ValueError(f"wheel contains unexpected or sensitive files: {forbidden}")
    print(f"[package:contents] PASSED: {len(expected)} source files and resources present; no forbidden files", flush=True)


def sdist_sources() -> set[str]:
    sources = {
        "pyproject.toml",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "THIRD_PARTY_NOTICES.en.md",
    }
    package_dir = ROOT / "src" / "vitalis"
    sources.update(
        path.relative_to(ROOT).as_posix()
        for path in package_dir.rglob("*.py") if path.is_file()
    )
    sources.add("src/vitalis/adapters/zepp/data/strength_exercises.json")
    extension = ROOT / "clients" / "browser_extension"
    for name in (
        "README.md", "README.en.md", "background.js", "manifest.json",
        "page_credential.js", "popup.css", "popup.html", "popup.js",
    ):
        if not (extension / name).is_file():
            raise ValueError(f"required extension file missing: {name}")
        sources.add(f"clients/browser_extension/{name}")
    return sources


def inspect_sdist(sdist: Path, expected: set[str]) -> None:
    with tarfile.open(sdist, "r:*") as archive:
        entries = archive.getmembers()
        if any(not member.isfile() and not member.isdir() for member in entries):
            raise ValueError("sdist contains unexpected links or archive entries")
        names = {PurePosixPath(member.name).as_posix() for member in entries if member.isfile()}
    if not names:
        raise ValueError("sdist is empty")
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise ValueError(f"sdist has unexpected top-level paths: {sorted(roots)}")
    prefix = next(iter(roots)) + "/"
    relative = {name.removeprefix(prefix) for name in names if name.startswith(prefix)}
    missing = expected - relative
    if missing:
        raise ValueError(f"sdist omits source files: {sorted(missing)}")
    forbidden = relative - expected - {"PKG-INFO", ".gitignore"}
    if forbidden or any(".." in PurePosixPath(name).parts for name in relative):
        raise ValueError(f"sdist contains unexpected or sensitive files: {sorted(forbidden)}")
    print(f"[package:sdist-contents] PASSED: {len(relative)} files; no private workspace material", flush=True)


# The wheel must win over the checkout and inherited site-packages. The temporary
# venv shares only preinstalled runtime dependencies, so pip never contacts an index.
SMOKE = """\
import importlib.metadata as metadata
import importlib.resources as resources
import os
from pathlib import Path
import sys
import sysconfig

root = Path(sys.argv[1]).resolve()
expected = sys.argv[2:]
package = __import__('vitalis')
origin = Path(package.__file__).resolve()
assert origin.is_relative_to(root), f'package resolved outside temporary venv: {origin}'
distribution = metadata.distribution('vitalis')
for name in ('vitalis',):
    entry = next((ep for ep in distribution.entry_points if ep.group == 'console_scripts' and ep.name == name), None)
    assert entry is not None, f'missing command: {name}'
    assert callable(entry.load()), f'unloadable command: {name}'
    script = Path(sysconfig.get_path('scripts')) / (name + ('.exe' if os.name == 'nt' else ''))
    assert script.is_file(), f'missing installed command: {script}'
for name in expected:
    assert resources.files('vitalis').joinpath(name).is_file(), f'missing installed resource: {name}'
from io import BytesIO
from zipfile import ZipFile
from vitalis.entrypoints.api.routes.connect import zepp_extension_zip
with ZipFile(BytesIO(zepp_extension_zip().body)) as extension:
    assert 'vitalis-zepp-login/manifest.json' in extension.namelist()
print(f'installed package: {origin}; verified CLI, extension download and {len(expected)} resources')
"""


def package_check(env: dict[str, str]) -> int:
    with tempfile.TemporaryDirectory(prefix="vitalis-package-") as directory:
        temp = Path(directory)
        wheels = temp / "wheels"
        wheels.mkdir()
        status = run_check(Check("package:build", (
            PYTHON, "-B", "-m", "pip", "wheel", "--no-index", "--no-deps",
            "--no-build-isolation", "--wheel-dir", str(wheels), ".",
        ), timeout=180), env=env)
        if status:
            return status
        built = list(wheels.glob("vitalis-*.whl"))
        if len(built) != 1:
            print(f"[package:build] FAILED: expected one vitalis wheel, got {built}", flush=True)
            return 1
        try:
            sources = package_sources()
            inspect_wheel(built[0], sources)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            print(f"[package:contents] FAILED: {exc}", flush=True)
            return 1

        sdists = temp / "sdists"
        sdists.mkdir()
        status = run_check(Check("package:sdist-build", (
            PYTHON, "-B", "-m", "hatchling", "build", "-t", "sdist", "-d", str(sdists),
        ), timeout=180), env=env)
        if status:
            return status
        built_sdists = list(sdists.glob("vitalis-*.tar.gz"))
        if len(built_sdists) != 1:
            print(f"[package:sdist-build] FAILED: expected one vitalis sdist, got {built_sdists}", flush=True)
            return 1
        try:
            inspect_sdist(built_sdists[0], sdist_sources())
        except (OSError, ValueError, tarfile.TarError) as exc:
            print(f"[package:sdist-contents] FAILED: {exc}", flush=True)
            return 1

        venv = temp / "venv"
        installed_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        package_env = {**env, "UV_PROJECT_ENVIRONMENT": str(venv)}
        status = run_check(Check("package:locked-dependencies", (
            UV, "sync", "--locked", "--offline", "--no-dev", "--no-install-project",
        ), timeout=180), env=package_env)
        if status:
            return status
        status = run_check(Check("package:install", (
            UV, "pip", "install", "--offline", "--no-deps", "--python",
            str(installed_python), str(built[0]),
        ), cwd=temp, timeout=120), env=env)
        if status:
            return status
        smoke_env = env.copy()
        smoke_env.pop("PYTHONPATH", None)
        resources = sorted(name.removeprefix("vitalis/") for name in sources if not name.endswith(".py"))
        return run_check(Check("package:installed", (
            str(installed_python), "-B", "-c", SMOKE, str(venv), *resources,
        ), cwd=temp, timeout=60), env=smoke_env)


def missing_check(name: str, path: Path) -> int:
    print(f"[{name}] BLOCKED: missing mandatory checker {path}", flush=True)
    return 1


def run_target(target: str, *, ci: bool, env: dict[str, str]) -> int:
    if target == "quick":
        return run_steps(quick_checks(), env=env)
    if target == "backend":
        return run_steps(backend_checks(), env=env)
    if target == "clients":
        return run_steps(clients_checks(ci), env=env)
    if target == "docs":
        status = run_steps(docs_checks(), env=env)
        checker = ROOT / "tools" / "check_docs.py"
        if not checker.is_file():
            blocked = missing_check("docs:checker", checker)
            return status or blocked
        generated = ROOT / "tools" / "generate_api_reference.py"
        return run_steps([
            Check("docs:checker", (PYTHON, "-B", str(checker)), timeout=180),
            Check("docs:api-reference", (PYTHON, "-B", str(generated), "--check"), timeout=60),
        ], env=env) or status
    if target == "package":
        return package_check(env)
    if target == "e2e":
        acceptance = ROOT / "tests" / "e2e" / "test_offline_acceptance.py"
        if not acceptance.is_file():
            return missing_check("e2e:offline", acceptance)
        return run_steps([Check("e2e:offline", PYTEST + (str(acceptance),), timeout=300)], env=env)
    raise ValueError(f"unknown target: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("quick", "backend", "clients", "docs", "package", "all"))
    parser.add_argument("--ci", action="store_true", help="require offline-only verification")
    args = parser.parse_args(argv)
    env = check_environment(args.ci)
    targets = ("quick", "backend", "clients", "docs", "package", "e2e") if args.target == "all" else (args.target,)
    first_failure = 0
    for target in targets:
        print(f"=== {target} ===", flush=True)
        status = run_target(target, ci=args.ci, env=env)
        print(f"=== {target}: {'PASSED' if status == 0 else f'FAILED exit={status}'} ===", flush=True)
        if status and not first_failure:
            first_failure = status
    return first_failure


if __name__ == "__main__":
    sys.exit(main())
