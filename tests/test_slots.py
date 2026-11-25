"""Slots on the directory of the test (`own_slots` in conftest.py); stubs stand in for Maya."""

import json
import os
import subprocess
import sys
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


def test_a_waiting_harness_says_whom_it_waits_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Polls 30 s apart: a miss, the holders, the same, the same a minute on, new holders, a slot."""
    monkeypatch.setenv(slots.VARIABLE, "2")
    now = [0.0]
    monkeypatch.setattr(slots, "clock", lambda: now[0])
    monkeypatch.setattr(slots, "pause", lambda seconds: now.__setitem__(0, now[0] + 30))
    work = tmp_path / "work"
    work.mkdir()
    (work / "last_call").touch()
    os.utime(work / "last_call", (time.time() - 600,) * 2)
    started = slots.Holder(
        1, 11, 1, "Retarget_Master", "start", "http://127.0.0.1:5/mcp", str(work), time.time() - 1200
    )
    running = slots.Holder(2, 22, 2, "UE_exporter", "run", "http://127.0.0.1:6/mcp", "", time.time() - 60)
    after = slots.Holder(2, 33, 3, "UE_exporter", "run", "http://127.0.0.1:7/mcp", "", time.time())
    tries = iter([None] * 5 + [slots.Slot(1, tmp_path / "slot-1", None)])
    seen = iter([[started, running]] * 3 + [[started, after]])
    monkeypatch.setattr(slots, "try_take", lambda directory=None: next(tries))
    monkeypatch.setattr(slots, "holders", lambda directory=None: next(seen))
    assert slots.take(None).number == 1
    err = capsys.readouterr().err
    assert err.count("all 2 Maya slots are taken, waiting for one:") == 2
    assert (
        "slot 1: Maya 11 Retarget_Master, start, 20 min, idle 10 min; free it: maya-harness stop http://127.0.0.1:5/mcp"
        in err
    )
    assert "slot 2: Maya 22 UE_exporter, run, 1 min\n" in err
    assert "slot 2: Maya 33 UE_exporter, run, 0 min\n" in err
    assert err.count("still waiting for a Maya slot, 1 min") == 1
    assert err.rstrip().endswith("got Maya slot 1 after 2.5 min")
    assert all(
        line.startswith(f"maya-harness {os.getpid()}: ") for line in err.splitlines() if not line.startswith("  ")
    )


def test_a_slot_found_on_the_second_try_is_taken_without_a_word(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A probe of `status` takes a free slot for a moment: one miss is no reason to say all are taken."""
    tries = iter([None, slots.Slot(1, tmp_path / "slot-1", None)])
    monkeypatch.setattr(slots, "try_take", lambda directory=None: next(tries))
    monkeypatch.setattr(slots, "pause", lambda seconds: None)
    slots.take(None)
    assert capsys.readouterr().err == ""


def test_holders_are_the_taken_slots_only(own_slots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A free slot keeps the .holder of its last Maya; a holder that is not a live Maya is starting."""
    monkeypatch.setenv(slots.VARIABLE, "3")
    first, second = slots.take(0), slots.take(0)
    second.describe(
        {"pid": os.getpid(), "created": 1, "project": "p", "command": "run", "url": "u", "work": "w", "time": 0}
    )
    first.describe({"pid": 1, "created": 1, "project": "p", "command": "run", "url": "u", "work": "w", "time": 0})
    first.release()
    assert slots.holders() == [slots.Holder(2)]
    second.release()
    assert slots.holders() == []


def test_in_call_is_the_start_of_a_call_whose_process_lives(tmp_path: Path) -> None:
    assert slots.in_call(tmp_path) is None
    slots.begin_call(tmp_path)
    since = slots.in_call(tmp_path)
    assert since is not None and abs(since - time.time()) < 60
    slots.end_call(tmp_path)
    assert slots.in_call(tmp_path) is None
    slots.end_call(tmp_path)  # twice: the file is gone already


def test_in_call_of_a_dead_process_is_none(tmp_path: Path) -> None:
    """An exec killed in its wait leaves the file behind: Maya is not busy forever."""
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    (tmp_path / "in_call").write_text(json.dumps({"pid": dead.pid, "since": time.time()}), encoding="utf-8")
    assert slots.in_call(tmp_path) is None


def test_a_maya_of_start_in_a_call_is_busy_not_idle(tmp_path: Path) -> None:
    now = time.time()
    (tmp_path / "last_call").touch()
    (tmp_path / "in_call").write_text(json.dumps({"pid": os.getpid(), "since": now - 600}), encoding="utf-8")
    holder = slots.Holder(1, 11, 1, "p", "start", "http://x", str(tmp_path), now - 1200)
    assert holder.line(now) == "slot 1: Maya 11 p, start, 20 min, busy 10 min; free it: maya-harness stop http://x"
