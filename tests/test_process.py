import os
import shutil
import subprocess
from pathlib import Path

from maya_harness import process
from maya_harness.process import affinity_mask, environment_block


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
    handle = process.kernel32.OpenProcess(process.PROCESS_QUERY_LIMITED_INFORMATION, False, child.pid)
    try:
        created = process.creation_time(handle)
        assert process.maya_alive(child.pid, created)
        assert not process.maya_alive(child.pid, created + 1)
        assert process.maya_alive(child.pid, 0)  # an old state.json: the pid decides
    finally:
        child.kill()
        child.wait(5)
        process.kernel32.CloseHandle(handle)
    assert not process.maya_alive(child.pid, created)


def test_kill_maya_kills_only_our_maya(tmp_path: Path) -> None:
    """A copy of ping.exe named maya.exe stands in for Maya: the same image name, a creation time of its own."""
    fake = tmp_path / "maya.exe"
    shutil.copy(Path(os.environ["SystemRoot"], "System32", "PING.EXE"), fake)
    child = subprocess.Popen([str(fake), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL)
    rights = process.PROCESS_QUERY_LIMITED_INFORMATION | process.SYNCHRONIZE
    handle = process.kernel32.OpenProcess(rights, False, child.pid)
    try:
        created = process.creation_time(handle)
        assert process.kill_maya(child.pid, created + 1)  # the pid names another process: ours is gone
        assert not process.kill_maya(child.pid, 0)  # an old state.json: cannot tell
        assert child.poll() is None
        assert process.kill_maya(child.pid, created)
        assert child.wait(5) == 1
        assert process.kill_maya(child.pid, created)  # exited, a handle still held: no OSError
        assert process.kill_maya(0x7FFFFFFC, 1)  # no such process
    finally:
        child.kill()
        process.kernel32.CloseHandle(handle)
