import zipfile
from pathlib import Path

import pytest
import release

PYPROJECT = '[project]\nname = "maya-harness"\nversion = "0.4.0"\n'
LINK = "https://github.com/o/maya_harness/releases/download/v0.4.0/maya_harness-0.4.0-py3-none-any.whl"
NEW_LINK = "https://github.com/o/maya_harness/releases/download/v0.4.1/maya_harness-0.4.1-py3-none-any.whl"


def record(message: str, merge: bool) -> str:
    """One commit as `git log --format=LOG_FORMAT` prints it."""
    parents = "aaa bbb" if merge else "aaa"
    return f"{parents}\x1f{message}\n\x1e\n"


MERGE = """feat: сеанс Maya как API

Первый абзац что и зачем,
перенесённый на две строки.

Второй абзац что и зачем.

Вместе с ним:

* cli — пункт.

Проверено: just check.
"""


def test_newer_version_compares_numbers() -> None:
    release.check_newer("0.10.0", "0.9.0")


@pytest.mark.parametrize("new", ["0.4.0", "0.3.9", "0.4", "v0.4.1", "0.4.1 "])
def test_not_newer_or_malformed_version_is_refused(new: str) -> None:
    with pytest.raises(release.Refusal):
        release.check_newer(new, "0.4.0")


def test_bump_pyproject() -> None:
    assert release.bump_pyproject(PYPROJECT, "0.4.0", "0.4.1") == PYPROJECT.replace("0.4.0", "0.4.1")


@pytest.mark.parametrize("text", ['[project]\nversion = "0.3.0"\n', PYPROJECT + '[tool.x]\nversion = "0.4.0"\n'])
def test_bump_pyproject_needs_exactly_one_match(text: str) -> None:
    with pytest.raises(ValueError):
        release.bump_pyproject(text, "0.4.0", "0.4.1")


def test_bump_readme() -> None:
    assert release.bump_readme(f"url = {LINK}\n", "0.4.1") == f"url = {NEW_LINK}\n"


@pytest.mark.parametrize("text", ["no link\n", f"{LINK}\n{LINK}\n"])
def test_bump_readme_needs_exactly_one_match(text: str) -> None:
    with pytest.raises(ValueError):
        release.bump_readme(text, "0.4.1")


def test_notes_start_with_the_draft_marker() -> None:
    notes = release.release_notes(record("docs(readme): колесо v0.4.0", merge=False))
    assert notes.splitlines()[0] == release.DRAFT_MARKER


def test_notes_of_a_merge_are_its_what_and_why() -> None:
    notes = release.release_notes(record(MERGE, merge=True))
    assert "## feat: сеанс Maya как API" in notes
    assert "Первый абзац что и зачем, перенесённый на две строки." in notes
    assert "Второй абзац что и зачем." in notes
    assert "Вместе с" not in notes
    assert "cli" not in notes
    assert "Проверено" not in notes


def test_notes_of_a_merge_stop_at_together_with_them() -> None:
    message = MERGE.replace("Вместе с ним:", "Вместе с ними:")
    assert "Вместе с" not in release.release_notes(record(message, merge=True))


def test_notes_of_a_merge_stop_at_checked() -> None:
    message = "feat: x\n\nЧто и зачем.\n\nПроверено: just check.\n"
    notes = release.release_notes(record(message, merge=True))
    assert "Что и зачем." in notes
    assert "Проверено" not in notes


def test_notes_of_a_direct_commit_are_its_title() -> None:
    log = record(MERGE, merge=True) + record("docs: правка\n\nТело прямого коммита.", merge=False)
    notes = release.release_notes(log)
    assert notes.index("## feat: сеанс Maya как API") < notes.index("## docs: правка")
    assert "Тело прямого коммита" not in notes


def test_wheel_version(tmp_path: Path) -> None:
    path = tmp_path / release.wheel_name("0.4.1")
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("maya_harness/__init__.py", "")
        wheel.writestr("maya_harness-0.4.1.dist-info/METADATA", "Metadata-Version: 2.4\nName: x\nVersion: 0.4.1\n")
    assert release.wheel_version(path) == "0.4.1"
