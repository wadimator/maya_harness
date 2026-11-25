"""A limit on the Mayas maya_harness runs at once on this machine, for every harness process.

* `count()` slots, files `slot-<n>` in `DIR`. A slot is taken by a handle of its file opened
  with no sharing: an open by anyone else fails with a sharing violation. The harness copies the
  handle into Maya (`win32.give`) and closes its own, so Maya holds the slot, and Windows
  closes the handle when Maya dies, however it dies: a kill, a crash, its idle timer, the job of
  a killed harness. A slot is never left taken by nobody.
* The handle is opened for reading: an open with access 0 does not check the share mode, so a
  second take with access 0 succeeds on a taken slot, and a probe for reading sees a slot taken
  with access 0 free.
* Not `LockFileEx`: the lock belongs to the process that took it and goes when that process
  closes its handle, though Maya holds a copy. Not an inherited handle (`HANDLE_LIST`): every
  process Maya starts with inheritance would hold the slot past Maya. Not a take by Maya itself
  in `userSetup.py`: that needs a reservation for the start, `O_EXCL` and a timeout.
* `slot-<n>.holder` is JSON only to show who holds a slot; it is read only while the slot is
  taken: a free slot may keep the one of a dead Maya. A holder whose Maya is not alive is still
  starting: the slot is taken, its `.holder` not yet written.
* A harness that waits says whom it waits for, on stderr (stdout of `--json` is one JSON line):
  every holder, and for a Maya of `start` its idle time or running call and how to free its
  slot. Again in full when the holders change, else a line a minute. One `write` a message,
  with the pid of the waiting harness: the workers of a pool wait at once.
* Telling a taken slot from a free one means opening it, which takes a free slot for a moment:
  a harness that finds no free slot says so only when the next try finds none either.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import call, process, win32

DIR = Path(tempfile.gettempdir()) / "maya_harness" / "slots"
VARIABLE = "MAYA_HARNESS_SLOTS"
DEFAULT_COUNT = 6  # on 8 cores / 16 threads 6 Mayas run `all` in 494 s, 3 in 791 s, 8 in 475 s
POLL = 0.5  # seconds between the tries of a harness that waits for a slot
REMINDER = 60  # seconds between the lines of a harness that still waits


class NoSlot(Exception):
    """No slot was free in time, or the number of slots is wrong."""


@dataclass
class Slot:
    """A slot this process took; `give` hands it to Maya, `release` lets it go."""

    number: int
    path: Path
    handle: int | None

    def give(self, owner: object) -> None:
        """Hands the slot to the process `owner`: it holds the slot until it dies. Our own handle
        is closed either way: on an error the slot is free again."""
        assert self.handle is not None
        try:
            win32.give(self.handle, owner)
        finally:
            self.release()

    def release(self) -> None:
        if self.handle is not None:
            win32.kernel32.CloseHandle(self.handle)
            self.handle = None

    def describe(self, holder: Holder) -> None:
        """Writes `slot-<n>.holder`, which shows who holds the slot. Its keys are those every
        version reads: the slots are shared by them all."""
        data = {
            "pid": holder.pid,
            "created": holder.created,
            "project": holder.project,
            "command": holder.command,
            "url": holder.url,
            "work": holder.work,
            "time": holder.since,
        }
        holder_file(self.path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def count() -> int:
    value = os.environ.get(VARIABLE, "")
    if not value:
        return DEFAULT_COUNT
    if not value.isdigit() or int(value) < 1:
        raise NoSlot(f"{VARIABLE}={value!r}: the number of Mayas at once is a whole number, 1 or more")
    return int(value)


def slot_file(number: int, directory: Path | None = None) -> Path:
    return (directory or DIR) / f"slot-{number}"


def holder_file(path: Path) -> Path:
    return path.with_name(f"{path.name}.holder")


def open_slot(path: Path) -> int | None:
    """A handle of the slot file with no sharing; None when the slot is taken."""
    handle = win32.kernel32.CreateFileW(
        str(path), win32.GENERIC_READ, 0, None, win32.OPEN_ALWAYS, win32.FILE_ATTRIBUTE_NORMAL, None
    )
    if handle is None or handle == win32.INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error == win32.ERROR_SHARING_VIOLATION:
            return None
        raise ctypes.WinError(error)
    return int(handle)


def try_take(directory: Path | None = None) -> Slot | None:
    """The first free slot, taken; None when every slot is taken."""
    (directory or DIR).mkdir(parents=True, exist_ok=True)
    for number in range(1, count() + 1):
        path = slot_file(number, directory)
        handle = open_slot(path)
        if handle is not None:
            return Slot(number, path, handle)
    return None


@dataclass(frozen=True)
class Holder:
    """Who holds a taken slot, from its `.holder`; `pid` 0 for a Maya still starting."""

    number: int
    pid: int = 0
    created: int = 0
    project: str = ""
    command: str = ""
    url: str = ""
    work: str = ""
    since: float = 0.0

    def line(self, now: float) -> str:
        if not self.pid:
            return f"slot {self.number}: a Maya starting"
        text = f"slot {self.number}: Maya {self.pid} {self.project}, {self.command}, {(now - self.since) // 60:.0f} min"
        if self.command == "start":
            busy_since, idle_since = call.activity(Path(self.work), self.since)
            if busy_since is not None:
                doing = f"busy {(now - busy_since) // 60:.0f} min"
            else:
                doing = f"idle {(now - idle_since) // 60:.0f} min"
            text += f", {doing}; free it: maya-harness stop {self.url}"
        return text


def holders(directory: Path | None = None) -> list[Holder]:
    """The taken slots and who holds them."""
    found = []
    for number in range(1, count() + 1):
        path = slot_file(number, directory)
        if not path.exists():  # never taken
            continue
        handle = open_slot(path)  # for reading: an open with access 0 succeeds on a taken slot too
        if handle is not None:  # free
            win32.kernel32.CloseHandle(handle)
            continue
        found.append(read_holder(number, path))
    return found


def read_holder(number: int, path: Path) -> Holder:
    try:
        data = json.loads(holder_file(path).read_text(encoding="utf-8"))
        holder = Holder(
            number,
            int(data["pid"]),
            int(data["created"]),
            str(data["project"]),
            str(data["command"]),
            str(data["url"]),
            str(data["work"]),
            float(data["time"]),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return Holder(number)
    return holder if process.maya_alive(holder.pid, holder.created) else Holder(number)


def clock() -> float:
    return time.monotonic()


def pause(seconds: float) -> None:
    time.sleep(seconds)


def say(text: str) -> None:
    sys.stderr.write(f"maya-harness {os.getpid()}: {text}\n")
    sys.stderr.flush()


def take(wait: float | None, directory: Path | None = None, unneeded: Callable[[], bool] | None = None) -> Slot:
    """Takes a free slot, waiting for one up to `wait` seconds, without a limit for None, and
    saying whom it waits for; NoSlot when none came free. `unneeded` is asked after every miss:
    True ends the wait with NoSlot."""
    began = clock()
    deadline = None if wait is None else began + wait
    misses = 0
    shown: tuple[tuple[int, int, int], ...] | None = None
    reminded = began
    while True:
        slot = try_take(directory)
        if slot is not None:
            if shown is not None:
                say(f"got Maya slot {slot.number} after {(clock() - began) / 60:.1f} min")
            return slot
        now = clock()
        if deadline is not None and now >= deadline:
            raise NoSlot(f"all {count()} Maya slots stayed taken for {wait:g} s; {VARIABLE} sets their number")
        if unneeded is not None and unneeded():
            raise NoSlot("a Maya slot is no longer needed")
        misses += 1
        if misses > 1:
            taken = holders(directory)
            key = tuple((holder.number, holder.pid, holder.created) for holder in taken)
            if key != shown:
                wall = time.time()
                lines = [f"all {count()} Maya slots are taken, waiting for one:"]
                lines += [f"  {holder.line(wall)}" for holder in taken]
                say("\n".join(lines))
                shown, reminded = key, now
            elif now - reminded >= REMINDER:
                say(f"still waiting for a Maya slot, {(now - began) // 60:.0f} min")
                reminded = now
        pause(POLL if deadline is None else max(0.0, min(POLL, deadline - now)))
