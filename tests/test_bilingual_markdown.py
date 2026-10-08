"""Current documentation and third-party notice invariants."""

import hashlib
from pathlib import Path

import pytest

from tools.check_docs import ROOT, check_links, documentation_files


MIT_SHA256 = "265eb53046c15797b39920f0e82914e450e431b2fc26b09d27bfd0a5c42d869d"
REMAINING_PAIRS = (
    ("README.md", "README.en.md"),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.en.md"),
    ("clients/browser_extension/README.md", "clients/browser_extension/README.en.md"),
)


def _read(relative: str) -> str:
    return ROOT.joinpath(relative).read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("chinese,english", REMAINING_PAIRS)
def test_retained_translation_entrypoints_are_reciprocal(chinese: str, english: str):
    zh = ROOT / chinese
    en = ROOT / english
    assert zh.is_file() and en.is_file()
    assert en.name in "\n".join(_read(chinese).splitlines()[:10])
    assert zh.name in "\n".join(_read(english).splitlines()[:10])


def test_mit_license_original_is_identical_across_notices():
    marker = "### MIT License\n\n"
    zh = _read("THIRD_PARTY_NOTICES.md")
    en = _read("THIRD_PARTY_NOTICES.en.md")
    assert zh.count(marker) == en.count(marker) == 1
    original = zh.split(marker, 1)[1].strip() + "\n"
    assert original == en.split(marker, 1)[1].strip() + "\n"
    assert hashlib.sha256(original.encode("utf-8")).hexdigest() == MIT_SHA256


def test_active_document_links_remain_local_and_valid():
    documents = documentation_files(ROOT)
    assert check_links(documents, ROOT, skill_dir=ROOT / "skills/vitalis") == []


def test_old_duplicate_document_entrypoints_are_gone():
    present = {path.relative_to(ROOT).as_posix() for path in documentation_files(ROOT)}
    for relative in (
        "SYSTEM.md", "SYSTEM.en.md", "docs/API.md", "docs/API.en.md",
        "docs/ARCHITECTURE.md", "docs/ARCHITECTURE.en.md",
        "docs/GETTING_STARTED.md", "docs/GETTING_STARTED.en.md",
        "docs/RESEARCH_NOTES.md", "docs/RESEARCH_NOTES.en.md",
        "docs/SYSTEM_HISTORY.md", "docs/SYSTEM_HISTORY.en.md",
        "docs/ZEPP_INTEGRATION.md", "docs/ZEPP_INTEGRATION.en.md",
        "skills/vitalis/SKILL.en.md",
    ):
        assert relative not in present, relative
    assert "docs/architecture.md" in present


def test_pre_release_compatibility_chain_is_removed():
    assert not (ROOT / "tools" / "upgrade_deployment_db.py").exists()
    assert not (ROOT / "tests" / "integration" / "test_deployment_upgrade.py").exists()
