"""A session: the directory, environment and state of one Maya the harness started.

* Every session has its own directory `%TEMP%/maya_harness_<project>_<pid>_*`:
  `app/` is `MAYA_APP_DIR` with the `userSetup.py` that loads dcc-mcp, `modules/` holds a copy
  of the dcc-mcp `.mod`, `appdata/` is `APPDATA`, `work/` holds what the session writes itself.
  Two Mayas with one `MAYA_APP_DIR` break each other's FBX import: `FBXPushSettings` writes a
  file there («Read settings failed»).
* `state.json` in the directory names its project, the harness and Maya pids and the address.
  `status`, `exec` and the cleanup read it, not the directory name: after the prefix comes the
  project, and a project name with `_` in it (`Retarget_Master`) cannot be split into a pid.
* A directory is kept while its harness or its Maya is alive. One without a readable
  `state.json` is removed only a day after it was made: a fresh one may not have written it yet.
* The environment is the user's minus everything that tells Maya or Python where to find code
  (`MAYA_*`, `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `VIRTUAL_ENV`); `PATH` stays as it is.
  `APPDATA` is the session's own: plugins keep their settings there (studiolibrary does), and a
  session must neither read the user's nor write over them. `LOCALAPPDATA` stays: Autodesk
  sign-in lives there.
* `MAYA_PLUG_IN_PATH` holds the plugin's own Maya plugins: loading them from an untrusted
  directory stops at the modal «Untrusted Plugin» dialog. `DCC_MCP_MAYA_SIDECAR=0` and
  `DCC_MCP_GATEWAY_PORT=0` keep this Maya out of the user's dcc-mcp gateway.
* The Maya is marked by `MAYA_HARNESS_WORK`, its work directory: the harness runs code only in
  a Maya with the marker, and an artist's Maya has none.
* `pylib/` holds a copy of this package and is the last on Maya's `PYTHONPATH`, after the
  project's `sys_path`: the code of a scenario imports `maya_harness.inmaya`. A copy, not the
  parent of the installed package: that is the `site-packages` of the project's `.venv`, with
  pytest, mypy and the rest.
* A Maya left running and forgotten lives on a hidden desktop, where nobody can close it by
  hand: `userSetup.py` starts a timer that quits Maya after `MAYA_HARNESS_TTL` idle minutes.
  Idle is the age of `work/last_call`, which `exec` touches before and after its call: the timer
  is on Maya's main thread and is silent while a call runs there, so without the touch after
  the call a call longer than the TTL would be followed by a quit at once. Qt runs the timer
  inside a modal dialog's loop too: a Maya stuck on a dialog still quits.
* `userSetup.py` turns off the userSetup checksum: otherwise `quit` stops at the modal «Secure
  UserSetup Checksum verification», since `userSetup.py` is new in every session. A
  `userPrefs.mel` written in advance does not do it: Maya's first start in a new settings
  directory ignores it and writes its own.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .process import maya_alive, process_alive

PREFIX = "maya_harness_"
STATE_FILE = "state.json"
MARKER = "MAYA_HARNESS_WORK"
PROJECT_VARIABLE = "MAYA_HARNESS_PROJECT"
MODULE_VARIABLE = "MAYA_HARNESS_DCC_MCP_MOD"
DEFAULT_MAYA = Path(r"C:\Program Files\Autodesk\Maya2025")
UNCLAIMED_AGE = 24 * 60 * 60  # seconds a directory without a readable state.json is kept
DROPPED = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV")

USER_SETUP = """
import os, traceback
from pathlib import Path

import maya.cmds as cmds

_status = Path(os.environ["MAYA_HARNESS_WORK"]) / "startup.txt"
_status.write_text("userSetup.py loaded", encoding="utf-8")
cmds.optionVar(intValue=("SafeModeUserSetupHashOption", 0))

_last_call = Path(os.environ["MAYA_HARNESS_WORK"]) / "last_call"
_last_call.touch()
_ttl = int(os.environ.get("MAYA_HARNESS_TTL") or 0)


