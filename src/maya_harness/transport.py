"""Calls into a session's Maya through dcc-mcp-maya: the only module that knows dcc-mcp.

* The server is in-process, on the port the session reserved (`DCC_MCP_MAYA_PORT`); it is up
  when its `/health` endpoint answers.
* The handshake is `initialize`, then `notifications/initialized`. Code runs as a `tools/call`
  of `execute_python` on Maya's main thread; the reply's `structuredContent` holds `success`,
  `error`, and `context` with `stdout`, `stderr` and the `output` of the last expression as JSON.
  The value is asked for only when the code ends in an expression: dcc-mcp-maya 0.9.43 runs code
  without one and then fails the call.
* A call that comes while Maya opens, saves or clears a scene is refused with the error code
  `maya-busy` (the DCC_MCP patch of the module: run nested in `file -open`, it would see a half
  loaded scene). `execute` repeats such a call every `BUSY_RETRY` seconds until its timeout.
  The code is read from the `error` field of a failed reply, never from its text: the stdout of
  code that ran may say `maya-busy` too, and repeating it would run it again. Checked in Maya
  with dcc-mcp-maya 0.9.43: the refusal is `structuredContent` `{"success": false, "error":
  "maya-busy", "message": ..., "context": {}}`, with `isError: true` next to it.
* A call has a timeout of its own, so the caller can share one deadline between its calls.
* While a reply is awaited, the caller's `watch` is called every `WATCH_EVERY` seconds: what to
  look at is the caller's business (a modal dialog of its Maya), this module knows only dcc-mcp.
  A `watch` that raises cuts the wait short; the code may run on in Maya.
"""

from __future__ import annotations

import ast
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUSY = "maya-busy"
BUSY_RETRY = 0.5  # seconds between the repeats of a call refused with maya-busy
STARTUP_TIMEOUT = 180  # seconds for a Maya to start and connect; a cold start takes 12-15
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "dcc-mcp" / "log"
WATCH_EVERY = 2.0  # seconds between the calls of `watch` while a reply is awaited

Watch = Callable[[], None]


class TransportError(RuntimeError):
    """The call did not reach Maya, or Maya is not a session of maya_harness."""


class CallAborted(TransportError):
    """The caller's `watch` cut the wait for a reply short: the code may have run, and may run on."""


class ReplyError(OSError):
    """The server got the request, but its reply cannot be read: cut short, or not dcc-mcp's. The
    code may have run."""


@dataclass(frozen=True)
class Result:
    """What a call of `execute` returned. `output` is the last expression's value as text: a
    string as it is, anything else as its `str()` (`{'a': 1}`, not JSON), so code that wants JSON
    ends in `json.dumps(...)`. `error` is the server's error code: for code that raised,
    dcc-mcp-maya 0.9.43 names it `RuntimeError` whatever was raised, and the real type ends the
    traceback in `stderr`. A refusal without a traceback has its reason in `error` (a blocked
    `cmds.file`, no code) or, when the server gives it only in `_meta`, in `stderr`."""

    success: bool
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    output: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def value(self) -> Any:
        """`output` decoded; None when there is none, the text itself when it is not JSON."""
        if not self.output:
            return None
        try:
            return json.loads(self.output)
        except ValueError:
            return self.output


def server_logs(pid: int) -> list[Path]:
    """The dcc-mcp logs of the Maya with `pid`. Windows reuses pids: a log that a closed Maya left
    with this pid holds a stale address."""
    return list(LOG_DIR.glob(f"dcc-mcp-maya.{pid}.*.log"))


def wait_for_server(url: str, work: Path, running: Callable[[], bool], timeout: float) -> None:
    """Waits until the server at `url` answers; `work/startup.txt` tells how far `userSetup.py` got."""
    deadline = time.monotonic() + timeout
    status = work / "startup.txt"
    while time.monotonic() < deadline:
        if status.is_file():
            startup = status.read_text(encoding="utf-8")
            if startup.startswith("Traceback"):
                raise TransportError(f"dcc-mcp failed to load in Maya:\n{startup}")
        try:
            with urllib.request.urlopen(url.removesuffix("/mcp") + "/health", timeout=0.5) as response:
                if json.loads(response.read().decode("utf-8")).get("ok"):
                    return
        except OSError:
            pass
        if not running():
            raise TransportError("Maya exited before its dcc-mcp server started.")
        time.sleep(0.25)
    startup = status.read_text(encoding="utf-8") if status.is_file() else "userSetup.py did not run"
    bootstrap = work / "dcc_mcp_bootstrap.jsonl"
    if bootstrap.is_file():
        startup += f"; dcc-mcp bootstrap:\n{bootstrap.read_text(encoding='utf-8')}"
    raise TimeoutError(f"No dcc-mcp server at {url} after {timeout:.0f} s; {startup}.")


