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
* Every Maya holds a slot of this machine (`slots`): `start` takes one first, waiting for it
  while all are taken, and hands it to Maya; it comes free when Maya dies.
* As a library: `start` makes the directory and starts Maya, `Session.wait` waits for its
  server. `start` does not wait: a worker of a pool that finds nothing left to run once its Maya
  has started closes it without waiting for the server. `Session.close` kills Maya and removes
  the directory; `Session.release` leaves both, for a Maya that outlives this process
  (`maya-harness start`).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from . import process, slots, transport
from .process import maya_alive, process_alive

PREFIX = "maya_harness_"
NUMBERS = itertools.count(1)  # of the Mayas `start` starts: each gets a desktop of its own
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


class StartError(Exception):
    """No Maya to run code in: no maya.exe, no dcc-mcp module, or the Maya did not start."""


@dataclass
class Session:
    """A Maya this process started and its session directory; `start` makes one."""

    root: Path
    maya: process.MayaProcess
    url: str
    released: bool = False

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def pid(self) -> int:
        """The pid of the Maya, after `close` too: its server logs are named by it."""
        return self.maya.pid

    def wait(self, timeout: float) -> None:
        """Waits for the dcc-mcp server of the Maya and connects to it; StartError when it does not come."""
        try:
            transport.wait_for_server(self.url, self.work, self.maya.running, timeout)
            transport.connect(self.url, timeout, self.work)
        except (transport.TransportError, TimeoutError, OSError) as error:
            titles = self.maya.window_titles() if self.maya.running() else []
            logs = [str(log) for log in transport.server_logs(self.pid)]
            raise StartError(
                f"Maya {self.pid} did not start: {error}; its windows {titles}; its logs {logs}"
            ) from error

    def execute(self, code: str, timeout: float) -> transport.Result:
        """Runs `code` in the Maya; marks the call before and after it for the idle timer."""
        touch(self.work)
        try:
            return transport.execute(self.url, code, timeout)
        finally:
            touch(self.work)

    def close(self) -> None:
        """Kills the Maya and removes the directory; after `release`, does nothing."""
        if self.released:
            return
        self.maya.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def release(self) -> None:
        """Lets the Maya and the directory outlive this process."""
        self.maya.release()
        self.released = True

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


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


def start(
    params: SessionParams,
    *,
    visible: bool = False,
    env: Mapping[str, str] | None = None,
    ttl: int = 0,
    record: bool = False,
    temp: Path | None = None,
    slot_wait: float | None = None,
    slot_unneeded: Callable[[], bool] | None = None,
) -> Session:
    """Takes a slot of this machine (`slots`), then starts a Maya of `params` in a new session
    directory under `temp` (the system temp by default); does not wait for its server,
    `Session.wait` does.

    `env` is the environment to start from, `os.environ` by default: `maya.exe` and the dcc-mcp
    `.mod` are found by it, and the Maya's environment is made of it (see the module docstring).
    `record` writes `state.json`, so that `status`, `exec` and `stop` find the session; `ttl` is in idle
    minutes, 0 for none. `slot_wait` is how many seconds to wait for a free slot, None for no
    limit; StartError when none came free. `slot_unneeded`, asked while it waits, ends the wait
    with StartError once it is True."""
    base = os.environ if env is None else env
    exe = maya_exe(base)
    if not exe.is_file():
        raise StartError(f"no Maya at {exe}; set MAYAPY_EXE or MAYA_LOCATION")
    module = module_file(base)
    if not module.is_file():
        raise StartError(f"no dcc-mcp-maya module at {module}; set {MODULE_VARIABLE} to its .mod file")
    try:
        slot = slots.take(slot_wait, unneeded=slot_unneeded)
    except slots.NoSlot as error:
        raise StartError(str(error)) from error
    try:
        root = make_session_dir(params.project, temp or Path(tempfile.gettempdir()))
    except BaseException:
        slot.release()
        raise
    try:
        port = free_port()
        maya = process.MayaProcess(exe, prepare(root, params, module, base, port, ttl), visible, next(NUMBERS))
    except BaseException:  # Ctrl+C too: remove_stale keeps a directory without state.json for a day
        shutil.rmtree(root, ignore_errors=True)
        slot.release()
        raise
    started = Session(root, maya, f"http://127.0.0.1:{port}/mcp")
    try:
        slot.give(maya.handle)  # here: on an error, the except below kills Maya
        holder = {
            "pid": maya.pid,
            "created": maya.created,
            "project": params.project,
            "command": "start" if record else "run",
            "url": started.url,
            "work": str(started.work),
            "time": time.time(),
        }
        slot.describe(holder)
        for log in transport.server_logs(maya.pid):  # Windows reuses pids: a stale log holds a stale address
            log.unlink(missing_ok=True)
        if record:
            state = State(
                params.project,
                str(params.project_root),
                os.getpid(),
                maya.pid,
                started.url,
                time.time(),
                ttl,
                maya.created,
            )
            write_state(root, state)
    except BaseException:
        started.close()
        slot.release()
        raise
    return started
