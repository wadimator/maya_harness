import json
import os
import time
from pathlib import Path

import pytest

from maya_harness import cli, session, slots, transport
from maya_harness.session import Record, State


def make(root: str, started: float, project: str = "ue_exporter") -> Record:
    state = State(project, str(Path(root)), 1, 2, f"http://127.0.0.1:{int(started)}/mcp", started)
    return Record(Path("C:/temp") / f"maya_harness_{started}", state)


def test_exec_parses_code_url_and_flags() -> None:
    args = cli.parser().parse_args(["exec", "http://127.0.0.1:5/mcp", "-c", "1", "--reload", "--json"])
    assert (args.url, args.code, args.file, args.reload, args.json) == ("http://127.0.0.1:5/mcp", "1", None, True, True)


def test_exec_needs_exactly_one_of_code_and_file() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["exec"])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["exec", "-c", "1", "-f", "x.py"])


def test_stop_takes_one_target() -> None:
    assert cli.parser().parse_args(["stop", "--all-mine"]).all_mine
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["stop", "--project", "--all-mine"])


def test_start_defaults() -> None:
    args = cli.parser().parse_args(["start"])
    assert (args.visible, args.ttl) == (False, cli.DEFAULT_TTL)


def test_exec_picks_the_latest_session_of_its_own_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two live sessions with one project name: the clone and its worktree."""
    clone, worktree = tmp_path / "UE_exporter", tmp_path / "wt" / "UE_exporter"
    for root in (clone, worktree):
        root.mkdir(parents=True)
        (root / "pyproject.toml").write_text('[project]\nname = "ue_exporter"\n', encoding="utf-8")
    older, own, newer_other = make(str(clone), 1), make(str(clone), 2), make(str(worktree), 3)
    monkeypatch.setattr(session, "sessions", lambda temp: [older, own, newer_other])
    monkeypatch.chdir(clone)
    assert cli.select(None) == (own.state.url, own)
    monkeypatch.chdir(worktree)
    assert cli.select(None) == (newer_other.state.url, newer_other)


def test_exec_without_a_session_of_its_root_runs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session, "sessions", lambda temp: [make(str(tmp_path / "other"), 1)])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(cli.NotRun, match="no running Maya"):
        cli.select(None)


def test_exec_at_a_url_takes_its_session_when_there_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    known = make("C:/p", 7)
    monkeypatch.setattr(session, "sessions", lambda temp: [known])
    assert cli.select(known.state.url) == (known.state.url, known)
    assert cli.select("http://127.0.0.1:9/mcp") == ("http://127.0.0.1:9/mcp", None)


def test_reload_code_drops_the_packages_and_their_submodules() -> None:
    import sys

    sys.modules["fake_pkg_for_reload"] = sys
    sys.modules["fake_pkg_for_reload.sub"] = sys
    sys.modules["fake_pkg_for_reload_other"] = sys
    code = cli.reload_code(["fake_pkg_for_reload"])
    exec(code, {})
    assert "fake_pkg_for_reload" not in sys.modules
    assert "fake_pkg_for_reload.sub" not in sys.modules
    assert sys.modules.pop("fake_pkg_for_reload_other") is sys


def test_missing_maya_exits_with_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAYAPY_EXE", str(tmp_path / "nowhere" / "mayapy.exe"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["start"]) == cli.NOT_RUN
    assert "no Maya at" in capsys.readouterr().err


def test_exec_touches_last_call_after_a_failed_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The idle timer is silent during a call: a call longer than the TTL must not count as idle."""
    import os

    from maya_harness import transport

    work = tmp_path / "work"
    work.mkdir()
    last_call = work / "last_call"

    def execute(url: str, code: str, timeout: float, watch: object = None) -> transport.Result:
        last_call.touch()
        os.utime(last_call, (0, 0))  # the call took longer than any TTL
        raise ConnectionResetError("the call was cut")

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: work)
    monkeypatch.setattr(transport, "execute", execute)
    assert cli.main(["exec", "-c", "1"]) == cli.FAILED  # the code reached Maya
    assert last_call.stat().st_mtime > 1_000_000_000


