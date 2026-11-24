"""Slots on the directory of the test (`own_slots` in conftest.py); stubs stand in for Maya."""

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import process, slots

Stub = Callable[..., subprocess.Popen[str]]
HOLD = "import sys; sys.stdin.readline()"  # a holder that lives until told
GRANDCHILD = """
import subprocess, sys
sys.stdin.readline()  # started once it holds the slot
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"], close_fds=False, creationflags=0x08000000
)
print(child.pid, flush=True)
"""


def free(directory: Path) -> bool:
    """A take succeeds: the slot is free; let go of it at once."""
    slot = slots.try_take(directory)
    if slot is None:
        return False
    slot.release()
    return True


def test_a_slot_is_taken_and_released(own_slots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    slot = slots.take(0)
    assert slot.number == 1
    assert slots.try_take() is None
    slot.release()
    assert free(own_slots)


def test_a_second_take_gets_the_next_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slots.VARIABLE, "2")
    first, second = slots.take(0), slots.take(0)
    assert (first.number, second.number) == (1, 2)
    assert slots.try_take() is None
    first.release()
    second.release()


def test_all_taken_waits_and_gives_up_after_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    held = slots.take(0)
    begin = time.monotonic()
    with pytest.raises(slots.NoSlot, match="all 1 Maya slots stayed taken for 0.3 s"):
        slots.take(0.3)
    assert time.monotonic() - begin >= 0.3
    held.release()


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_a_wrong_number_of_slots_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(slots.VARIABLE, value)
    with pytest.raises(slots.NoSlot, match=slots.VARIABLE):
        slots.take(0)


def test_a_slot_given_to_a_process_is_held_until_it_is_killed(
    own_slots: Path, monkeypatch: pytest.MonkeyPatch, stub: Stub
) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    holder = stub(HOLD, stdin=subprocess.PIPE)
    slot = slots.take(0)
    slot.give(holder._handle)  # type: ignore[attr-defined]
    assert slot.handle is None  # our own handle is closed
    assert not free(own_slots)
    holder.kill()
    holder.wait(5)
    assert free(own_slots)


def test_a_slot_is_free_once_its_holder_exits_though_its_child_lives(
    own_slots: Path, monkeypatch: pytest.MonkeyPatch, stub: Stub
) -> None:
    """The handle Maya gets is not inheritable: a process Maya starts with inheritance does not hold the slot."""
    monkeypatch.setenv(slots.VARIABLE, "1")
    holder = stub(GRANDCHILD, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    slots.take(0).give(holder._handle)  # type: ignore[attr-defined]
    assert holder.stdin and holder.stdout
    holder.stdin.write("\n")
    holder.stdin.flush()
    pid = int(holder.stdout.readline())
    grandchild = process.kernel32.OpenProcess(process.PROCESS_TERMINATE | process.SYNCHRONIZE, False, pid)
    try:
        assert holder.wait(5) == 0
        assert process.kernel32.WaitForSingleObject(grandchild, 0) == process.WAIT_TIMEOUT  # alive
        assert free(own_slots)
    finally:
        process.kernel32.TerminateProcess(grandchild, 1)
        process.kernel32.CloseHandle(grandchild)


def test_a_failed_give_frees_the_slot(own_slots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slots.VARIABLE, "1")
    slot = slots.take(0)
    with pytest.raises(OSError):
        slot.give(0)  # no process
    assert free(own_slots)
