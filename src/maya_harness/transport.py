"""Calls into a session's Maya through dcc-mcp-maya: the only module that knows dcc-mcp.

* The server is in-process, on the port the session reserved (`DCC_MCP_MAYA_PORT`); it is up
  when its `/health` endpoint answers.
* The handshake is `initialize`, then `notifications/initialized`. Code runs as a `tools/call`
  of `execute_python` on Maya's main thread; the reply's `structuredContent` holds `success`,
  `error`, and `context` with `stdout`, `stderr` and the `output` of the last expression as JSON.
* A call that comes while Maya opens, saves or clears a scene is refused with the error code
  `maya-busy` (the DCC_MCP patch of the module: run nested in `file -open`, it would see a half
  loaded scene). `execute` repeats such a call every `BUSY_RETRY` seconds until its timeout.
  The code is read from the `error` field of a failed reply, never from its text: the stdout of
  code that ran may say `maya-busy` too, and repeating it would run it again. Checked in Maya
  with dcc-mcp-maya 0.9.43: the refusal is `structuredContent` `{"success": false, "error":
  "maya-busy", "message": ..., "context": {}}`, with `isError: true` next to it.
* A call has a timeout of its own, so the caller can share one deadline between its calls.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUSY = "maya-busy"
BUSY_RETRY = 0.5  # seconds between the repeats of a call refused with maya-busy
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "dcc-mcp" / "log"
MARKER_CODE = "import json, os\njson.dumps(os.environ.get('MAYA_HARNESS_WORK', ''))"
QUIT_CODE = "import maya.cmds as cmds\ncmds.evalDeferred(lambda: cmds.quit(force=True), lowestPriority=True)"


class TransportError(RuntimeError):
    """The call did not reach Maya, or Maya is not a session of maya_harness."""


@dataclass(frozen=True)
class Result:
    """What a call of `execute` returned. `output` is the last expression's value, decoded from
    JSON; `error` is the server's error, or the traceback of the code."""

    success: bool
    error: str = ""
    stdout: str = ""
    stderr: str = ""
    output: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


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


def read_in_background(request: urllib.request.Request, timeout: float) -> str:
    """The reply read in a thread: on Windows a blocking socket read holds Ctrl+C until the reply
    comes, and waiting on the thread lets Ctrl+C through.

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
    while thread.is_alive():
        thread.join(0.2)
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def call(url: str, method: str, params: dict[str, object], request_id: int | None, timeout: float) -> dict[str, Any]:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    request = urllib.request.Request(
        url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    body = read_in_background(request, timeout)
    if request_id is None or not body:
        return {}
    reply: dict[str, Any] = json.loads(body)
    return reply


def _execute_once(url: str, code: str, timeout: float) -> dict[str, Any]:
    arguments = {"code": code, "result_type": "JSON", "capture_output": True}
    reply = call(url, "tools/call", {"name": "execute_python", "arguments": arguments}, 2, timeout)
    if "error" in reply:  # the server refused the call itself, e.g. an unknown tool
        return {"success": False, "error": json.dumps(reply["error"], ensure_ascii=False)}
    content: dict[str, Any] = reply.get("result", {}).get("structuredContent", {})
    return content


def is_busy(content: dict[str, Any]) -> bool:
    return content.get("success") is False and content.get("error") == BUSY


def to_result(content: dict[str, Any]) -> Result:
    context = content.get("context") or {}
    output = context.get("output")
    try:
        value = json.loads(output) if isinstance(output, str) and output else None
    except ValueError:
        value = output
    error = content.get("error") or ""
    return Result(
        success=bool(content.get("success")),
        error=error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
        stdout=str(context.get("stdout") or ""),
        stderr=str(context.get("stderr") or ""),
        output=value,
        raw=content,
    )


def execute(url: str, code: str, timeout: float) -> Result:
    """Runs `code` in the Maya at `url`; a maya-busy refusal is repeated until `timeout` runs out."""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"no time left for a call to {url}")
        content = _execute_once(url, code, left)
        if not is_busy(content):
            return to_result(content)
        if deadline - time.monotonic() <= BUSY_RETRY:
            raise TimeoutError(f"Maya at {url} stayed {BUSY} (opening or saving a scene) for {timeout:.0f} s")
        time.sleep(BUSY_RETRY)


def connect(url: str, timeout: float, work: Path | None = None) -> None:
    """The handshake, then the marker: a session's Maya holds its work directory there, and the
    Maya a session started must hold its own one — a port another Maya took would run the code
    there."""
    call(
        url,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "maya_harness"}},
        1,
        timeout,
    )
    call(url, "notifications/initialized", {}, None, timeout)
    result = execute(url, MARKER_CODE, timeout)
    if not result.success:
        raise TransportError(f"{url} did not check its marker: {result.error or result.raw}")
    if not result.output:
        raise TransportError(f"{url} is not a Maya started by maya_harness: nothing is run there.")
    if work is not None and result.output != str(work):
        raise TransportError(f"{url} is another Maya of maya_harness: its marker is {result.output}, not {work}.")
