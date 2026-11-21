import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import session
from maya_harness.session import SessionParams, State

PROJECT = "Retarget_Master"  # `_` in the name: the directory name cannot be split into a pid
DEAD, ALIVE, OTHER_ALIVE = 101, 202, 303
NOW = 1_000_000_000.0


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
    assert env["PYTHONPATH"] == os.pathsep.join([str(Path("C:/project/src")), str(Path("C:/project/vendor"))])
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


def test_environment_without_paths_sets_none() -> None:
    env = session.environment(BASE, Path("C:/s"), SessionParams(PROJECT, Path("C:/p")), 1)
    assert "PYTHONPATH" not in env
    assert "MAYA_PLUG_IN_PATH" not in env


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
