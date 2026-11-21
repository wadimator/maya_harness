import http.client
import json
import urllib.error
import urllib.request
from email.message import Message
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
    assert result.value() == "done"
    assert server.calls == 3


def test_busy_until_timeout_is_an_error_about_maya_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer(monkeypatch, BUSY)
    with pytest.raises(transport.TransportError, match="maya-busy"):
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
    with pytest.raises(transport.TransportError):
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


def test_a_refused_connection_did_not_reach_maya(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib wraps in URLError only what fails while connecting or sending: Maya got no request."""

    def refused(request: urllib.request.Request, timeout: float) -> str:
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(transport, "read_in_background", refused)
    with pytest.raises(transport.TransportError, match="no connection"):
        transport.execute(URL, "1", timeout=10)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError(10054, "reset"),
        http.client.RemoteDisconnected("closed"),
        urllib.error.HTTPError(URL, 500, "error", Message(), None),
    ],
)
def test_a_reset_or_an_http_error_is_not_a_transport_error(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """Maya got the request: the code may have run, and exec must not say that nothing was run."""

    def fail(request: urllib.request.Request, timeout: float) -> str:
        raise error

    monkeypatch.setattr(transport, "read_in_background", fail)
    with pytest.raises(type(error)):
        transport.execute(URL, "1", timeout=10)


@pytest.mark.parametrize(
    "reply",
    [
        "<html>not dcc-mcp</html>",
        "[1]",
        http.client.IncompleteRead(b"{", 99),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
def test_an_unreadable_reply_is_a_reply_error(monkeypatch: pytest.MonkeyPatch, reply: str | Exception) -> None:
    """Not a ValueError out of `main`: an OSError, so exec says the code may have run."""

    def read(request: urllib.request.Request, timeout: float) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(transport, "read_in_background", read)
    with pytest.raises(transport.ReplyError, match="unreadable reply"):
        transport.execute(URL, "1", timeout=10)


@pytest.mark.parametrize(
    ("code", "result_type"),
    [
        ("x = 1", "NONE"),
        ("for i in range(2):\n    i", "NONE"),
        ("# only a comment", "NONE"),
        ("import os\nos.sep", "JSON"),
        ("1", "JSON"),
        ("def f(:", "JSON"),
    ],
)
def test_a_value_is_asked_only_of_code_ending_in_an_expression(
    monkeypatch: pytest.MonkeyPatch, code: str, result_type: str
) -> None:
    """dcc-mcp fails a value request for code that does not end in an expression."""
    sent: list[object] = []

    def call(url: str, method: str, params: dict[str, Any], request_id: int | None, timeout: float) -> Any:
        sent.append(params["arguments"]["result_type"])
        return {"result": {"structuredContent": ok()}}

    monkeypatch.setattr(transport, "call", call)
    assert transport.execute(URL, code, timeout=10).success
    assert sent == [result_type]


def test_a_failure_without_stderr_keeps_the_explanation_of_the_server() -> None:
    """The reply of dcc-mcp-maya 0.9.43: the explanation is only in `_meta`."""
    explanation = "result_type='JSON' requested but the source does not end with a standalone expression."
    content = {
        "success": False,
        "message": "Python execution failed",
        "error": "RuntimeError",
        "context": {
            "stdout": "",
            "stderr": "",
            "error_type": "RuntimeError",
            "traceback": f"RuntimeError: {explanation}\n",
        },
        "_meta": {"dcc.error": {"type": "RuntimeError", "message": explanation}},
    }
    assert transport.to_result(content).stderr == explanation