def _maya_harness_quit_when_idle():
    import time

    try:
        idle = time.time() - _last_call.stat().st_mtime
    except OSError:
        return
    if idle > _ttl * 60:
        cmds.quit(force=True)


def _maya_harness_start_idle_timer():
    import __main__
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.timeout.connect(_maya_harness_quit_when_idle)
    timer.start(60 * 1000)
    __main__._maya_harness_idle_timer = timer  # a timer nobody holds is collected and stops


if _ttl > 0:
    cmds.evalDeferred(_maya_harness_start_idle_timer)

try:
    for _plugin in {plugins!r}:
        cmds.loadPlugin(_plugin, quiet=True)
except BaseException:
    _status.write_text(traceback.format_exc(), encoding="utf-8")
    raise
else:
    _status.write_text("dcc_mcp_maya_plugin loaded", encoding="utf-8")
"""


@dataclass(frozen=True)
class SessionParams:
    """What a project puts into its Maya. `sys_path` goes first on Maya's `sys.path`."""

    project: str
    project_root: Path
    sys_path: tuple[Path, ...] = ()
    plugins: tuple[str, ...] = ()
    plug_in_path: tuple[Path, ...] = ()


@dataclass(frozen=True)
class State:
    """The contents of `state.json`. `started` is seconds since the epoch, `ttl` minutes."""

    project: str
    project_root: str
    runner_pid: int
    maya_pid: int
    url: str
    started: float
    ttl: int = 0
    maya_created: int = 0


@dataclass(frozen=True)
class Record:
    """A session found on disk: its directory and its state.json."""

    root: Path
    state: State

    @property
    def work(self) -> Path:
        return self.root / "work"


def maya_exe(environ: Mapping[str, str]) -> Path:
    """`maya.exe` next to `MAYAPY_EXE`, else under `MAYA_LOCATION`, else under the default Maya 2025."""
    if environ.get("MAYAPY_EXE"):
        return Path(environ["MAYAPY_EXE"]).with_name("maya.exe")
    if environ.get("MAYA_LOCATION"):
        return Path(environ["MAYA_LOCATION"]) / "bin" / "maya.exe"
    return DEFAULT_MAYA / "bin" / "maya.exe"


def module_file(environ: Mapping[str, str], home: Path | None = None) -> Path:
    """The dcc-mcp `.mod`: `MAYA_HARNESS_DCC_MCP_MOD`, else the one Maya installs into Documents."""
    if environ.get(MODULE_VARIABLE):
        return Path(environ[MODULE_VARIABLE])
    return (home or Path.home()) / "Documents" / "maya" / "modules" / "dcc_mcp_maya.mod"


@dataclass(frozen=True)
class Module:
    """What the first line of a `.mod` says: `+ MAYAVERSION:2025 PLATFORM:win64 <name> <version> <dir>`,
    and the module-relative paths of the lines after it (`PYTHONPATH+:=python`)."""

    name: str
    version: str
    directory: Path
    paths: tuple[Path, ...]


def read_module(path: Path) -> Module:
    lines = path.read_text(encoding="utf-8").splitlines()
    fields = lines[0].split(None, 5) if lines else []
    if len(fields) < 6 or fields[0] != "+":
        raise ValueError(f"{path}: the first line is not `+ <conditions> <name> <version> <dir>`")
    directory = Path(fields[5])
    paths = tuple(directory / line.split(":=", 1)[1].strip() for line in lines[1:] if ":=" in line)
    return Module(fields[3], fields[4], directory, paths)


