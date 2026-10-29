"""Static contracts for the deliberately knowledge-only adapted coach bundle."""

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "skills" / "healthy-fitness-coach"
SHA = "d4471a86b2edc8dedd2d86ad115476f9edd43a5a"
REPO = "https://github.com/Surge-Dan/healthy-fitness-coach-skill"
SOURCES = {
    "safety-boundaries": {"safety-screening.md": [[1, 64]]},
    "evidence-and-uncertainty": {"evidence-rules.md": [[1, 58]]},
    "training-principles": {
        "training-domains.md": [[1, 90]],
        "programming.md": [[3, 20], [34, 86]],
    },
    "technique-and-session-education": {"exercise-coaching.md": [[21, 60]]},
    "nutrition-education": {"nutrition.md": [[1, 64]]},
    "recovery-and-discomfort": {"recovery-pain.md": [[1, 58]]},
}


def test_exact_knowledge_only_file_allowlist():
    expected = {"SKILL.md", "LICENSE", "provenance.json"} | {
        f"references/{name}.md" for name in SOURCES
    }
    assert BUNDLE.is_dir(), "adapted coach bundle has not been implemented"
    assert {
        p.relative_to(BUNDLE).as_posix() for p in BUNDLE.rglob("*") if p.is_file()
    } == expected
    assert not any(p.is_symlink() for p in BUNDLE.rglob("*"))


def test_license_is_verbatim_pinned_upstream():
    license_bytes = (BUNDLE / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "616c007fe2fdaf14ea65256aaaaab0f95792fe1f12c0545b1a6657083748f8ba"
    )
    assert "Copyright (c) 2026 梅乃丹" in license_bytes.decode("utf-8")


def test_provenance_pins_reviewed_paths_and_ranges():
    data = json.loads((BUNDLE / "provenance.json").read_text(encoding="utf-8"))
    assert data["upstream_repository"] == REPO
    assert data["upstream_commit"] == SHA
    assert data["adaptation"] == "knowledge-only"
    assert data["independently_verified_research"] is False
    assert data["license"] == {
        "spdx": "MIT",
        "upstream_path": "LICENSE",
        "local_path": "LICENSE",
    }
    assert set(data["references"]) == set(SOURCES)
    for name, sources in SOURCES.items():
        entry = data["references"][name]
        assert entry["local_path"] == f"references/{name}.md"
        assert entry["adaptation_notes"]
        assert {s["path"]: s["line_ranges"] for s in entry["sources"]} == {
            f"healthy-fitness-coach/references/{path}": ranges
            for path, ranges in sources.items()
        }
        for source in entry["sources"]:
            assert source["url"] == f"{REPO}/blob/{SHA}/{source['path']}"
    assert data["excluded_capabilities"]


def test_entrypoint_authority_safety_and_no_persistence():
    text = (BUNDLE / "SKILL.md").read_text(encoding="utf-8")
    assert "name: healthy-fitness-coach" in text
    for phrase in (
        "knowledge-only",
        "不是原版完整能力",
        "Vitalis API",
        "个人事实、趋势、评分",
        "不得自行推导",
        "readiness",
        "热量",
        "PR",
        "DNA",
        "已保存推荐",
        "任何工具调用之前",
        "胸痛",
        "当地急救",
        "不诊断",
        "18～55",
        "不能默认用户适用",
        "明确要求记录",
        "现有 Vitalis",
        "scope",
        "不隐式持久化",
        "原始日志",
        "影子档案",
        "缓存",
        "外部网络",
        "MCP",
        "Xunji",
        "假设示例",
    ):
        assert phrase in text
    assert text.index("任何工具调用之前") < text.index("## 个人数据权威")
    for name in SOURCES:
        assert f"references/{name}.md" in text


@pytest.mark.parametrize("name", SOURCES)
def test_references_are_short_educational_and_resolve(name):
    path = BUNDLE / "references" / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 65
    assert "一般教育" in text
    assert "？" in text
    assert "provenance.json" in text
    assert "SKILL.md" in text
    assert SHA in text
    for destination in re.findall(r"\]\(([^)]+)\)", text):
        if not destination.startswith(("https://", "http://")):
            assert (path.parent / destination.split("#", 1)[0]).is_file(), destination


def test_all_entrypoint_local_links_resolve():
    text = (BUNDLE / "SKILL.md").read_text(encoding="utf-8")
    for destination in re.findall(r"\]\(([^)]+)\)", text):
        if not destination.startswith(("https://", "http://")):
            assert (BUNDLE / destination.split("#", 1)[0]).is_file(), destination
