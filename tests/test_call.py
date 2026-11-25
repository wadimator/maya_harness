import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from maya_harness import call, inmaya, process, transport

NOW = 1_700_000_000.0
URL = "http://127.0.0.1:1/mcp"


def marker(monkeypatch: pytest.MonkeyPatch, value: str) -> list[str]:
    """A Maya whose marker is `value`; the code it was sent."""
    sent: list[str] = []
    monkeypatch.setattr(transport, "handshake", lambda url, timeout, watch=None: None)

    def execute(url: str, code: str, timeout: float, watch: object = None) -> transport.Result:
        sent.append(code)
        return transport.Result(True, output=json.dumps(value))

    monkeypatch.setattr(transport, "execute", execute)
    return sent


def test_connect_refuses_a_maya_without_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    marker(monkeypatch, "")
    with pytest.raises(transport.TransportError, match="not a Maya started by maya_harness"):
        call.connect(URL, timeout=10)


def test_connect_refuses_another_session(monkeypatch: pytest.MonkeyPatch) -> None:
    marker(monkeypatch, "C:/other/work")
    with pytest.raises(transport.TransportError, match="another Maya"):
        call.connect(URL, timeout=10, work=Path("C:/mine/work"))


def test_connect_finds_the_work_directory_by_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = marker(monkeypatch, "C:/mine/work")
    assert call.connect(URL, timeout=10, pid=10) == call.Target(URL, Path("C:/mine/work"), 10)
    assert sent == [call.MARKER_CODE]


def test_the_marker_code_reads_the_marker_of_inmaya(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(inmaya.MARKER, "C:/mine/work")
    *statements, last = call.MARKER_CODE.splitlines()
    scope: dict[str, object] = {}
    exec("\n".join(statements), scope)
    assert json.loads(eval(last, scope)) == "C:/mine/work"


def test_execute_marks_the_call_for_the_idle_timer_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The idle timer is silent during a call: a call longer than the TTL must not count as idle."""
    last_call = tmp_path / inmaya.LAST_CALL
    seen: list[object] = []

    def execute(url: str, code: str, timeout: float, watch: object = None) -> transport.Result:
        seen.append(call.activity(tmp_path, NOW)[0])
        os.utime(last_call, (0, 0))  # the call took longer than any TTL
        raise ConnectionResetError("the call was cut")

    monkeypatch.setattr(transport, "execute", execute)
    with pytest.raises(ConnectionResetError):
        call.Target(URL, tmp_path).execute("1", 1)
    assert seen != [None]  # busy while it ran
    assert call.activity(tmp_path, NOW)[0] is None
    assert last_call.stat().st_mtime > NOW - 60 * 60 * 24 * 365


def test_execute_watches_a_maya_with_a_pid_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    watches: list[object] = []
    monkeypatch.setattr(
        transport, "execute", lambda url, code, timeout, watch=None: watches.append(watch) or transport.Result(True)
    )
    call.Target(URL, tmp_path).execute("1", 1)
    call.Target(URL, tmp_path, 10).execute("1", 1)
    given = lambda: None  # noqa: E731
    call.Target(URL, tmp_path, 10).execute("1", 1, given)
    assert watches[0] is None and callable(watches[1]) and watches[2] is given


def test_watcher_cuts_a_call_short_only_on_a_modal_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    dialog: list[str] = []
    monkeypatch.setattr(process, "modal_dialog", lambda pid, desktop=None: dialog)
    watch = call.Target(URL, Path("C:/w"), 10).watch()
    assert watch is not None
    watch()
    dialog.append("Untrusted Plugin Loading - Security Warning")
    with pytest.raises(transport.CallAborted) as raised:
        watch()
    assert "Maya 10 shows a modal dialog: ['Untrusted Plugin Loading - Security Warning']" in str(raised.value)
    assert f"maya-harness stop {URL}" in str(raised.value)


def test_activity_of_an_idle_maya_is_the_end_of_its_last_call(tmp_path: Path) -> None:
    assert call.activity(tmp_path, NOW) == (None, NOW)
    (tmp_path / inmaya.LAST_CALL).touch()
    os.utime(tmp_path / inmaya.LAST_CALL, (NOW + 5, NOW + 5))
    assert call.activity(tmp_path, NOW) == (None, NOW + 5)


def test_activity_of_a_running_call_is_its_start(tmp_path: Path) -> None:
    mark = {"pid": os.getpid(), "since": NOW}
    (tmp_path / call.IN_CALL).write_text(json.dumps(mark), encoding="utf-8")
    assert call.activity(tmp_path, 0)[0] == NOW


def test_a_call_of_a_dead_process_is_none(tmp_path: Path) -> None:
    """An exec killed in its wait leaves the file behind: Maya is not busy forever."""
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    (tmp_path / call.IN_CALL).write_text(json.dumps({"pid": dead.pid, "since": time.time()}), encoding="utf-8")
    assert call.activity(tmp_path, 0)[0] is None