def free_port() -> int:
    """A currently free localhost port; dcc-mcp binds it as soon as Maya starts."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def read_state(root: Path) -> State | None:
    """The state of the session in `root`; None when `state.json` is missing, broken or holds a
    pid that is not a number."""
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        state = State(
            project=str(data["project"]),
            project_root=str(data["project_root"]),
            runner_pid=data["runner_pid"],
            maya_pid=data["maya_pid"],
            url=str(data["url"]),
            started=float(data["started"]),
            ttl=int(data.get("ttl", 0)),
            maya_created=int(data.get("maya_created", 0)),
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    pids = (state.runner_pid, state.maya_pid)
    if not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in pids):
        return None
    return state


def touch(work: Path) -> None:
    """Marks a call: the idle timer of the session counts from the mtime of `last_call`."""
    (work / "last_call").touch()


def write_state(root: Path, state: State) -> None:
    (root / STATE_FILE).write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def remove_stale(
    temp: Path,
    alive: Callable[[int], bool] = process_alive,
    now: Callable[[], float] = time.time,
    maya: Callable[[int, int], bool] = maya_alive,
) -> list[Path]:
    """Removes the session directories nobody holds; returns them."""
    removed = []
    for root in temp.glob(f"{PREFIX}*"):
        if not root.is_dir():
            continue
        state = read_state(root)
        if state is None:
            try:
                fresh = now() - root.stat().st_mtime < UNCLAIMED_AGE
            except OSError:
                continue
            if fresh:
                continue
        elif alive(state.runner_pid) or maya(state.maya_pid, state.maya_created):
            continue
        shutil.rmtree(root, ignore_errors=True)
        removed.append(root)
    return removed


def sessions(temp: Path, maya: Callable[[int, int], bool] = maya_alive) -> list[Record]:
    """The sessions whose Maya is alive, the latest started last."""
    found = []
    for root in temp.glob(f"{PREFIX}*"):
        state = read_state(root) if root.is_dir() else None
        if state is not None and maya(state.maya_pid, state.maya_created):
            found.append(Record(root, state))
    return sorted(found, key=lambda session: session.state.started)


def make_session_dir(project: str, temp: Path) -> Path:
    remove_stale(temp)
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}{project}_{os.getpid()}_", dir=temp))


def environment(base: Mapping[str, str], root: Path, params: SessionParams, port: int, ttl: int = 0) -> dict[str, str]:
    """The environment of the session's Maya: see the module docstring. `ttl` is in minutes, 0 for none."""
    env = {
        name: value
        for name, value in base.items()
        if not name.upper().startswith("MAYA_") and name.upper() not in DROPPED
    }
    work = root / "work"
    env.update(
        APPDATA=str(root / "appdata"),
        MAYA_APP_DIR=str(root / "app"),
        MAYA_MODULE_PATH=str(root / "modules"),
        MAYA_NO_HOME="1",
        MAYA_DISABLE_ADP="1",
        MAYA_DISABLE_CIP="1",
        MAYA_DISABLE_CLIC_IPM="1",
        DCC_MCP_MAYA_SIDECAR="0",
        DCC_MCP_GATEWAY_PORT="0",
        DCC_MCP_MAYA_PORT=str(port),
        DCC_MCP_MAYA_BOOTSTRAP_LOG=str(work / "dcc_mcp_bootstrap.jsonl"),
        PYTHONUTF8="1",
        MAYA_HARNESS_TTL=str(ttl),
        **{MARKER: str(work), PROJECT_VARIABLE: params.project},
    )
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in (*params.sys_path, root / "pylib"))
    if params.plug_in_path:
        env["MAYA_PLUG_IN_PATH"] = os.pathsep.join(str(path) for path in params.plug_in_path)
    return env


def user_setup(plugins: tuple[str, ...]) -> str:
    """`userSetup.py` of the session: the project's Maya plugins, then dcc-mcp."""
    return USER_SETUP.format(plugins=(*plugins, "dcc_mcp_maya_plugin"))


def prepare(
    root: Path, params: SessionParams, module: Path, base: Mapping[str, str], port: int, ttl: int = 0
) -> dict[str, str]:
    """Fills `root` with the session's settings; returns the environment its Maya starts with."""
    scripts = root / "app" / "2025" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "userSetup.py").write_text(user_setup(params.plugins), encoding="utf-8")
    modules = root / "modules"
    modules.mkdir()
    shutil.copy(module, modules / module.name)  # its paths are absolute
    (root / "work").mkdir()
    (root / "appdata").mkdir()
    package = Path(__file__).resolve().parent
    shutil.copytree(package, root / "pylib" / package.name, ignore=shutil.ignore_patterns("__pycache__"))
    return environment(base, root, params, port, ttl)
