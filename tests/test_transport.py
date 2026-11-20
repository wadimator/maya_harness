import json
from typing import Any

import pytest

from maya_harness import transport

URL = "http://127.0.0.1:1/mcp"
BUSY: dict[str, Any] = {"success": False, "message": "Maya is reading a scene file", "error": "maya-busy"}


def ok(stdout: str = "", output: object = "done") -> dict[str, Any]:
    return {"success": True, "error": None, "context": {"stdout": stdout, "output": json.dumps(output)}}


class FakeServer:
    """Replies to `execute_python` calls in turn, on a clock that only `sleep` moves."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.now = 0.0
        monkeypatch.setattr(transport, "call", self.call)
        monkeypatch.setattr(transport.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(transport.time, "sleep", self.sleep)

    def call(self, url: str, method: str, params: dict[str, object], request_id: int | None, timeout: float) -> Any:
        assert timeout > 0
        self.calls += 1
        content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"result": {"structuredContent": content}}

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_busy_is_repeated_until_maya_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(monkeypatch, BUSY, BUSY, ok())
    result = transport.execute(URL, "1", timeout=10)
    assert result.success
    assert result.output == "done"
    assert server.calls == 3


def test_busy_until_timeout_is_an_error_about_maya_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(monkeypatch, BUSY)
    with pytest.raises(TimeoutError, match="maya-busy"):
        transport.execute(URL, "1", timeout=3)
    assert server.calls > 1


def test_maya_busy_in_stdout_of_code_that_ran_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(monkeypatch, ok(stdout="maya-busy\n", output="maya-busy"))
    result = transport.execute(URL, 'print("maya-busy")', timeout=10)
    assert result.success
    assert result.stdout == "maya-busy\n"
    assert server.calls == 1


def test_failed_code_mentioning_maya_busy_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = {"success": False, "error": "RuntimeError: maya-busy", "context": {"stderr": "maya-busy"}}
    server = FakeServer(monkeypatch, failed)
    result = transport.execute(URL, "raise RuntimeError('maya-busy')", timeout=10)
    assert not result.success
    assert server.calls == 1


def test_no_time_left_is_an_error_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(monkeypatch, ok())
    with pytest.raises(TimeoutError):
        transport.execute(URL, "1", timeout=0)
    assert server.calls == 0


def test_server_refusal_is_a_failed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "call", lambda *args: {"error": {"code": -32601, "message": "no tool"}})
    result = transport.execute(URL, "1", timeout=10)
    assert not result.success
    assert "no tool" in result.error


def test_connect_refuses_a_maya_without_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer(monkeypatch, ok(output=""))
    with pytest.raises(transport.TransportError, match="not a Maya started by maya_harness"):
        transport.connect(URL, timeout=10)


def test_connect_refuses_another_session(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer(monkeypatch, ok(output="C:/other/work"))
    with pytest.raises(transport.TransportError, match="another Maya"):
        transport.connect(URL, timeout=10, work=transport.Path("C:/mine/work"))