def read_in_background(request: urllib.request.Request, timeout: float, watch: Watch | None = None) -> str:
    """The reply read in a thread: on Windows a blocking socket read holds Ctrl+C until the reply
    comes, and waiting on the thread lets Ctrl+C through, and `watch` too.

    Check Ctrl+C from PowerShell: processes started from Git Bash ignore it, and the check passes
    falsely."""
    outcome: list[str | BaseException] = []

    def read() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                outcome.append(response.read().decode("utf-8"))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    watched = time.monotonic()
    while thread.is_alive():
        thread.join(0.2)
        if watch is not None and thread.is_alive() and time.monotonic() - watched >= WATCH_EVERY:
            watch()
            watched = time.monotonic()
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def call(
    url: str,
    method: str,
    params: dict[str, object],
    request_id: int | None,
    timeout: float,
    watch: Watch | None = None,
) -> dict[str, Any]:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    request = urllib.request.Request(
        url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    try:
        body = read_in_background(request, timeout, watch)
        reply = json.loads(body) if request_id is not None and body else {}
    except (urllib.error.HTTPError, ConnectionError):  # Maya answered or hung up: the code may have run
        raise
    except urllib.error.URLError as error:  # urllib raises it only while connecting or sending
        raise TransportError(f"no connection to {url}: {error.reason}") from error
    except (http.client.HTTPException, ValueError) as error:  # a cut reply, not UTF-8, not JSON
        raise ReplyError(f"unreadable reply from {url}: {error}") from error
    if not isinstance(reply, dict):
        raise ReplyError(f"unreadable reply from {url}: not a JSON object")
    return reply


def result_type(code: str) -> str:
    """`JSON` when the code ends in an expression, else `NONE`: dcc-mcp fails a value request for
    code without one after running it. Code that does not parse goes as `JSON`, and Maya reports
    its error."""
    try:
        body = ast.parse(code).body
    except (SyntaxError, ValueError):
        return "JSON"
    return "JSON" if body and isinstance(body[-1], ast.Expr) else "NONE"


def _execute_once(url: str, code: str, timeout: float, watch: Watch | None = None) -> dict[str, Any]:
    arguments = {"code": code, "result_type": result_type(code), "capture_output": True}
    reply = call(url, "tools/call", {"name": "execute_python", "arguments": arguments}, 2, timeout, watch)
    if "error" in reply:  # the server refused the call itself, e.g. an unknown tool
        return {"success": False, "error": json.dumps(reply["error"], ensure_ascii=False)}
    content: dict[str, Any] = reply.get("result", {}).get("structuredContent", {})
    return content


def is_busy(content: dict[str, Any]) -> bool:
    return content.get("success") is False and content.get("error") == BUSY


def to_result(content: dict[str, Any]) -> Result:
    context = content.get("context") or {}
    output = context.get("output")
    error = content.get("error") or ""
    return Result(
        success=bool(content.get("success")),
        error=error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
        stdout=str(context.get("stdout") or ""),
        stderr=str(context.get("stderr") or ((content.get("_meta") or {}).get("dcc.error") or {}).get("message") or ""),
        output="" if output is None else output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
        raw=content,
    )


def execute(url: str, code: str, timeout: float, watch: Watch | None = None) -> Result:
    """Runs `code` in the Maya at `url`; a maya-busy refusal is repeated until `timeout` runs out.
    `watch` that raises cuts the wait for a reply short."""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TransportError(f"no time left for a call to {url}")
        content = _execute_once(url, code, left, watch)
        if not is_busy(content):
            return to_result(content)
        if deadline - time.monotonic() <= BUSY_RETRY:
            raise TransportError(f"Maya at {url} stayed {BUSY} (opening or saving a scene) for {timeout:.0f} s")
        time.sleep(BUSY_RETRY)


def handshake(url: str, timeout: float, watch: Watch | None = None) -> None:
    """`initialize`, then `notifications/initialized`: the server takes calls after it."""
    call(
        url,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "maya_harness"}},
        1,
        timeout,
        watch,
    )
    call(url, "notifications/initialized", {}, None, timeout, watch)
