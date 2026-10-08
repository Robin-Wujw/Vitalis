"""Check documentation links and the standalone Skill's local references."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import markdown
from markdown.extensions.toc import slugify_unicode


ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("skills/vitalis")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        key = "src" if tag == "img" else "href" if tag == "a" else None
        if key and values.get(key):
            self.links.append(values[key])


def _render(path: Path) -> _Links:
    parsed = _Links()
    parsed.feed(markdown.markdown(
        path.read_text(encoding="utf-8"),
        extensions=["fenced_code", "tables", "toc"],
        extension_configs={"toc": {"slugify": slugify_unicode}},
    ))
    return parsed


def _exact_case(source: Path, path: str) -> bool:
    cursor = source.parent
    for segment in PurePosixPath(path).parts:
        if segment in {"", "."}:
            continue
        if segment == "..":
            cursor = cursor.parent
            continue
        if not cursor.is_dir() or not any(item.name == segment for item in cursor.iterdir()):
            return False
        cursor = cursor / segment
    return True


def check_links(files: list[Path], root: Path, *, skill_dir: Path | None = None) -> list[str]:
    root = root.resolve()
    cache: dict[Path, _Links] = {}
    errors: list[str] = []

    def rendered(path: Path) -> _Links:
        if path not in cache:
            cache[path] = _render(path)
        return cache[path]

    for source in files:
        source = source.resolve()
        for url in rendered(source).links:
            parts = urlsplit(url)
            if parts.scheme or parts.netloc or url.startswith("//"):
                continue
            if parts.path.startswith("/api"):
                continue
            target = (source.parent / unquote(parts.path)).resolve() if parts.path else source
            if not target.is_relative_to(root):
                errors.append(f"{source.relative_to(root)}: link escapes repository: {url}")
                continue
            if skill_dir is not None and source.is_relative_to(skill_dir) and not target.is_relative_to(skill_dir):
                errors.append(f"{source.relative_to(root)}: Skill link escapes bundle: {url}")
                continue
            if not target.is_file() and not (target.is_dir() and any(target.iterdir())):
                errors.append(f"{source.relative_to(root)}: missing file: {url}")
                continue
            if parts.path and not _exact_case(source, unquote(parts.path)):
                errors.append(f"{source.relative_to(root)}: incorrect path casing: {url}")
                continue
            if parts.fragment and target.suffix.lower() in {".md", ".markdown", ".mdx"}:
                fragment = unquote(parts.fragment)
                if fragment not in rendered(target).ids:
                    errors.append(f"{source.relative_to(root)}: missing anchor: {url}")
    return errors


def _is_documentation(path: Path) -> bool:
    return path.suffix.lower() in {".md", ".markdown", ".mdx"}


def documentation_files(root: Path) -> list[Path]:
    """Return existing documentation tracked by Git, including nested paths.

    A Git inventory avoids scanning vendored caches, local virtualenvs, and
    user-supplied files. The fallback is used by tests with a temporary tree.
    """
    root = root.resolve()
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(top_level).resolve() != root:
            raise subprocess.CalledProcessError(1, "git")
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        candidates = (path for path in root.rglob("*") if path.is_file())
    else:
        tracked = [root / raw for raw in result.stdout.decode("utf-8").split("\0") if raw]
        # Authored examples are checked before they are staged or committed.
        examples = root / "docs" / "examples" / "reports"
        candidates = set(tracked) | set(examples.glob("*.md"))
    excluded = {
        root / "docs" / "Vitalis_Experience_Review_2026-10-08",
        root / ".git",
    }
    external_parts = {
        ".venv", "node_modules", ".codex_pydeps", "__pycache__", ".cache",
        "vendor", "third_party", "third-party", "site-packages",
    }
    files = []
    for path in candidates:
        if not path.is_file() or not _is_documentation(path):
            continue
        if any(path == item or item in path.parents for item in excluded):
            continue
        if any(part.lower() in external_parts for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    files = documentation_files(ROOT)
    skill_dir = (ROOT / SKILL).resolve()
    errors = check_links(files, ROOT, skill_dir=skill_dir)
    for path in ("README.md", "docs/README.md", "skills/vitalis/SKILL.md"):
        if ROOT.joinpath(path) not in files:
            errors.append(f"missing documentation entrypoint: {path}")
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"checked {len(files)} Markdown files: links and Skill references valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
