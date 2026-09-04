from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).parents[1]

PAIR_BASES = (
    "README.md",
    "SYSTEM.md",
    "THIRD_PARTY_NOTICES.md",
    "browser_extension/README.md",
    "docs/API.md",
    "docs/ARCHITECTURE.md",
    "docs/GETTING_STARTED.md",
    "docs/RESEARCH_NOTES.md",
    "docs/SYSTEM_HISTORY.md",
    "docs/ZEPP_INTEGRATION.md",
    "skills/vitalis/SKILL.md",
    "skills/vitalis/knowledge/evidence.md",
    "skills/vitalis/workflows/daily_explanation.md",
    "skills/vitalis/workflows/evening.md",
    "skills/vitalis/workflows/monthly.md",
    "skills/vitalis/workflows/morning.md",
    "skills/vitalis/workflows/on_demand.md",
    "skills/vitalis/workflows/weekly.md",
    "zepp_os/balance2_bridge/README.md",
)
INLINE_BILINGUAL = "docs/README.md"
PAIR_MAP = {
    chinese: chinese.removesuffix(".md") + ".en.md" for chinese in PAIR_BASES
}
REVERSE_PAIR_MAP = {english: chinese for chinese, english in PAIR_MAP.items()}
EXPECTED_MARKDOWN = {
    INLINE_BILINGUAL,
    *(path for pair in PAIR_MAP.items() for path in pair),
}
WORKFLOWS = (
    "daily_explanation",
    "evening",
    "monthly",
    "morning",
    "on_demand",
    "weekly",
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})([^\n]*)$", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`]+)`(?!`)")
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]", re.MULTILINE)
LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|[1-9]\.)\s+", re.MULTILINE)
NUMBER_RE = re.compile(
    r"(?<![\w])\d+(?:[.,]\d+)*(?:%|d|h|m|s|KB|MB)?(?![\w])"
)
DATE_RE = re.compile(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)")
COMMIT_HASH_RE = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{7}|[0-9a-f]{40})(?![0-9a-f])")
HTTP_ROUTE_RE = re.compile(
    r"\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|"
)
EXECUTABLE_FENCE_LANGUAGES = {
    "bash",
    "console",
    "http",
    "javascript",
    "js",
    "json",
    "python",
    "sh",
    "shell",
    "toml",
    "yaml",
    "yml",
}
# SHA-256 of the authoritative OpenStrap MIT body from Copyright through
# SOFTWARE., normalized to LF with exactly one trailing newline.
MIT_SHA256 = "265eb53046c15797b39920f0e82914e450e431b2fc26b09d27bfd0a5c42d869d"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _markdown_inventory() -> set[str]:
    output = _git(
        "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"
    )
    return {PurePosixPath(line).as_posix() for line in output.splitlines() if line}


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _split_link_destination(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1]
    # A Markdown title follows whitespace. Repository paths in this project do not
    # contain spaces, so retaining only the first token is unambiguous.
    return raw.split(maxsplit=1)[0]


def _local_links(relative: str, text: str):
    for match in LINK_RE.finditer(text):
        destination = _split_link_destination(match.group(1))
        parsed = urlsplit(destination)
        if parsed.scheme or parsed.netloc or destination.startswith("//"):
            continue
        yield destination, unquote(parsed.path), unquote(parsed.fragment)


def _resolve(source: str, target: str) -> str:
    if not target:
        return source
    source_parent = PurePosixPath(source).parent
    parts: list[str] = []
    for part in (source_parent / PurePosixPath(target)).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts).as_posix()


def _assert_exact_case(relative: str) -> None:
    current = ROOT
    for component in PurePosixPath(relative).parts:
        names = {entry.name for entry in current.iterdir()}
        assert component in names, f"local link has wrong Git case: {relative}"
        current /= component


