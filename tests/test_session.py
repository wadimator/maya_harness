import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import process, session, slots
from maya_harness.session import SessionParams, State

PROJECT = "Retarget_Master"  # `_` in the name: the directory name cannot be split into a pid
DEAD, ALIVE, OTHER_ALIVE = 101, 202, 303
NOW = 1_000_000_000.0
GIVE = process.give  # the real one: `installed` replaces it


def alive(pid: int) -> bool:
    return pid in (ALIVE, OTHER_ALIVE)


def maya(pid: int, created: int) -> bool:
    return alive(pid)


def make_dir(temp: Path, runner_pid: object, maya_pid: object, *, age: float = 0.0, created: int = 0) -> Path:
    root = temp / f"{session.PREFIX}{PROJECT}_{DEAD}_abc{runner_pid}{maya_pid}"
    root.mkdir()
    state = {
        "project": PROJECT,
        "project_root": "C:/x",
        "runner_pid": runner_pid,
        "maya_pid": maya_pid,
        "url": "http://127.0.0.1:1/mcp",
        "started": NOW,
        "ttl": 60,
    }
    if created:  # without it, state.json is one from before the creation time
        state["maya_created"] = created
    (root / session.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    os.utime(root, (NOW - age, NOW - age))
    return root


def remove(temp: Path) -> list[Path]:
    return session.remove_stale(temp, alive=alive, now=lambda: NOW, maya=maya)


def test_dead_runner_and_dead_maya_is_removed(tmp_path: Path) -> None:
    root = make_dir(tmp_path, DEAD, DEAD)
    assert remove(tmp_path) == [root]
    assert not root.exists()


def test_live_runner_keeps_the_directory(tmp_path: Path) -> None:
    root = make_dir(tmp_path, ALIVE, DEAD)
    assert remove(tmp_path) == []
    assert root.exists()


def test_live_maya_keeps_the_directory_of_a_dead_runner(tmp_path: Path) -> None:
    root = make_dir(tmp_path, DEAD, ALIVE)
    assert remove(tmp_path) == []
    assert root.exists()


def test_garbage_state_is_removed_when_old(tmp_path: Path) -> None:
    root = make_dir(tmp_path, "x", ALIVE, age=session.UNCLAIMED_AGE + 1)
    assert remove(tmp_path) == [root]


def test_garbage_state_is_kept_when_fresh(tmp_path: Path) -> None:
    root = make_dir(tmp_path, "x", ALIVE)
    assert remove(tmp_path) == []
    assert root.exists()


def test_fresh_directory_without_state_is_kept(tmp_path: Path) -> None:
    root = tmp_path / f"{session.PREFIX}{PROJECT}_{DEAD}_new"
    root.mkdir()
    os.utime(root, (NOW, NOW))
    assert remove(tmp_path) == []
    old = tmp_path / f"{session.PREFIX}{PROJECT}_{DEAD}_old"
    old.mkdir()
    os.utime(old, (NOW - session.UNCLAIMED_AGE - 1,) * 2)
    assert remove(tmp_path) == [old]


def test_sessions_lists_live_mayas_oldest_first(tmp_path: Path) -> None:
    make_dir(tmp_path, DEAD, DEAD)
    first = make_dir(tmp_path, DEAD, ALIVE)
    second = make_dir(tmp_path, ALIVE, OTHER_ALIVE)
    state = json.loads((second / session.STATE_FILE).read_text(encoding="utf-8"))
    state["started"] = NOW + 1
    (second / session.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert [found.root for found in session.sessions(tmp_path, maya=maya)] == [first, second]
    assert session.read_state(first).maya_created == 0


def test_a_maya_pid_counts_only_with_its_creation_time(tmp_path: Path) -> None:
    """Windows reuses pids: the recorded creation time goes along with the Maya pid."""
    root = make_dir(tmp_path, DEAD, ALIVE, created=7)

    def created_at(time: int) -> Callable[[int, int], bool]:
        return lambda pid, created: alive(pid) and created == time

    assert [found.root for found in session.sessions(tmp_path, maya=created_at(7))] == [root]
    assert session.sessions(tmp_path, maya=created_at(8)) == []
    assert session.remove_stale(tmp_path, alive=alive, now=lambda: NOW, maya=created_at(7)) == []
    assert session.remove_stale(tmp_path, alive=alive, now=lambda: NOW, maya=created_at(8)) == [root]


def test_state_round_trip(tmp_path: Path) -> None:
    state = State(PROJECT, "C:/x", 1, 2, "http://127.0.0.1:1/mcp", NOW, 60, 134_000_000_000_000_000)
    session.write_state(tmp_path, state)
    assert session.read_state(tmp_path) == state


PARAMS = SessionParams(
    project=PROJECT,
    project_root=Path("C:/project"),
    sys_path=(Path("C:/project/src"), Path("C:/project/vendor")),
    plugins=("fbxmaya",),
    plug_in_path=(Path("C:/project/maya_plugins"),),
)
BASE = {
    "PATH": r"C:\project\.venv\Scripts;C:\Windows",
    "APPDATA": r"C:\Users\u\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
    "MAYA_MODULE_PATH": r"C:\user\modules",
    "Maya_Script_Path": r"C:\user\scripts",
    "PYTHONPATH": r"C:\user\python",
    "PYTHONHOME": r"C:\python",
    "PYTHONSTARTUP": r"C:\startup.py",
    "VIRTUAL_ENV": r"C:\project\.venv",
    "USERNAME": "u",
}


def test_environment_drops_what_points_maya_and_python_at_code() -> None:
    env = session.environment(BASE, Path("C:/s"), PARAMS, 4321)
    assert "Maya_Script_Path" not in env
    assert "PYTHONHOME" not in env
    assert "PYTHONSTARTUP" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["MAYA_MODULE_PATH"] == str(Path("C:/s/modules"))
    project_paths = [str(Path("C:/project/src")), str(Path("C:/project/vendor"))]
    assert env["PYTHONPATH"] == os.pathsep.join([*project_paths, str(Path("C:/s/pylib"))])
    assert env["MAYA_PLUG_IN_PATH"] == str(Path("C:/project/maya_plugins"))


def test_environment_keeps_path_localappdata_and_the_rest() -> None:
    env = session.environment(BASE, Path("C:/s"), PARAMS, 4321)
    assert env["PATH"] == BASE["PATH"]
    assert env["LOCALAPPDATA"] == BASE["LOCALAPPDATA"]
    assert env["USERNAME"] == "u"


def test_environment_isolates_appdata_inside_the_session() -> None:
    env = session.environment(BASE, Path("C:/s"), PARAMS, 4321)
    assert env["APPDATA"] == str(Path("C:/s/appdata"))


def test_environment_sets_the_session() -> None:
    env = session.environment(BASE, Path("C:/s"), PARAMS, 4321)
    assert env["MAYA_APP_DIR"] == str(Path("C:/s/app"))
    assert env["MAYA_HARNESS_WORK"] == str(Path("C:/s/work"))
    assert env["MAYA_HARNESS_PROJECT"] == PROJECT
    assert env["DCC_MCP_MAYA_PORT"] == "4321"
    assert env["DCC_MCP_MAYA_SIDECAR"] == "0"
    assert env["DCC_MCP_GATEWAY_PORT"] == "0"
    assert env["PYTHONUTF8"] == "1"
    for name in ("MAYA_NO_HOME", "MAYA_DISABLE_ADP", "MAYA_DISABLE_CIP", "MAYA_DISABLE_CLIC_IPM"):
        assert env[name] == "1"


def test_environment_without_paths_has_only_the_package_copy() -> None:
    env = session.environment(BASE, Path("C:/s"), SessionParams(PROJECT, Path("C:/p")), 1)
    assert env["PYTHONPATH"] == str(Path("C:/s/pylib"))
    assert "MAYA_PLUG_IN_PATH" not in env


def test_prepare_copies_the_package_for_the_code_in_maya(tmp_path: Path) -> None:
    module = tmp_path / "dcc_mcp_maya.mod"
    module.write_text("+ MAYAVERSION:2025 PLATFORM:win64 dcc_mcp_maya 0.9.43 C:/m\n", encoding="utf-8")
    root = tmp_path / "session"
    env = session.prepare(root, SessionParams(PROJECT, tmp_path), module, {}, 1)
    copy = Path(env["PYTHONPATH"]) / "maya_harness"
    assert (copy / "inmaya.py").read_bytes() == (Path(session.__file__).parent / "inmaya.py").read_bytes()
    assert not list(copy.rglob("__pycache__"))


def test_user_setup_loads_project_plugins_then_dcc_mcp() -> None:
    code = session.user_setup(("fbxmaya", "gameFbxExporter"))
    compile(code, "userSetup.py", "exec")
    assert "('fbxmaya', 'gameFbxExporter', 'dcc_mcp_maya_plugin')" in code
    assert 'SafeModeUserSetupHashOption", 0' in code


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"MAYAPY_EXE": r"D:\Maya\bin\mayapy.exe", "MAYA_LOCATION": r"E:\Maya"}, Path(r"D:\Maya\bin\maya.exe")),
        ({"MAYA_LOCATION": r"E:\Maya"}, Path(r"E:\Maya\bin\maya.exe")),
        ({}, session.DEFAULT_MAYA / "bin" / "maya.exe"),
    ],
)
def test_maya_exe(environ: dict[str, str], expected: Path) -> None:
    assert session.maya_exe(environ) == expected


