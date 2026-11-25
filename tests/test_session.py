import dataclasses
import json
import os
from pathlib import Path

import pytest

from maya_harness import inmaya, registry, session, slots, win32
from maya_harness.registry import State
from maya_harness.session import SessionParams

PROJECT = "Retarget_Master"  # `_` in the name: the directory name cannot be split into a pid
NOW = 1_000_000_000.0
GIVE = win32.give  # the real one: `installed` replaces it


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
    assert sorted(path.name for path in copy.iterdir()) == ["__init__.py", "inmaya.py"]
    assert (copy / "inmaya.py").read_bytes() == (Path(session.__file__).parent / "inmaya.py").read_bytes()
    user_setup = root / "app" / "2025" / "scripts" / "userSetup.py"
    assert user_setup.read_text(encoding="utf-8") == session.USER_SETUP


def test_user_setup_is_inmaya_startup() -> None:
    compile(session.USER_SETUP, "userSetup.py", "exec")
    assert "inmaya.startup()" in session.USER_SETUP


def test_user_setup_writes_the_traceback_where_the_session_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> None:
        raise RuntimeError("no plugin")

    monkeypatch.setattr(inmaya, "startup", broken)
    monkeypatch.setenv(inmaya.MARKER, str(tmp_path))
    with pytest.raises(RuntimeError):
        exec(session.USER_SETUP, {})
    status = (tmp_path / inmaya.STARTUP).read_text(encoding="utf-8")
    assert status.startswith("Traceback") and "no plugin" in status


def test_environment_passes_the_plugins_and_the_ttl_to_startup() -> None:
    env = session.environment(
        BASE, Path("C:/s"), dataclasses.replace(PARAMS, plugins=("fbxmaya", "gameFbxExporter")), 1, 5
    )
    assert env[inmaya.PLUGINS] == os.pathsep.join(["fbxmaya", "gameFbxExporter"])
    assert env[inmaya.TTL] == "5"
    assert session.environment(BASE, Path("C:/s"), SessionParams(PROJECT, Path("C:/p")), 1)[inmaya.PLUGINS] == ""


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
        self.desktop = None
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
    monkeypatch.setattr(win32, "give", lambda *_: None)  # a fake Maya has no process to hold a slot
    monkeypatch.setattr(session.transport, "server_logs", lambda pid: [])
    temp = tmp_path / "temp"
    temp.mkdir()
    return temp


STARTED = SessionParams(PROJECT, Path("C:/project"))


def session_dirs(temp: Path) -> list[Path]:
    return list((temp / registry.SESSIONS).glob(f"{registry.PREFIX}*"))


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

    monkeypatch.setattr(session.Session, "_wait_for_server", wait_for_server)
    monkeypatch.setattr(session.call, "connect", lambda *_, **__: None)
    with pytest.raises(raised), session.start(STARTED, temp=installed) as started:
        started.wait(1)
        raise raised
    assert isinstance(started.maya, FakeMaya) and started.maya.closed
    assert session_dirs(installed) == []