def _strip_heading_markup(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return value.replace("`", "").replace("*", "").replace("_", "")


def _github_slug(value: str) -> str:
    value = _strip_heading_markup(value).strip().lower()
    value = "".join(
        char
        for char in value
        if char in " -_" or not unicodedata.category(char).startswith(("P", "S"))
    )
    return re.sub(r" +", "-", value)


def _anchors(relative: str) -> set[str]:
    text = _read(relative)
    anchors: set[str] = set()
    seen: Counter[str] = Counter()
    for match in re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", text, re.MULTILINE):
        base = _github_slug(match.group(2))
        suffix = seen[base]
        seen[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    anchors.update(
        match.group(1)
        for match in re.finditer(
            r"<a\s+(?:[^>]*?\s)?(?:id|name)=[\"']([^\"']+)[\"']", text, re.I
        )
    )
    return anchors


def _heading_levels(text: str) -> list[int]:
    return [len(match.group(1)) for match in re.finditer(r"^(#{1,6})\s+", text, re.M)]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        opening = re.match(r"^\s*(`{3,}|~{3,})(.*)$", lines[index])
        if not opening:
            index += 1
            continue
        marker = opening.group(1)
        info = opening.group(2).strip().split(maxsplit=1)[0].lower()
        body: list[str] = []
        index += 1
        while index < len(lines) and not re.match(
            rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[index]
        ):
            body.append(lines[index])
            index += 1
        assert index < len(lines), "unclosed Markdown code fence"
        blocks.append((info, "\n".join(body)))
        index += 1
    return blocks


def _table_shape(text: str) -> list[list[int]]:
    tables: list[list[int]] = []
    current: list[int] = []
    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append(len(re.split(r"(?<!\\)\|", stripped)) - 2)
        elif current:
            if len(current) > 1:
                tables.append(current)
            current = []
    return tables


def _normalized_literal(value: str) -> str:
    value = " ".join(value.split())
    for english, chinese in REVERSE_PAIR_MAP.items():
        value = value.replace(english, chinese)
        value = value.replace(PurePosixPath(english).name, PurePosixPath(chinese).name)
    return value


def _normalized_link_target(source: str, path: str, fragment: str) -> tuple[str, str]:
    resolved = _resolve(source, path)
    resolved = REVERSE_PAIR_MAP.get(resolved, resolved)
    # Heading text is translated, so a fragment may legitimately be language-local.
    return resolved, "present" if fragment else ""


def _changed_paths() -> set[str]:
    changed: set[str] = set()
    for line in _git("status", "--porcelain", "--untracked-files=all").splitlines():
        path = line[3:]
        if " -> " in path:
            changed.update(path.split(" -> ", maxsplit=1))
        else:
            changed.add(path)
    base = os.environ.get("BILINGUAL_BASE_REF")
    if base:
        changed.update(_git("diff", "--name-only", f"{base}...HEAD").splitlines())
    return {PurePosixPath(path.strip('"')).as_posix() for path in changed}


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL frontmatter must be first"
    end = text.find("\n---\n", 4)
    assert end >= 0, "SKILL frontmatter must be closed"
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        assert separator, f"invalid frontmatter line: {line}"
        result[key.strip()] = value.strip()
    return result


def test_markdown_inventory_is_explicit_and_complete():
    actual = _markdown_inventory()
    assert len(PAIR_BASES) == 19
    assert len(EXPECTED_MARKDOWN) == 39
    assert actual == EXPECTED_MARKDOWN, (
        f"missing={sorted(EXPECTED_MARKDOWN - actual)}; "
        f"unexpected={sorted(actual - EXPECTED_MARKDOWN)}"
    )


@pytest.mark.parametrize("chinese,english", PAIR_MAP.items())
def test_language_switches_are_reciprocal(chinese: str, english: str):
    chinese_text = "\n".join(_read(chinese).splitlines()[:20])
    english_text = "\n".join(_read(english).splitlines()[:20])
    chinese_targets = {
        _resolve(chinese, path) for _, path, _ in _local_links(chinese, chinese_text)
    }
    english_targets = {
        _resolve(english, path) for _, path, _ in _local_links(english, english_text)
    }
    assert english in chinese_targets, f"{chinese} must link to {english}"
    assert chinese in english_targets, f"{english} must link to {chinese}"


def test_documentation_hub_is_the_only_inline_bilingual_exception():
    inventory = _markdown_inventory()
    assert INLINE_BILINGUAL in inventory
    assert "docs/README.en.md" not in inventory
    hub = _read(INLINE_BILINGUAL)
    assert "## 中文导航" in hub
    assert "## English Navigation" in hub
    assert "Documentation Ownership" in hub


@pytest.mark.parametrize("chinese,english", PAIR_MAP.items())
def test_translation_structure_and_technical_literals_match(chinese: str, english: str):
    zh = _read(chinese)
    en = _read(english)
    assert _heading_levels(zh) == _heading_levels(en)
    assert CHECKBOX_RE.findall(zh) == CHECKBOX_RE.findall(en)
    assert [
        (len(indent), "ordered" if marker.endswith(".") else "unordered")
        for indent, marker in LIST_ITEM_RE.findall(zh)
    ] == [
        (len(indent), "ordered" if marker.endswith(".") else "unordered")
        for indent, marker in LIST_ITEM_RE.findall(en)
    ]
    assert _table_shape(zh) == _table_shape(en)
    # Translators may render an English number word as an Arabic numeral (for example,
    # "three tests" as "3 个测试"). Requiring the complete literal set preserves every
    # technical value without falsely requiring the same prose spelling frequency.
    assert set(NUMBER_RE.findall(zh)) == set(NUMBER_RE.findall(en))
    assert Counter(DATE_RE.findall(zh)) == Counter(DATE_RE.findall(en))
    assert Counter(
        token for token in COMMIT_HASH_RE.findall(zh) if any(char.isdigit() for char in token)
    ) == Counter(
        token for token in COMMIT_HASH_RE.findall(en) if any(char.isdigit() for char in token)
    )
    assert Counter(map(_normalized_literal, INLINE_CODE_RE.findall(zh))) == Counter(
        map(_normalized_literal, INLINE_CODE_RE.findall(en))
    )

    zh_blocks = _fenced_blocks(zh)
    en_blocks = _fenced_blocks(en)
    assert [info for info, _ in zh_blocks] == [info for info, _ in en_blocks]
    assert [body.count("\n") for _, body in zh_blocks] == [
        body.count("\n") for _, body in en_blocks
    ]
    for (info, zh_body), (_, en_body) in zip(zh_blocks, en_blocks):
        if info in EXECUTABLE_FENCE_LANGUAGES:
            assert zh_body == en_body, f"executable block differs in {chinese}/{english}"

    zh_targets = Counter(
        _normalized_link_target(chinese, path, fragment)
        for _, path, fragment in _local_links(chinese, zh)
    )
    en_targets = Counter(
        _normalized_link_target(english, path, fragment)
        for _, path, fragment in _local_links(english, en)
    )
    assert zh_targets == en_targets


def test_all_local_links_and_anchors_exist_with_exact_git_case():
    inventory = _markdown_inventory()
    anchor_cache: dict[str, set[str]] = {}
    for source in sorted(inventory):
        for raw, path, fragment in _local_links(source, _read(source)):
            resolved = _resolve(source, path)
            target = ROOT / resolved
            assert target.exists(), f"broken local link in {source}: {raw} -> {resolved}"
            _assert_exact_case(resolved)
            if fragment and target.is_file() and target.suffix.lower() in {".md", ".html"}:
                if target.suffix.lower() == ".md":
                    anchors = anchor_cache.setdefault(resolved, _anchors(resolved))
                    assert fragment.lower() in anchors, (
                        f"broken anchor in {source}: {raw}; "
                        f"#{fragment} not found in {resolved}"
                    )


def test_local_markdown_links_stay_in_the_source_language():
    # Historical records preserve their original literal links verbatim, and the hub is
    # intentionally bilingual inline. Both are explicit policy exceptions.
    exempt = {INLINE_BILINGUAL, "docs/SYSTEM_HISTORY.md", "docs/SYSTEM_HISTORY.en.md"}
    for source in sorted(EXPECTED_MARKDOWN - exempt):
        english_source = source.endswith(".en.md")
        counterpart = REVERSE_PAIR_MAP.get(source) or PAIR_MAP.get(source)
        for _, path, _ in _local_links(source, _read(source)):
            resolved = _resolve(source, path)
            if resolved == INLINE_BILINGUAL or resolved == counterpart:
                continue
            if resolved in PAIR_MAP or resolved in REVERSE_PAIR_MAP:
                assert resolved.endswith(".en.md") == english_source, (
                    f"cross-language local link in {source}: {resolved}"
                )


def test_changed_markdown_pairs_are_coupled_when_a_diff_is_available():
    changed = _changed_paths()
    for chinese, english in PAIR_MAP.items():
        assert (chinese in changed) == (english in changed), (
            f"paired Markdown must change together: {chinese}, {english}"
        )


def test_skill_sidecars_are_non_runtime_and_routing_stays_canonical():
    skill_zh = _read("skills/vitalis/SKILL.md")
    skill_en = _read("skills/vitalis/SKILL.en.md")
    frontmatter_zh = _frontmatter(skill_zh)
    frontmatter_en = _frontmatter(skill_en)
    assert set(frontmatter_zh) == set(frontmatter_en)
    assert frontmatter_zh["name"] == frontmatter_en["name"] == "vitalis"

    for name in WORKFLOWS:
        canonical = f"workflows/{name}.md"
        sidecar = f"skills/vitalis/workflows/{name}.en.md"
        assert canonical in skill_zh
        assert canonical in skill_en
        assert (ROOT / sidecar).is_file()
        assert not _read(f"skills/vitalis/workflows/{name}.md").startswith("---\n")
        assert not _read(sidecar).startswith("---\n")

    assert not re.search(r"workflows/[A-Za-z0-9_/-]+\.en\.md", skill_zh + skill_en)
    runtime_suffixes = {".py", ".js", ".json", ".toml", ".yaml", ".yml"}
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if relative.parts and relative.parts[0] in {".git", "tests"}:
            continue
        if path.is_file() and path.suffix.lower() in runtime_suffixes:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "SKILL.en.md" in text or re.search(r"workflows/[^\s\"']+\.en\.md", text):
                offenders.append(relative.as_posix())
    assert not offenders, f"runtime files load English reading sidecars: {offenders}"


def _mit_body(text: str) -> str:
    marker = "### MIT License\n\n"
    assert text.count(marker) == 1
    return text.split(marker, maxsplit=1)[1].strip() + "\n"


def test_authoritative_mit_license_is_identical_and_fixed():
    zh = _mit_body(_read("THIRD_PARTY_NOTICES.md"))
    en = _mit_body(_read("THIRD_PARTY_NOTICES.en.md"))
    assert zh == en
    assert hashlib.sha256(zh.encode("utf-8")).hexdigest() == MIT_SHA256


def test_api_translation_preserves_routes_and_executable_examples():
    zh = _read("docs/API.md")
    en = _read("docs/API.en.md")
    assert Counter(HTTP_ROUTE_RE.findall(zh)) == Counter(HTTP_ROUTE_RE.findall(en))
    zh_blocks = [body for info, body in _fenced_blocks(zh) if info in {"bash", "json"}]
    en_blocks = [body for info, body in _fenced_blocks(en) if info in {"bash", "json"}]
    assert zh_blocks == en_blocks
    required_literals = {
        "X-User-Id",
        "VITALIS_TIMEZONE",
        "workout_source",
        "workout_id",
        "open_health_insights",
        "association_only=true",
    }
    for literal in required_literals:
        assert literal in zh and literal in en


def test_system_declares_the_durable_bilingual_policy():
    zh = _read("SYSTEM.md")
    en = _read("SYSTEM.en.md")
    for literal in (
        "docs/README.md",
        "docs/README.en.md",
        "skills/vitalis/SKILL.en.md",
        "skills/vitalis/workflows/*.en.md",
        "tests/test_bilingual_markdown.py",
        "git diff --check",
        "THIRD_PARTY_NOTICES.md",
        "THIRD_PARTY_NOTICES.en.md",
        ".en.md",
    ):
        assert literal in zh and literal in en
    assert "新增、删除、重命名或语义更新" in zh
    assert "addition, deletion, rename, or semantic update" in en
    assert "同一变更" in zh and "same change" in en
    assert "zh-CN" in zh and "zh-CN" in en
    assert "唯一例外" in zh and "only exception" in en
    assert "不得翻译或改写" in zh and "must not be translated or rewritten" in en
    assert "不是运行时入口" in zh and "not runtime entry points" in en
