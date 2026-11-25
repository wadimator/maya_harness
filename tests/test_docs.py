"""Guard: links and file paths in the Markdown documents point at files that exist.

Modules and docs get renamed; a dead link or a stale path in backticks is invisible until
someone follows it. Only local targets are checked, no external links: a relative link as
GitHub resolves it, and a path such as ``maya_harness/transport.py`` that must exist under at
least one of the places a reader would look for it (see ``path_roots``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".venv",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    "tasks",
}

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#")
REPO_PATH = re.compile(r"`([\w.\-]+(?:/[\w.\-]+)+\.(?:py|md|toml|json|ps1|sh|mel))`")


def markdown_files() -> list[Path]:
    return sorted(
        path for path in PROJECT_ROOT.rglob("*.md") if not SKIP_DIRS & set(path.relative_to(PROJECT_ROOT).parts)
    )


def relative_links(document: Path) -> list[str]:
    return [
        target
        for target in LINK.findall(document.read_text(encoding="utf-8"))
        if not target.startswith(SKIP_PREFIXES) and not Path(target).is_absolute()
    ]


def path_roots(document: Path) -> list[Path]:
    """Where a path in backticks may start: the root, the directory above it (a path that
    begins with the package name), the document's own directory and every package in src/."""
    packages = sorted(path for path in (PROJECT_ROOT / "src").iterdir() if path.is_dir())
    return [PROJECT_ROOT, PROJECT_ROOT.parent, document.parent, *packages]


def _id(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def test_markdown_files_are_found() -> None:
    assert markdown_files(), "no Markdown files collected -- check SKIP_DIRS"


@pytest.mark.parametrize("document", markdown_files(), ids=_id)
def test_relative_links_resolve(document: Path) -> None:
    broken = [target for target in relative_links(document) if not (document.parent / target.split("#")[0]).exists()]

    assert broken == []


@pytest.mark.parametrize("document", markdown_files(), ids=_id)
def test_repository_paths_exist(document: Path) -> None:
    roots = path_roots(document)
    missing = [
        path
        for path in REPO_PATH.findall(document.read_text(encoding="utf-8"))
        if not any((root / path).exists() for root in roots)
    ]

    assert missing == []
