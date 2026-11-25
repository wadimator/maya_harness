import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import process, win32
from maya_harness.process import affinity_mask, environment_block

CREATE_NO_WINDOW = 0x08000000
PROCESS_ALL_ACCESS = 0x1FFFFF


def test_affinity_spares_the_first_cpus_above_eight() -> None:
    assert affinity_mask(16) == 0b1111_1111_1111_0000


def test_affinity_has_no_limit_up_to_eight_cpus() -> None:
    assert affinity_mask(8) == 0
    assert affinity_mask(4) == 0


def test_environment_block_is_sorted_and_double_terminated() -> None:
    block = environment_block({"b": "2", "A": "1", "C": "3"})
    assert block == "A=1\0b=2\0C=3\0\0"


def test_is_our_maya_needs_the_image_and_the_creation_time() -> None:
    assert process.is_our_maya("C:/Autodesk/Maya2025/bin/maya.exe", 5, 5)
    assert process.is_our_maya("C:/Autodesk/Maya2025/bin/MAYA.EXE", 5, 5)
    assert not process.is_our_maya("C:/Python/python.exe", 5, 5)
    assert not process.is_our_maya("C:/Autodesk/Maya2025/bin/maya.exe", 6, 5)


def test_maya_alive_needs_the_recorded_creation_time(tmp_path: Path) -> None:
    """A pid Windows gave to another process is not our Maya, though the process runs."""
    fake = tmp_path / "maya.exe"
    shutil.copy(Path(os.environ["SystemRoot"], "System32", "PING.EXE"), fake)
    child = subprocess.Popen([str(fake), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL)
    handle = win32.kernel32.OpenProcess(win32.PROCESS_QUERY_LIMITED_INFORMATION, False, child.pid)
    try:
        created = win32.creation_time(handle)
        assert process.maya_alive(child.pid, created)
        assert not process.maya_alive(child.pid, created + 1)
        assert process.maya_alive(child.pid, 0)  # an old state.json: the pid decides
    finally:
        child.kill()
        child.wait(5)
        win32.kernel32.CloseHandle(handle)
    assert not process.maya_alive(child.pid, created)


def test_kill_maya_kills_only_our_maya(tmp_path: Path) -> None:
    """A copy of ping.exe named maya.exe stands in for Maya: the same image name, a creation time of its own."""
    fake = tmp_path / "maya.exe"
    shutil.copy(Path(os.environ["SystemRoot"], "System32", "PING.EXE"), fake)
    child = subprocess.Popen([str(fake), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL)
    rights = win32.PROCESS_QUERY_LIMITED_INFORMATION | win32.SYNCHRONIZE
    handle = win32.kernel32.OpenProcess(rights, False, child.pid)
    try:
        created = win32.creation_time(handle)
        assert process.kill_maya(child.pid, created + 1)  # the pid names another process: ours is gone
        assert not process.kill_maya(child.pid, 0)  # an old state.json: cannot tell
        assert child.poll() is None
        assert process.kill_maya(child.pid, created)
        assert child.wait(5) == 1
        assert process.kill_maya(child.pid, created)  # exited, a handle still held: no OSError
        assert process.kill_maya(0x7FFFFFFC, 1)  # no such process
    finally:
        child.kill()
        win32.kernel32.CloseHandle(handle)


GRANDCHILD = f"""
import subprocess, sys
sys.stdin.readline()  # started once the stub is in the job
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags={CREATE_NO_WINDOW})
print(child.pid, flush=True)
sys.stdin.readline()
"""


def test_a_released_maya_takes_what_it_started_along_when_killed(stub: Callable[..., subprocess.Popen[str]]) -> None:
    """The license agents of a real Maya stand in a grandchild here. A `MayaProcess` runs only
    maya.exe (`-hideConsole`), so the stub is put in a job of its own the way `MayaProcess` does."""
    child = stub(GRANDCHILD, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    maya = object.__new__(process.MayaProcess)
    maya.desktop, maya.closed, maya._lock = None, False, threading.Lock()
    maya.job = win32.kernel32.CreateJobObjectW(None, None)
    maya._set_job_limits(win32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    maya.handle = win32.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, child.pid)
    assert win32.kernel32.AssignProcessToJobObject(maya.job, maya.handle)
    assert child.stdin and child.stdout
    child.stdin.write("\n")
    child.stdin.flush()
    pid = int(child.stdout.readline())
    grandchild = win32.kernel32.OpenProcess(win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid)
    try:
        maya.release()
        assert child.poll() is None  # the job no longer closes with the harness
        child.kill()
        child.wait(5)
        assert win32.kernel32.WaitForSingleObject(grandchild, 5000) == 0
    finally:
        win32.kernel32.TerminateProcess(grandchild, 1)
        win32.kernel32.CloseHandle(grandchild)


def test_modal_dialog_is_the_other_windows_while_the_main_window_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dialog that has no title yet is not one: `confirmDialog` disables the window first."""
    main = (1, "untitled - Autodesk MAYA 2025.3.1: untitled")
    windows = [main]
    enabled = [True]
    monkeypatch.setattr(process, "_windows", lambda pid, desktop: windows)
    monkeypatch.setattr(win32.user32, "IsWindowEnabled", lambda window: enabled[0])
    windows.append((2, "Script Editor"))
    assert process.modal_dialog(10) == []
    enabled[0] = False
    windows.remove((2, "Script Editor"))
    assert process.modal_dialog(10) == []
    windows.append((3, "Untrusted Plugin Loading - Security Warning"))
    assert process.modal_dialog(10) == ["Untrusted Plugin Loading - Security Warning"]
    windows.remove(main)
    assert process.modal_dialog(10) == []
