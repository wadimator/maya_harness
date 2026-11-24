"""A limit on the Mayas maya_harness runs at once on this machine, for every harness process.

* `count()` slots, files `slot-<n>` in `DIR`. A slot is taken by a handle of its file opened
  with no sharing: an open by anyone else fails with a sharing violation. The harness copies the
  handle into Maya (`process.give`) and closes its own, so Maya holds the slot, and Windows
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
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import process
from .process import kernel32

DIR = Path(tempfile.gettempdir()) / "maya_harness" / "slots"
VARIABLE = "MAYA_HARNESS_SLOTS"
DEFAULT_COUNT = 4  # more than 4 Mayas on 8 cores / 16 threads slow each other down (Retarget_Master)
POLL = 0.5  # seconds between the tries of a harness that waits for a slot

GENERIC_READ = 0x80000000
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x80
ERROR_SHARING_VIOLATION = 32
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]


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
            process.give(self.handle, owner)
        finally:
            self.release()

    def release(self) -> None:
        if self.handle is not None:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def describe(self, holder: dict[str, Any]) -> None:
        """Writes `slot-<n>.holder`, which shows who holds the slot."""
        holder_file(self.path).write_text(json.dumps(holder, ensure_ascii=False), encoding="utf-8")


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
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error == ERROR_SHARING_VIOLATION:
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


def take(wait: float | None, directory: Path | None = None) -> Slot:
    """Takes a free slot, waiting for one up to `wait` seconds, without a limit for None;
    NoSlot when none came free."""
    deadline = None if wait is None else time.monotonic() + wait
    while True:
        slot = try_take(directory)
        if slot is not None:
            return slot
        if deadline is not None and time.monotonic() >= deadline:
            raise NoSlot(f"all {count()} Maya slots stayed taken for {wait:g} s; {VARIABLE} sets their number")
        time.sleep(POLL if deadline is None else max(0.0, min(POLL, deadline - time.monotonic())))
