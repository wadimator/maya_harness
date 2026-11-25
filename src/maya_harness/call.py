"""The one way to run code in the Maya of a session: connect to it, then calls marked for the
idle timer and for `status`.

* `connect` makes the handshake, then checks the marker: a session's Maya holds its work
  directory in `inmaya.MARKER`, and the Maya a session started must hold its own one — a port
  another Maya took would run the code there. The marker goes to Maya's main thread like any
  code: a Maya on a modal dialog holds it, and only the watch cuts that wait short.
* `Target.execute` touches `inmaya.LAST_CALL` before and after the call: the idle timer is on
  Maya's main thread and is silent while a call runs there, so without the touch after the call
  a call longer than the TTL would be followed by a quit at once. `IN_CALL` tells `status`,
  `stop` and a harness waiting for a slot a running call from idleness: the process that waits
  for it, and since when.
* The watch of a call into a Maya with a known pid cuts the call short with `CallAborted` when a
  modal dialog holds Maya, which takes no call until someone answers it. The call is not taken
  back: the code runs on once the dialog is answered, so Maya is left as it is. A Maya at a
  foreign address has no known pid and no watch.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import inmaya, process, transport

IN_CALL = "in_call"  # in the work directory: the pid that waits for the running call, and since when
MARKER_CODE = f"import json, os\njson.dumps(os.environ.get({inmaya.MARKER!r}, ''))"


def watcher(pid: int, url: str, desktop: object = None) -> transport.Watch:
    """The watch of a call into the Maya `pid`: see the module docstring."""

    def watch() -> None:
        titles = process.modal_dialog(pid, desktop)
        if titles:
            raise transport.CallAborted(
                f"Maya {pid} shows a modal dialog: {titles}; the call is still running there: "
                f"answer it, or maya-harness stop {url}"
            )

    return watch


@dataclass(frozen=True)
class Target:
    """A connected Maya of a session: its address, its work directory and, when known, its pid
    and desktop."""

    url: str
    work: Path
    pid: int | None = None
    desktop: object = None

    def watch(self) -> transport.Watch | None:
        """Cuts a call short when this Maya stands on a modal dialog; None without a pid."""
        return None if self.pid is None else watcher(self.pid, self.url, self.desktop)

    def execute(self, code: str, timeout: float, watch: transport.Watch | None = None) -> transport.Result:
        """Runs `code` in this Maya, marked as a call; `watch` is `self.watch()` by default."""
        last_call = self.work / inmaya.LAST_CALL
        in_call = self.work / IN_CALL
        last_call.touch()
        in_call.write_text(json.dumps({"pid": os.getpid(), "since": time.time()}), encoding="utf-8")
        try:
            return transport.execute(self.url, code, timeout, self.watch() if watch is None else watch)
        finally:
            in_call.unlink(missing_ok=True)
            last_call.touch()


def connect(
    url: str,
    timeout: float,
    *,
    work: Path | None = None,
    pid: int | None = None,
    desktop: object = None,
    watch: transport.Watch | None = None,
) -> Target:
    """The Maya at `url` after the handshake and the marker; `work`, when given, is the work
    directory it must hold. `watch` is the one of `pid` by default."""
    if watch is None and pid is not None:
        watch = watcher(pid, url, desktop)
    transport.handshake(url, timeout, watch)
    result = transport.execute(url, MARKER_CODE, timeout, watch)
    if not result.success:
        raise transport.TransportError(f"{url} did not check its marker: {result.error or result.raw}")
    marker = result.value()
    if not marker:
        raise transport.TransportError(f"{url} is not a Maya started by maya_harness: nothing is run there.")
    if work is not None and marker != str(work):
        raise transport.TransportError(f"{url} is another Maya of maya_harness: its marker is {marker}, not {work}.")
    return Target(url, Path(marker), pid, desktop)


def _running_call(work: Path) -> float | None:
    """When the running call began; None when none runs. A call whose process is gone is none:
    an `exec` killed in its wait leaves `IN_CALL` behind, and Maya is not busy forever."""
    try:
        mark = json.loads((work / IN_CALL).read_text(encoding="utf-8"))
        pid, since = mark["pid"], mark["since"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(pid, int) or not isinstance(since, int | float) or not process.process_alive(pid):
        return None
    return float(since)


def activity(work: Path, since: float) -> tuple[float | None, float]:
    """When the running call began, None when none runs, and when the Maya went idle: the end of
    its last call, else `since`."""
    try:
        idle_since = (work / inmaya.LAST_CALL).stat().st_mtime
    except OSError:
        idle_since = since
    return _running_call(work), idle_since