def test_exec_whose_maya_refuses_the_connection_runs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Maya died after connect: the request never reached it, so exit 2 is safe to retry."""
    import urllib.error
    import urllib.request

    from maya_harness import transport

    def refused(request: urllib.request.Request, timeout: float, watch: object = None) -> str:
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: tmp_path)
    monkeypatch.setattr(transport, "read_in_background", refused)
    assert cli.main(["exec", "-c", "1"]) == cli.NOT_RUN


def test_exec_with_an_unreadable_reply_exits_1_and_at_connect_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The code went out with the call: 1; only the handshake went out with connect: 2."""
    from maya_harness import transport

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "read_in_background", lambda request, timeout, watch=None: "<html></html>")
    assert cli.main(["exec", "-c", "1"]) == cli.NOT_RUN
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: tmp_path)
    assert cli.main(["exec", "-c", "1"]) == cli.FAILED


def test_stop_kills_at_once_and_keeps_the_directory_of_a_maya_still_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No call goes to Maya: a busy one would take it only after its current call."""
    from maya_harness import process

    def target(pid: int) -> Record:
        root = tmp_path / f"maya_harness_{pid}"
        root.mkdir()
        return Record(root, State("p", "C:/p", 1, pid, f"http://127.0.0.1:{pid}/mcp", time.time(), maya_created=pid))

    def no_call(*args: object) -> None:
        raise AssertionError("stop called Maya")

    stuck, killed = target(10), target(20)
    monkeypatch.setattr(session, "sessions", lambda temp: [stuck, killed])
    monkeypatch.setattr(transport, "connect", no_call)
    monkeypatch.setattr(transport, "execute", no_call)
    monkeypatch.setattr(process, "window_titles", lambda pid: ["Maya-2025"])
    monkeypatch.setattr(process, "kill_maya", lambda pid, created: pid == killed.state.maya_pid)
    assert cli.main(["stop", "--all-mine"]) == cli.FAILED
    assert stuck.root.is_dir()
    assert not killed.root.exists()
    out = capsys.readouterr().out
    assert "Maya 10 http://127.0.0.1:10/mcp p: idle 0 min, windows [Maya-2025]" in out
    assert "Maya 10: still running" in out
    assert "Maya 20: killed" in out


def test_environment_passes_the_ttl() -> None:
    env = session.environment({}, Path("C:/s"), session.SessionParams("p", Path("C:/p")), 1, ttl=5)
    assert env["MAYA_HARNESS_TTL"] == "5"


def test_exec_reload_is_a_call_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The code goes to Maya as it is: `from __future__` must stay its first statement."""
    from maya_harness import config, transport

    sent: list[str] = []

    def execute(url: str, code: str, timeout: float, watch: object = None) -> transport.Result:
        sent.append(code)
        return transport.Result(True)

    code = "from __future__ import annotations\n1"
    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(config, "load", lambda start: config.Config("p", tmp_path, reload=("pkg",)))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: tmp_path)
    monkeypatch.setattr(transport, "execute", execute)
    assert cli.main(["exec", "--reload", "-c", code]) == cli.OK
    assert sent == [cli.reload_code(("pkg",)), code]


@pytest.mark.parametrize(
    ("result", "printed"),
    [
        (
            transport.Result(False, "RuntimeError", stderr="NameError: name 'x' is not defined\n"),
            "NameError: name 'x' is not defined\n",
        ),
        (
            transport.Result(False, "Blocked cmds.file(new=True)", stderr="mine\n"),
            "mine\nBlocked cmds.file(new=True)\n",
        ),
        (transport.Result(False, "no tool"), "no tool\n"),
    ],
)
def test_exec_prints_the_error_only_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: transport.Result,
    printed: str,
) -> None:
    """dcc-mcp names every failure of code `RuntimeError`; its real type ends the traceback."""
    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: tmp_path)
    monkeypatch.setattr(transport, "execute", lambda url, code, timeout, watch=None: result)
    assert cli.main(["exec", "-c", "x"]) == cli.FAILED
    assert capsys.readouterr().err == printed


