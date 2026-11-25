import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from maya_harness import cli, slots

CREATE_NO_WINDOW = 0x08000000
TABLE = """
[[run]]
scenario = "check_fps"
seconds = 6

[[run]]
scenario = "check_bake"
seconds = 34

[[run]]
scenario = "check_bake"
control = "no_round"
expect = "film.whole_frames"
seconds = 8
"""


@pytest.fixture(autouse=True)
def own_slots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Slots of the test, not of the machine: with every slot of the machine taken by real Mayas,
    a test that starts a fake one would wait for a slot without a limit."""
    directory = tmp_path / "slots"
    monkeypatch.setattr(slots, "DIR", directory)
    monkeypatch.delenv(slots.VARIABLE, raising=False)
    return directory


@pytest.fixture
def stub() -> Iterator[Callable[..., subprocess.Popen[str]]]:
    """Starts `python -c <code>` without a window, in place of Maya, and kills it after the test.

    The interpreter is `sys._base_executable`: `python.exe` of `.venv` is a launcher that runs the
    real one as its child, so the pid of a stub started from `sys.executable` is the launcher's,
    and a test that kills it leaves the stub alive."""
    started: list[subprocess.Popen[str]] = []

    def start(code: str, **options: Any) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys._base_executable, "-c", code],  # type: ignore[attr-defined]
            creationflags=CREATE_NO_WINDOW,
            text=True,
            **options,
        )
        started.append(child)
        return child

    yield start
    for child in started:
        child.kill()
        child.wait(5)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with two scenarios and its table of runs; the current directory, its sessions in `temp`."""
    root = tmp_path / "plugin"
    checks = root / "checks"
    checks.mkdir(parents=True)
    for name in ("check_fps", "check_bake"):
        (checks / f"{name}.py").write_text("def run(**params): ...\n", encoding="utf-8")
    (checks / "runs.toml").write_text(TABLE, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.maya_harness]\nreload = ["plugin"]\n'
        '[tool.maya_harness.scenarios]\ndir = "checks"\nruns = "checks/runs.toml"\n',
        encoding="utf-8",
    )
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(cli, "temp_dir", lambda: tmp_path / "temp")
    monkeypatch.chdir(root)
    return root
