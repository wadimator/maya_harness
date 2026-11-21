from pathlib import Path

import pytest

from maya_harness import cli, session, transport
from maya_harness.session import Session, State


def make(root: str, started: float, project: str = "ue_exporter") -> Session:
    state = State(project, str(Path(root)), 1, 2, f"http://127.0.0.1:{int(started)}/mcp", started)
    return Session(Path("C:/temp") / f"maya_harness_{started}", state)


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

    def execute(url: str, code: str, timeout: float) -> transport.Result:
        last_call.touch()
        os.utime(last_call, (0, 0))  # the call took longer than any TTL
        raise ConnectionResetError("the call was cut")

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None: work)
    monkeypatch.setattr(transport, "execute", execute)
    assert cli.main(["exec", "-c", "1"]) == cli.FAILED  # the code reached Maya
    assert last_call.stat().st_mtime > 1_000_000_000


def test_exec_whose_maya_refuses_the_connection_runs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Maya died after connect: the request never reached it, so exit 2 is safe to retry."""
    import urllib.error
    import urllib.request

    from maya_harness import transport

    def refused(request: urllib.request.Request, timeout: float) -> str:
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None: tmp_path)
    monkeypatch.setattr(transport, "read_in_background", refused)
    assert cli.main(["exec", "-c", "1"]) == cli.NOT_RUN


def test_exec_with_an_unreadable_reply_exits_1_and_at_connect_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The code went out with the call: 1; only the handshake went out with connect: 2."""
    from maya_harness import transport

    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(transport, "read_in_background", lambda request, timeout: "<html></html>")
    assert cli.main(["exec", "-c", "1"]) == cli.NOT_RUN
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None: tmp_path)
    assert cli.main(["exec", "-c", "1"]) == cli.FAILED


def test_stop_goes_on_past_a_maya_that_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    from maya_harness import process, transport

    hung, other = make("C:/p", 1), make("C:/p", 2)
    quit_sent: list[str] = []

    def connect(url: str, timeout: float, work: Path | None = None) -> Path:
        if url == hung.state.url:
            raise TimeoutError("timed out")
        return Path(".")

    def execute(url: str, code: str, timeout: float) -> transport.Result:
        quit_sent.append(url)
        return transport.Result(True)

    monkeypatch.setattr(session, "sessions", lambda temp: [hung, other])
    monkeypatch.setattr(transport, "connect", connect)
    monkeypatch.setattr(transport, "execute", execute)
    monkeypatch.setattr(process, "process_alive", lambda pid: False)
    assert cli.main(["stop", "--all-mine"]) == cli.FAILED
    assert quit_sent == [other.state.url]


def test_environment_passes_the_ttl() -> None:
    env = session.environment({}, Path("C:/s"), session.SessionParams("p", Path("C:/p")), 1, ttl=5)
    assert env["MAYA_HARNESS_TTL"] == "5"


def test_exec_reload_is_a_call_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The code goes to Maya as it is: `from __future__` must stay its first statement."""
    from maya_harness import config, transport

    sent: list[str] = []

    def execute(url: str, code: str, timeout: float) -> transport.Result:
        sent.append(code)
        return transport.Result(True)

    code = "from __future__ import annotations\n1"
    monkeypatch.setattr(cli, "select", lambda url: ("http://127.0.0.1:1/mcp", None))
    monkeypatch.setattr(config, "load", lambda start: config.Config("p", tmp_path, reload=("pkg",)))
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None: tmp_path)
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
    monkeypatch.setattr(transport, "connect", lambda url, timeout, expected=None: tmp_path)
    monkeypatch.setattr(transport, "execute", lambda url, code, timeout: result)
    assert cli.main(["exec", "-c", "x"]) == cli.FAILED
    assert capsys.readouterr().err == printed
