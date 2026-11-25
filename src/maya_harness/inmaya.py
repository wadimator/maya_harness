"""The library for the code of a scenario, inside a Maya of maya_harness.

A session copies this package into its own `pylib/` and puts that on Maya's `PYTHONPATH`: not
the parent of the installed package, which is the `site-packages` of the project's `.venv`.

* `Check` holds the expectations of a run: each failure is `<name>: <message>`, and `run` takes
  the name up to the first `:` for a control's verdict.
* `run_sections` runs a scenario's sections: a control runs its own section alone.
* `no_prompts` keeps a missing reference file from opening a dialog, which hangs a hidden Maya.
* `work_dir` is the session's work directory; outside a Maya of maya_harness it raises: a
  scenario starts with a new scene and would discard an artist's open scene.

The runner calls the rest; a scenario does not:

* `startup` is the session's `userSetup.py`: it reads `PLUGINS` and `TTL` from the environment.
* `reload` drops packages from `sys.modules`, so the next import reads them from disk.
* `run_scenario` imports a scenario fresh and runs it.

The names of the files in the work directory and of the variables are here: the code in Maya
and the runner both use them, and Maya gets only this module of the package.

`maya.cmds` and `PySide6` are imported inside the functions that need them: the package's tests
run without Maya.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

MARKER = "MAYA_HARNESS_WORK"  # the work directory of the session; an artist's Maya has none
PLUGINS = "MAYA_HARNESS_PLUGINS"  # the project's Maya plugins, `os.pathsep` between them
TTL = "MAYA_HARNESS_TTL"  # idle minutes before Maya quits; 0 for never
LAST_CALL = "last_call"  # in the work directory: its mtime is the end of the last call
STARTUP = "startup.txt"  # in the work directory: how far `startup` got, or its traceback
SCENARIO_PREFIX = "check_"  # a scenario's module name; the modules with it are dropped before each run


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


def startup() -> None:
    """The session's `userSetup.py`: turns off its checksum, starts the idle timer and loads the
    project's Maya plugins, then dcc-mcp. `STARTUP` tells how far it got; an error is left to
    the caller, which writes its traceback there."""
    import maya.cmds as cmds

    work = work_dir()
    status = work / STARTUP
    status.write_text("userSetup.py loaded", encoding="utf-8")
    cmds.optionVar(intValue=("SafeModeUserSetupHashOption", 0))
    last_call = work / LAST_CALL
    last_call.touch()
    ttl = int(os.environ.get(TTL) or 0)
    if ttl > 0:
        cmds.evalDeferred(lambda: _start_idle_timer(last_call, ttl))
    plugins = [plugin for plugin in os.environ.get(PLUGINS, "").split(os.pathsep) if plugin]
    for plugin in (*plugins, "dcc_mcp_maya_plugin"):
        cmds.loadPlugin(plugin, quiet=True)
    status.write_text("dcc_mcp_maya_plugin loaded", encoding="utf-8")


def _start_idle_timer(last_call: Path, ttl: int) -> None:
    """Quits Maya once `last_call` is older than `ttl` minutes; looks once a minute."""
    import maya.cmds as cmds
    from PySide6.QtCore import QTimer  # type: ignore[import-not-found]

    import __main__

    def quit_when_idle() -> None:
        try:
            idle = time.time() - last_call.stat().st_mtime
        except OSError:
            return
        if idle > ttl * 60:
            cmds.quit(force=True)

    timer = QTimer()
    timer.timeout.connect(quit_when_idle)
    timer.start(60 * 1000)
    __main__._maya_harness_idle_timer = timer  # a timer nobody holds is collected and stops


def reload(packages: Iterable[str], prefix: str = "") -> None:
    """Drops from `sys.modules` the modules of `packages` and those whose name starts with
    `prefix`: the next import reads them from disk."""
    tops = set(packages)
    for name in tuple(sys.modules):
        if name.split(".")[0] in tops or (prefix and name.startswith(prefix)):
            del sys.modules[name]


def run_scenario(directory: str, module: str, params: dict[str, str], packages: Iterable[str]) -> str:
    """The report of the scenario `module` from `directory`, as JSON: the scenarios and
    `packages` are dropped first, so the code on disk is what runs."""
    while directory in sys.path:
        sys.path.remove(directory)
    sys.path.insert(0, directory)
    reload(packages, SCENARIO_PREFIX)
    scenario = importlib.import_module(module)
    return json.dumps(scenario.run(**params), ensure_ascii=False, default=str)