def test_wait_turns_a_timeout_into_start_error(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def wait_for_server(*args: object) -> None:
        raise TimeoutError("no server")

    monkeypatch.setattr(session.Session, "_wait_for_server", wait_for_server)
    with session.start(STARTED, temp=installed) as started, pytest.raises(session.StartError, match="Maya 4242"):
        started.wait(1)


def test_wait_shows_the_traceback_of_startup(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin that does not load: `start` fails at once, with the traceback `userSetup.py` wrote."""
    monkeypatch.setattr(session.transport, "healthy", lambda url: False)
    with session.start(STARTED, temp=installed) as started:
        (started.work / inmaya.STARTUP).write_text(
            "Traceback (most recent call last):\n  no_such_plugin", encoding="utf-8"
        )
        with pytest.raises(session.StartError, match=r"(?s)dcc-mcp failed to load in Maya:\nTraceback.*no_such_plugin"):
            started.wait(60)


def test_wait_ends_when_the_server_answers_or_maya_exits(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([False, True])
    monkeypatch.setattr(session.transport, "healthy", lambda url: next(answers))
    monkeypatch.setattr(session, "STARTUP_POLL", 0)
    connected: list[object] = []
    monkeypatch.setattr(session.call, "connect", lambda url, timeout, **kwargs: connected.append(kwargs["work"]))
    with session.start(STARTED, temp=installed) as started:
        started.wait(60)
        assert connected == [started.work]
        monkeypatch.setattr(session.transport, "healthy", lambda url: False)
        started.maya.closed = True  # type: ignore[attr-defined]
        with pytest.raises(session.StartError, match="Maya exited before its dcc-mcp server started"):
            started.wait(60)


def test_wait_out_of_time_shows_how_far_maya_got(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.transport, "healthy", lambda url: False)
    with session.start(STARTED, temp=installed) as started:
        (started.work / inmaya.STARTUP).write_text("userSetup.py loaded", encoding="utf-8")
        (started.work / session.BOOTSTRAP_LOG).write_text('{"step": "import"}', encoding="utf-8")
        with pytest.raises(session.StartError, match=r'after 0 s; userSetup.py loaded; dcc-mcp bootstrap:\n\{"step"'):
            started.wait(0)


def test_execute_is_a_marked_call(installed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`last_call` for the idle timer, `in_call` for `status` while the call runs, a watch for dialogs."""
    seen: list[object] = []

    def execute(url: str, code: str, timeout: float, watch: object = None) -> session.transport.Result:
        seen.extend([session.call.activity(started.work, 0)[0], watch])
        return session.transport.Result(True)

    monkeypatch.setattr(session.transport, "execute", execute)
    with session.start(STARTED, temp=installed) as started:
        last_call = started.work / inmaya.LAST_CALL
        last_call.unlink(missing_ok=True)
        assert started.execute("1", 1).success
        assert last_call.is_file()
        assert session.call.activity(started.work, 0)[0] is None
    busy, watch = seen
    assert busy is not None and callable(watch)


def test_release_keeps_the_maya_and_the_directory(installed: Path) -> None:
    with session.start(STARTED, temp=installed) as started:
        started.release()
    assert isinstance(started.maya, FakeMaya) and started.maya.released and not started.maya.closed
    assert session_dirs(installed) == [started.root]


def test_release_writes_state(installed: Path) -> None:
    """Only a released Maya outlives its harness: only it has a `state.json` to be found by."""
    with session.start(STARTED, temp=installed, ttl=5, command="start") as started:
        assert not (started.root / registry.STATE_FILE).exists()
        started.release()
        state = registry.read_state(started.root)
        assert state == State(PROJECT, str(STARTED.project_root), 4242, started.url, state.started, 5, 7)


def test_a_session_of_run_writes_no_state(installed: Path) -> None:
    with session.start(STARTED, temp=installed) as started:
        holder = json.loads((slots.DIR / "slot-1.holder").read_text(encoding="utf-8"))
        assert holder["command"] == "run"
    assert not (started.root / registry.STATE_FILE).exists()


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
    monkeypatch.setattr(win32, "give", GIVE)  # the fake Maya has no process: it fails
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
    """The slots are shared by every version: the keys are the ones they all read. A Maya of `start`
    shows `start` while it starts, and `release` does not write the holder again."""
    holder_file = slots.DIR / "slot-1.holder"
    with session.start(STARTED, temp=installed, command="start") as started:
        holder = json.loads(holder_file.read_text(encoding="utf-8"))
        written = holder_file.stat().st_mtime_ns
        started.release()
        assert holder_file.stat().st_mtime_ns == written
    assert sorted(holder) == ["command", "created", "pid", "project", "time", "url", "work"]
    assert holder["pid"] == 4242 and holder["created"] == 7 and holder["command"] == "start"
    assert (holder["project"], holder["url"], holder["work"]) == (PROJECT, started.url, str(started.work))
