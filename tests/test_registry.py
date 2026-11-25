import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import process, registry
from maya_harness.registry import Record, State

PROJECT = "Retarget_Master"  # `_` in the name: the directory name cannot be split into a pid
DEAD, ALIVE, OTHER_ALIVE = 101, 202, 303
NOW = 1_000_000_000.0


def alive(pid: int) -> bool:
    return pid in (ALIVE, OTHER_ALIVE)


def maya(pid: int, created: int) -> bool:
    return alive(pid)


def make_dir(temp: Path, runner_pid: object, maya_pid: object, *, age: float = 0.0, created: int = 1) -> Path:
    root = temp / f"{registry.PREFIX}{PROJECT}_{DEAD}_abc{runner_pid}{maya_pid}"
    root.mkdir()
    state = {
        "project": PROJECT,
        "project_root": "C:/x",
        "runner_pid": runner_pid,
        "maya_pid": maya_pid,
        "url": "http://127.0.0.1:1/mcp",
        "started": NOW,
        "ttl": 60,
        "maya_created": created,
    }
    (root / registry.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    os.utime(root, (NOW - age, NOW - age))
    return root


def remove(temp: Path) -> list[Path]:
    return registry.remove_stale(temp, alive=alive, now=lambda: NOW, maya=maya)


def test_dead_runner_and_dead_maya_is_removed(tmp_path: Path) -> None:
    root = make_dir(tmp_path, DEAD, DEAD)
    assert remove(tmp_path) == [root]
    assert not root.exists()


def test_live_runner_keeps_the_directory(tmp_path: Path) -> None:
    root = make_dir(tmp_path, ALIVE, DEAD)
    assert remove(tmp_path) == []
    assert root.exists()


def test_live_maya_keeps_the_directory_of_a_dead_runner(tmp_path: Path) -> None:
    root = make_dir(tmp_path, DEAD, ALIVE)
    assert remove(tmp_path) == []
    assert root.exists()


def test_garbage_state_is_removed_when_old(tmp_path: Path) -> None:
    root = make_dir(tmp_path, "x", ALIVE, age=registry.UNCLAIMED_AGE + 1)
    assert remove(tmp_path) == [root]


def test_garbage_state_is_kept_when_fresh(tmp_path: Path) -> None:
    root = make_dir(tmp_path, "x", ALIVE)
    assert remove(tmp_path) == []
    assert root.exists()


def test_fresh_directory_without_state_is_kept(tmp_path: Path) -> None:
    root = tmp_path / f"{registry.PREFIX}{PROJECT}_{DEAD}_new"
    root.mkdir()
    os.utime(root, (NOW, NOW))
    assert remove(tmp_path) == []
    old = tmp_path / f"{registry.PREFIX}{PROJECT}_{DEAD}_old"
    old.mkdir()
    os.utime(old, (NOW - registry.UNCLAIMED_AGE - 1,) * 2)
    assert remove(tmp_path) == [old]


def test_running_lists_live_mayas_oldest_first(tmp_path: Path) -> None:
    make_dir(tmp_path, DEAD, DEAD)
    first = make_dir(tmp_path, DEAD, ALIVE)
    second = make_dir(tmp_path, ALIVE, OTHER_ALIVE)
    state = json.loads((second / registry.STATE_FILE).read_text(encoding="utf-8"))
    state["started"] = NOW + 1
    (second / registry.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    assert [found.root for found in registry.running(tmp_path, maya=maya)] == [first, second]
    assert registry.read_state(first).maya_created == 1


def test_a_maya_pid_counts_only_with_its_creation_time(tmp_path: Path) -> None:
    """Windows reuses pids: the recorded creation time goes along with the Maya pid."""
    root = make_dir(tmp_path, DEAD, ALIVE, created=7)

    def created_at(time: int) -> Callable[[int, int], bool]:
        return lambda pid, created: alive(pid) and created == time

    assert [found.root for found in registry.running(tmp_path, maya=created_at(7))] == [root]
    assert registry.running(tmp_path, maya=created_at(8)) == []
    assert registry.remove_stale(tmp_path, alive=alive, now=lambda: NOW, maya=created_at(7)) == []
    assert registry.remove_stale(tmp_path, alive=alive, now=lambda: NOW, maya=created_at(8)) == [root]


def test_state_round_trip(tmp_path: Path) -> None:
    state = State(PROJECT, "C:/x", 1, 2, "http://127.0.0.1:1/mcp", NOW, 60, 134_000_000_000_000_000)
    registry.write_state(tmp_path, state)
    assert registry.read_state(tmp_path) == state


@pytest.mark.parametrize("key", ["ttl", "maya_created"])
def test_state_without_a_key_is_none(tmp_path: Path, key: str) -> None:
    registry.write_state(tmp_path, State(PROJECT, "C:/x", 1, 2, "http://127.0.0.1:1/mcp", NOW, 60, 7))
    data = json.loads((tmp_path / registry.STATE_FILE).read_text(encoding="utf-8"))
    del data[key]
    (tmp_path / registry.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")
    assert registry.read_state(tmp_path) is None


def record(root: str, started: float, url: str = "") -> Record:
    state = State(PROJECT, str(Path(root)), 1, 2, url or f"http://127.0.0.1:{int(started)}/mcp", started, 0, 1)
    return Record(Path("C:/temp") / f"maya_harness_{started}", state)


def test_select_takes_the_url_or_the_latest_of_the_project_root(tmp_path: Path) -> None:
    """Two live sessions with one project name: the clone and its worktree."""
    clone, worktree = tmp_path / "UE_exporter", tmp_path / "wt" / "UE_exporter"
    older, own, newer_other = record(str(clone), 1), record(str(clone), 2), record(str(worktree), 3)
    found = [older, own, newer_other]
    assert registry.select(found, None, clone) == own
    assert registry.select(found, None, worktree) == newer_other
    assert registry.select(found, None, tmp_path / "other") is None
    assert registry.select(found, older.state.url, None) == older
    assert registry.select(found, "http://127.0.0.1:9/mcp", None) is None


def test_stop_removes_the_directory_only_of_a_killed_maya(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stuck, killed = (
        Record(tmp_path / name, State(PROJECT, "C:/p", 1, pid, "u", NOW, 0, 1)) for name, pid in (("a", 10), ("b", 20))
    )
    for item in (stuck, killed):
        item.root.mkdir()
    monkeypatch.setattr(process, "kill_maya", lambda pid, created: pid == 20)
    assert not registry.stop(stuck) and stuck.root.is_dir()
    assert registry.stop(killed) and not killed.root.exists()


def test_describe_tells_a_running_call_from_idleness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = Record(tmp_path, State(PROJECT, str(tmp_path), 1, 10, "http://127.0.0.1:1/mcp", time.time() - 600, 5, 1))
    item.work.mkdir()
    monkeypatch.setattr(process, "window_titles", lambda pid: ["untitled - Autodesk MAYA"])
    row = registry.describe(item, time.time())
    assert (row["busy"], row["ttl"], row["windows"]) == (None, 5, ["untitled - Autodesk MAYA"])
    assert 599 <= row["idle"] <= 601 and 599 <= row["age"] <= 601
