"""The library for the code of a scenario, inside a Maya of maya_harness.

A session copies this package into its own `pylib/` and puts that on Maya's `PYTHONPATH`: not
the parent of the installed package, which is the `site-packages` of the project's `.venv`.

* `Check` holds the expectations of a run: each failure is `<name>: <message>`, and `run` takes
  the name up to the first `:` for a control's verdict.
* `run_sections` runs a scenario's sections: a control runs its own section alone.
* `no_prompts` keeps a missing reference file from opening a dialog, which hangs a hidden Maya.
* `work_dir` is the session's work directory; outside a Maya of maya_harness it raises: a
  scenario starts with a new scene and would discard an artist's open scene.

`maya.cmds` is imported inside the functions that need it: the package's tests run without Maya.
"""

from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

MARKER = "MAYA_HARNESS_WORK"  # the same name as session.MARKER: this module imports nothing of the package


def work_dir() -> Path:
    """The work directory of this Maya's session."""
    work = os.environ.get(MARKER)
    if not work:
        raise RuntimeError(f"No {MARKER}: a scenario starts with a new scene, run it with maya-harness.")
    return Path(work)


class Check:
    """The expectations of a run: each failure is `<name>: <message>`, and the runner takes the name up to
    the first `:` for a control's verdict. The actual value of an expectation goes to the report."""

    def __init__(self, control: str = "") -> None:
        self.control = control
        self.failures: list[str] = []
        self.report: dict[str, object] = {"control": control, "failures": self.failures}

    def expect(self, name: str, actual: object, expected: object) -> None:
        self.report[name] = actual
        if actual != expected:
            self.failures.append(f"{name}: {actual!r} != {expected!r}")

    def fail(self, name: str, message: str) -> None:
        self.failures.append(f"{name}: {message}")


Section = Callable[[Check], None]


def run_sections(
    sections: dict[str, Section], controls: dict[str, str], control: str = "", section: str = ""
) -> dict[str, object]:
    """The report of the scenario's sections, in their order: a control runs its own section alone, else
    the chosen one or all of them.

    Each section prepares its own scene, so any one runs alone. A section that raises fails as
    `<section>.raised`, its traceback in the report, and the next sections go on."""
    if control and control not in controls:
        raise ValueError(f"No control {control!r}: {', '.join(controls)}")
    chosen = controls[control] if control else section
    if chosen and chosen not in sections:
        raise ValueError(f"No section {chosen!r}: {', '.join(sections)}")
    check = Check(control)
    seconds: dict[str, float] = {}
    check.report["sections"] = seconds
    for name, body in sections.items():
        if chosen and name != chosen:
            continue
        start = time.monotonic()
        try:
            body(check)
        except Exception as error:
            check.fail(f"{name}.raised", f"{type(error).__name__}: {error}")
            check.report[f"{name}.traceback"] = traceback.format_exc()
        seconds[name] = round(time.monotonic() - start, 1)
    return check.report


@contextmanager
def no_prompts() -> Iterator[None]:
    """A missing reference file must not open a dialog: on the hidden desktop it hangs the run."""
    import maya.cmds as cmds  # type: ignore[import-not-found]

    prompt = cmds.file(query=True, prompt=True)
    cmds.file(prompt=False)
    try:
        yield
    finally:
        cmds.file(prompt=prompt)
