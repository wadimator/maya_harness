import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from maya_harness import slots

CREATE_NO_WINDOW = 0x08000000


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
