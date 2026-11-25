"""The sessions on disk that outlive their harness: find them, describe them, stop them.

* A session directory is `%TEMP%/maya_harness_<project>_<pid>_*`. `state.json` in it names its
  project, the harness and Maya pids and the address. `status`, `exec` and the cleanup read it,
  not the directory name: after the prefix comes the project, and a project name with `_` in it
  (`Retarget_Master`) cannot be split into a pid.
* A directory is kept while its harness or its Maya is alive. One without a readable
  `state.json` is removed only a day after it was made: a fresh one may not have written it yet.
* Windows reuses pids: a Maya is alive only with the creation time `state.json` recorded.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import call, process

PREFIX = "maya_harness_"
STATE_FILE = "state.json"
UNCLAIMED_AGE = 24 * 60 * 60  # seconds a directory without a readable state.json is kept


@dataclass(frozen=True)
class State:
    """The contents of `state.json`. `started` is seconds since the epoch, `ttl` minutes."""

    project: str
    project_root: str
    runner_pid: int
    maya_pid: int
    url: str
    started: float
    ttl: int
    maya_created: int


@dataclass(frozen=True)
class Record:
    """A session found on disk: its directory and its state.json."""

    root: Path
    state: State

    @property
    def work(self) -> Path:
        return self.root / "work"


def read_state(root: Path) -> State | None:
    """The state of the session in `root`; None when `state.json` is missing, broken or holds a
    pid that is not a number."""
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        state = State(
            project=str(data["project"]),
            project_root=str(data["project_root"]),
            runner_pid=data["runner_pid"],
            maya_pid=data["maya_pid"],
            url=str(data["url"]),
            started=float(data["started"]),
            ttl=int(data["ttl"]),
            maya_created=int(data["maya_created"]),
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    pids = (state.runner_pid, state.maya_pid)
    if not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in pids):
        return None
    return state


def write_state(root: Path, state: State) -> None:
    (root / STATE_FILE).write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def remove_stale(
    temp: Path,
    alive: Callable[[int], bool] = process.process_alive,
    now: Callable[[], float] = time.time,
    maya: Callable[[int, int], bool] = process.maya_alive,
) -> list[Path]:
    """Removes the session directories nobody holds; returns them."""
    removed = []
    for root in temp.glob(f"{PREFIX}*"):
        if not root.is_dir():
            continue
        state = read_state(root)
        if state is None:
            try:
                fresh = now() - root.stat().st_mtime < UNCLAIMED_AGE
            except OSError:
                continue
            if fresh:
                continue
        elif alive(state.runner_pid) or maya(state.maya_pid, state.maya_created):
            continue
        shutil.rmtree(root, ignore_errors=True)
        removed.append(root)
    return removed


def running(temp: Path, maya: Callable[[int, int], bool] = process.maya_alive) -> list[Record]:
    """The sessions whose Maya is alive, the latest started last."""
    found = []
    for root in temp.glob(f"{PREFIX}*"):
        state = read_state(root) if root.is_dir() else None
        if state is not None and maya(state.maya_pid, state.maya_created):
            found.append(Record(root, state))
    return sorted(found, key=lambda session: session.state.started)


def of_project(found: list[Record], root: Path) -> list[Record]:
    """The sessions started from the project at `root`, the latest last. By the project root, not
    the project name: a worktree has the same name, and its Maya has the other tree on sys.path."""
    return [item for item in found if Path(item.state.project_root) == root]


def select(found: list[Record], url: str | None, root: Path | None) -> Record | None:
    """The session at `url`; without one, the latest of the project at `root`. None when there is
    none: a Maya at `url` may be no session of maya_harness."""
    if url:
        return next((item for item in found if item.state.url == url), None)
    assert root is not None
    mine = of_project(found, root)
    return mine[-1] if mine else None


def describe(record: Record, now: float) -> dict[str, Any]:
    """A session for `status` and `stop`: `busy` — the seconds of the running call, None when none
    runs; `idle` — since the last call; `windows` — what Maya shows, a dialog on the hidden desktop
    too."""
    since, idle_since = call.activity(record.work, record.state.started)
    return {
        "project": record.state.project,
        "project_root": record.state.project_root,
        "pid": record.state.maya_pid,
        "url": record.state.url,
        "age": round(now - record.state.started),
        "idle": round(now - idle_since),
        "busy": None if since is None else round(now - since),
        "ttl": record.state.ttl,
        "dir": str(record.root),
        "windows": process.window_titles(record.state.maya_pid),
    }


def stop(record: Record) -> bool:
    """Kills the Maya of `record` and removes its directory; False when Maya still runs: the
    directory stays, its logs tell why."""
    if not process.kill_maya(record.state.maya_pid, record.state.maya_created):
        return False
    shutil.rmtree(record.root, ignore_errors=True)
    return True
