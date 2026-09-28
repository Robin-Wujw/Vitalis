"""The product Skill must still work when installed independently of this repo."""

from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "vitalis"


def test_skill_frontmatter_local_links_and_copy(tmp_path):
    document = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert document.startswith("---\nname: vitalis\ndescription: ")
    assert document.count("\n---\n") == 1
    assert len(document.splitlines()) < 150
    for link in re.findall(r"\]\(([^)]+)\)", document):
        assert not Path(link).is_absolute()
        resolved = (SKILL / link).resolve()
        assert resolved.is_relative_to(SKILL.resolve()) and resolved.is_file()
    assert "VITALIS_API_BASE_URL" in document
    assert "VITALIS_ACCESS_TOKEN" in document
    assert "scripts/vitalis_api.py" in document
    assert "--key-file" in document and "Idempotency-Key" in document
    assert "snapshot_missing" in document
    assert "INSUFFICIENT_DATA" in document
    assert "X-User-Id" in document

    installed = tmp_path / "installed" / "vitalis"
    assert not installed.is_relative_to(ROOT)
    shutil.copytree(SKILL, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runner = installed / "scripts" / "vitalis_api.py"
    assert runner.is_file()
    result = subprocess.run(
        [sys.executable, str(runner), "status"], cwd=tmp_path,
        env={"PATH": "", "VITALIS_API_BASE_URL": "http://127.0.0.1:9000"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert result.stderr == ""
    assert '"error": "invalid_access_token"' in result.stdout