def test_module_file() -> None:
    assert session.module_file({session.MODULE_VARIABLE: r"D:\x.mod"}) == Path(r"D:\x.mod")
    home = Path("C:/Users/u")
    assert session.module_file({}, home) == home / "Documents" / "maya" / "modules" / "dcc_mcp_maya.mod"


def test_read_module(tmp_path: Path) -> None:
    mod = tmp_path / "dcc_mcp_maya.mod"
    mod.write_text(
        "+ MAYAVERSION:2025 PLATFORM:win64 dcc_mcp_maya 0.9.43 C:/My Modules/dcc-mcp\n"
        "PYTHONPATH+:=python\nPLUG_IN_PATH+:=plug-ins\n",
        encoding="utf-8",
    )
    module = session.read_module(mod)
    assert module.name == "dcc_mcp_maya"
    assert module.version == "0.9.43"
    assert module.directory == Path("C:/My Modules/dcc-mcp")
    assert module.paths == (Path("C:/My Modules/dcc-mcp/python"), Path("C:/My Modules/dcc-mcp/plug-ins"))


def test_read_module_refuses_a_broken_first_line(tmp_path: Path) -> None:
    mod = tmp_path / "x.mod"
    mod.write_text("PYTHONPATH+:=python\n", encoding="utf-8")
    with pytest.raises(ValueError, match="first line"):
        session.read_module(mod)


