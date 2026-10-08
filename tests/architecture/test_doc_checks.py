from pathlib import Path

from tools.check_docs import check_links, documentation_files


def test_link_checker_accepts_unicode_anchor_and_local_directory(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "target.md"
    target.write_text("# 来源身份所有权\n", encoding="utf-8")
    source = tmp_path / "README.md"
    source.write_text("[章节](docs/target.md#来源身份所有权) [目录](docs/)\n", encoding="utf-8")
    assert check_links([source], tmp_path) == []


def test_link_checker_rejects_missing_target_and_escaping_skill(tmp_path):
    folder = tmp_path / "skills" / "vitalis"
    folder.mkdir(parents=True)
    (tmp_path / "secret.md").write_text("private\n", encoding="utf-8")
    skill = folder / "SKILL.md"
    skill.write_text("[missing](missing.md) [escape](../../secret.md)\n", encoding="utf-8")
    problems = check_links([skill], tmp_path, skill_dir=folder)
    assert len(problems) == 2
    assert "missing file" in problems[0]
    assert "Skill link escapes bundle" in problems[1]


def test_link_checker_requires_exact_filesystem_case(tmp_path):
    (tmp_path / "Current.md").write_text("# Current\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("[wrong](current.md)\n", encoding="utf-8")
    problems = check_links([readme], tmp_path)
    assert any("incorrect path casing" in issue or "missing file" in issue for issue in problems)


def test_link_checker_ignores_urls_in_fenced_examples(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("```markdown\n[example](gone.md)\n```\n[web](https://example.com)\n", encoding="utf-8")
    assert check_links([readme], tmp_path) == []


def test_documentation_inventory_covers_extensions_and_excludes_external_caches(tmp_path):
    (tmp_path / "README.md").write_text("# root\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.MARKDOWN").write_text("# guide\n", encoding="utf-8")
    (docs / "component.mdx").write_text("# component\n", encoding="utf-8")
    for folder in ("node_modules", ".venv", ".codex_pydeps", "vendor"):
        cached = tmp_path / folder
        cached.mkdir()
        (cached / "cache.md").write_text("# cache\n", encoding="utf-8")
    external = docs / "Vitalis_Experience_Review_2026-10-08"
    external.mkdir()
    (external / "spec.md").write_text("# design package\n", encoding="utf-8")

    present = {path.relative_to(tmp_path).as_posix() for path in documentation_files(tmp_path)}
    assert present == {"README.md", "docs/guide.MARKDOWN", "docs/component.mdx"}