def test_status_shows_the_slots_in_text_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from maya_harness import slots

    monkeypatch.setattr(session, "sessions", lambda temp: [])
    held = slots.take(0)
    try:
        assert cli.main(["status"]) == cli.OK
        out = capsys.readouterr().out
        assert "Maya slots: 1 of 4 taken\n  slot 1: a Maya starting\n" in out
        assert cli.main(["status", "--json"]) == cli.OK
        assert json.loads(capsys.readouterr().out) == []
    finally:
        held.release()


@pytest.mark.parametrize("value", ["-5", "nan"])
@pytest.mark.parametrize("command", [["start"], ["run", "check_fps"]])
def test_a_slot_wait_below_0_is_refused(capsys: pytest.CaptureFixture[str], command: list[str], value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.parser().parse_args([*command, f"--slot-wait={value}"])
    assert stopped.value.code == cli.NOT_RUN
    assert "takes 0 or more seconds" in capsys.readouterr().err
    assert cli.parser().parse_args([*command, "--slot-wait", "0"]).slot_wait == 0


DIALOG = "Untrusted Plugin Loading - Security Warning"


def on_a_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A session whose Maya shows the dialog; `select` finds it, so exec watches it."""
    from maya_harness import process

    target = Record(tmp_path, State("p", str(tmp_path), 1, 10, "http://127.0.0.1:1/mcp", time.time()))
    (tmp_path / "work").mkdir()
    monkeypatch.setattr(cli, "select", lambda url: (target.state.url, target))
    monkeypatch.setattr(process, "modal_dialog", lambda pid, desktop=None: [DIALOG])


def test_exec_on_a_modal_dialog_exits_1_with_its_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The code went to Maya: it runs on once the dialog is answered."""

    def execute(url: str, code: str, timeout: float, watch: transport.Watch | None = None) -> transport.Result:
        assert watch is not None and slots.in_call(tmp_path / "work") is not None  # status: busy
        watch()
        return transport.Result(True)

    on_a_dialog(monkeypatch, tmp_path)
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None, watch=None: tmp_path / "work")
    monkeypatch.setattr(transport, "execute", execute)
    assert cli.main(["exec", "-c", "1"]) == cli.FAILED
    err = capsys.readouterr().err
    assert DIALOG in err and "maya-harness stop http://127.0.0.1:1/mcp" in err
    assert slots.in_call(tmp_path / "work") is None


def test_exec_into_a_maya_already_on_a_dialog_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The marker of connect waits on Maya's main thread too: only the watch ends that wait."""

    def connect(url: str, timeout: float, expected: Path | None = None, watch: transport.Watch | None = None) -> Path:
        assert watch is not None
        watch()
        return tmp_path / "work"

    on_a_dialog(monkeypatch, tmp_path)
    monkeypatch.setattr(transport, "connect", connect)
    assert cli.main(["exec", "-c", "1"]) == cli.NOT_RUN
    assert DIALOG in capsys.readouterr().err


def test_status_tells_a_running_call_from_idleness_and_shows_the_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A call that hangs for minutes is not idleness: `last_call` is touched only around it."""
    from maya_harness import process

    item = Record(tmp_path, State("p", str(tmp_path), 1, 10, "http://127.0.0.1:1/mcp", time.time() - 600))
    item.work.mkdir()
    monkeypatch.setattr(session, "sessions", lambda temp: [item])
    monkeypatch.setattr(process, "window_titles", lambda pid: [DIALOG, "untitled - Autodesk MAYA"])
    assert cli.main(["status"]) == cli.OK
    assert f"idle 10 min, windows [{DIALOG}, untitled - Autodesk MAYA]" in capsys.readouterr().out
    (item.work / "in_call").write_text(json.dumps({"pid": os.getpid(), "since": time.time() - 540}), encoding="utf-8")
    assert cli.main(["status"]) == cli.OK
    assert "busy 9 min, windows [" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == cli.OK
    row = json.loads(capsys.readouterr().out)[0]
    assert 539 <= row["busy"] <= 541 and row["windows"] == [DIALOG, "untitled - Autodesk MAYA"]