class FakeMaya:
    """`process.MayaProcess` without a Maya."""

    def __init__(self, exe: Path, env: dict[str, str], visible: bool = False, number: int = 1) -> None:
        self.pid = 4242
        self.created = 7
        self.handle = 0
        self.closed = False
        self.released = False

    def running(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self.released = True

    def window_titles(self) -> list[str]:
        return ["Autodesk Maya 2025"]


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A `maya.exe` and a dcc-mcp `.mod` in `tmp_path`, a fake MayaProcess; returns the temp to start in."""
    (tmp_path / "maya.exe").touch()
    (tmp_path / "mayapy.exe").touch()
    (tmp_path / "dcc_mcp_maya.mod").write_text("+ MAYAVERSION:2025 PLATFORM:win64 dcc_mcp_maya 1 C:/m\n", "utf-8")
    monkeypatch.setenv("MAYAPY_EXE", str(tmp_path / "mayapy.exe"))
    monkeypatch.setenv(session.MODULE_VARIABLE, str(tmp_path / "dcc_mcp_maya.mod"))
    monkeypatch.setattr(session.process, "MayaProcess", FakeMaya)
    monkeypatch.setattr(session.process, "give", lambda *_: None)  # a fake Maya has no process to hold a slot
    monkeypatch.setattr(session.transport, "server_logs", lambda pid: [])
    temp = tmp_path / "temp"
    temp.mkdir()
    return temp


STARTED = SessionParams(PROJECT, Path("C:/project"))


def session_dirs(temp: Path) -> list[Path]:
    return list(temp.glob(f"{session.PREFIX}*"))


@pytest.mark.parametrize("missing", ["maya.exe", "dcc_mcp_maya.mod"])
def test_start_without_maya_or_module_refuses(installed: Path, missing: str) -> None:
    (installed.parent / missing).unlink()
    with pytest.raises(session.StartError, match="no Maya at" if missing == "maya.exe" else "no dcc-mcp-maya"):
        session.start(STARTED, temp=installed)
    assert session_dirs(installed) == []


def test_start_interrupted_in_maya_start_leaves_no_directory(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(session.process, "MayaProcess", interrupted)
    with pytest.raises(KeyboardInterrupt):
        session.start(STARTED, temp=installed)
    assert session_dirs(installed) == []


@pytest.mark.parametrize("raised", [RuntimeError, KeyboardInterrupt])
def test_with_closes_the_maya_and_removes_the_directory(
    installed: Path, monkeypatch: pytest.MonkeyPatch, raised: type[BaseException]
) -> None:
    def wait_for_server(*args: object) -> None:
        if raised is KeyboardInterrupt:
            raise KeyboardInterrupt

    monkeypatch.setattr(session.transport, "wait_for_server", wait_for_server)
    monkeypatch.setattr(session.transport, "connect", lambda url, timeout, work: work)
    with pytest.raises(raised), session.start(STARTED, temp=installed) as started:
        started.wait(1)
        raise raised
    assert isinstance(started.maya, FakeMaya) and started.maya.closed
    assert session_dirs(installed) == []


def test_wait_turns_a_timeout_into_start_error(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def wait_for_server(*args: object) -> None:
        raise TimeoutError("no server")

    monkeypatch.setattr(session.transport, "wait_for_server", wait_for_server)
    with session.start(STARTED, temp=installed) as started, pytest.raises(session.StartError, match="Maya 4242"):
        started.wait(1)


def test_release_keeps_the_maya_and_the_directory(installed: Path) -> None:
    with session.start(STARTED, temp=installed) as started:
        started.release()
    assert isinstance(started.maya, FakeMaya) and started.maya.released and not started.maya.closed
    assert session_dirs(installed) == [started.root]


@pytest.mark.parametrize("record", [True, False])
def test_record_writes_state(installed: Path, record: bool) -> None:
    with session.start(STARTED, temp=installed, ttl=5, record=record) as started:
        state = session.read_state(started.root)
        if record:
            assert state is not None
            expected = State(PROJECT, str(STARTED.project_root), os.getpid(), 4242, started.url, state.started, 5, 7)
            assert state == expected
        else:
            assert not (started.root / session.STATE_FILE).exists()


def test_execute_touches_last_call(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.transport, "execute", lambda url, code, timeout: session.transport.Result(True))
    with session.start(STARTED, temp=installed) as started:
        last_call = started.work / "last_call"
        last_call.touch()
        os.utime(last_call, (NOW, NOW))
        assert started.execute("1", 1).success
        assert last_call.stat().st_mtime > NOW


def slot_free() -> bool:
    slot = slots.try_take()
    if slot is None:
        return False
    slot.release()
    return True


def test_a_start_that_fails_before_maya_holds_the_slot_frees_it(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")

    def broken(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session, "prepare", broken)
    with pytest.raises(OSError, match="disk full"):
        session.start(STARTED, temp=installed)
    assert slot_free()
    assert session_dirs(installed) == []


def test_a_maya_that_cannot_take_the_slot_is_closed_and_the_slot_freed(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    closed: list[FakeMaya] = []

    class Maya(FakeMaya):
        def close(self) -> None:
            closed.append(self)

    monkeypatch.setattr(session.process, "MayaProcess", Maya)
    monkeypatch.setattr(session.process, "give", GIVE)  # the fake Maya has no process: it fails
    with pytest.raises(OSError):
        session.start(STARTED, temp=installed)
    assert len(closed) == 1
    assert slot_free()
    assert session_dirs(installed) == []


def test_start_gives_up_when_no_slot_comes_free(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    held = slots.take(0)
    with pytest.raises(session.StartError, match="Maya slots stayed taken"):
        session.start(STARTED, temp=installed, slot_wait=0.1)
    assert session_dirs(installed) == []
    held.release()


def test_start_names_the_holder_of_its_slot(installed: Path) -> None:
    with session.start(STARTED, temp=installed, record=True) as started:
        holder = json.loads((slots.DIR / "slot-1.holder").read_text(encoding="utf-8"))
    assert holder["pid"] == 4242 and holder["created"] == 7 and holder["command"] == "start"
    assert (holder["project"], holder["url"], holder["work"]) == (PROJECT, started.url, str(started.work))
