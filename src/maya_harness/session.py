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
* `userSetup.py` turns off the userSetup checksum: otherwise `quit` stops at the modal «Secure
  UserSetup Checksum verification», since `userSetup.py` is new in every session. A
  `userPrefs.mel` written in advance does not do it: Maya's first start in a new settings
  directory ignores it and writes its own.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .process import process_alive

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


@dataclass(frozen=True)
class Session:
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
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    pids = (state.runner_pid, state.maya_pid)
    if not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in pids):
        return None
    return state


def write_state(root: Path, state: State) -> None:
    (root / STATE_FILE).write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def remove_stale(
    temp: Path, alive: Callable[[int], bool] = process_alive, now: Callable[[], float] = time.time
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
        elif alive(state.runner_pid) or alive(state.maya_pid):
            continue
        shutil.rmtree(root, ignore_errors=True)
        removed.append(root)
    return removed


def sessions(temp: Path, alive: Callable[[int], bool] = process_alive) -> list[Session]:
    """The sessions whose Maya is alive, the latest started last."""
    found = []
    for root in temp.glob(f"{PREFIX}*"):
        state = read_state(root) if root.is_dir() else None
        if state is not None and alive(state.maya_pid):
            found.append(Session(root, state))
    return sorted(found, key=lambda session: session.state.started)


def make_session_dir(project: str, temp: Path) -> Path:
    remove_stale(temp)
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}{project}_{os.getpid()}_", dir=temp))


def environment(base: Mapping[str, str], root: Path, params: SessionParams, port: int) -> dict[str, str]:
    """The environment of the session's Maya: see the module docstring."""
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
        **{MARKER: str(work), PROJECT_VARIABLE: params.project},
    )
    if params.sys_path:
        env["PYTHONPATH"] = os.pathsep.join(str(path) for path in params.sys_path)
    if params.plug_in_path:
        env["MAYA_PLUG_IN_PATH"] = os.pathsep.join(str(path) for path in params.plug_in_path)
    return env


def user_setup(plugins: tuple[str, ...]) -> str:
    """`userSetup.py` of the session: the project's Maya plugins, then dcc-mcp."""
    return USER_SETUP.format(plugins=(*plugins, "dcc_mcp_maya_plugin"))


def prepare(root: Path, params: SessionParams, module: Path, base: Mapping[str, str], port: int) -> dict[str, str]:
    """Fills `root` with the session's settings; returns the environment its Maya starts with."""
    scripts = root / "app" / "2025" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "userSetup.py").write_text(user_setup(params.plugins), encoding="utf-8")
    modules = root / "modules"
    modules.mkdir()
    shutil.copy(module, modules / module.name)  # its paths are absolute
    (root / "work").mkdir()
    (root / "appdata").mkdir()
    return environment(base, root, params, port)
