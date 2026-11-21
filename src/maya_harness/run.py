"""`maya-harness run`: the project's check scenarios, in Mayas of their own or in one at `--url`.

* A scenario is a module `check_*.py` in `[tool.maya_harness.scenarios].dir`; its
  `run(**params)` returns a report whose `failures` are `<name>: <message>` (`inmaya.Check`).
  Each run imports it fresh: the scenario modules and the project's `reload` packages are
  dropped from `sys.modules` first, so the code on disk is what runs.
* A control breaks on purpose what its scenario checks and must fail first on an expected
  failure; the expectation is in the table of runs, since the harness cannot import a scenario,
  which needs Maya. The table (`[tool.maya_harness.scenarios].runs`) is TOML:

  ```toml
  [[run]]
  scenario = "check_full"
  control = "1"            # absent for the scenario itself
  expect = "reopened.ma"   # the first failure of the control; only with control
  seconds = 51             # measured; `all` runs the longest first, so the Mayas end close together
  ```

* The pool: up to `--jobs` Mayas, each a session of its own in the job of this process; each
  takes the next run from one queue. A Maya that crashes or times out fails its run and leaves;
  the others take the rest. Ctrl+C lets no Maya take another run. The Mayas are killed and their
  session directories removed at the end; a session writes no `state.json`, so `exec` and `stop`
  never pick a Maya of a run.
* Mayas at once: on 8 cores / 16 threads 3 Mayas slow a scenario by 14%, 4 by 19%, 7 by 16-60%
  (Retarget_Master, 2026).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from . import config, process, session, transport

PREFIX = "check_"  # a scenario's module name; the modules with it are dropped before each run
JOBS = 3
MAX_JOBS = 4
TIMEOUT = 900  # seconds per scenario: a run may reopen scenes with heavy rigs
RUN_KEYS = ("scenario", "control", "expect", "seconds")

# Executed in Maya: fresh project and scenario modules, then the scenario.
CODE = """
import json, sys
_path = {directory!r}
while _path in sys.path:
    sys.path.remove(_path)
sys.path.insert(0, _path)
for _name in tuple(sys.modules):
    if _name.split(".")[0] in {reload!r} or _name.startswith({prefix!r}):
        del sys.modules[_name]
import {module}
json.dumps({module}.run(**{params!r}), ensure_ascii=False, default=str)
"""


class Refused(ValueError):
    """The arguments or the table of runs are wrong: no Maya is started."""


class Run(NamedTuple):
    scenario: str
    params: dict[str, str]
    expected: str  # the first failure a control must fail on; empty for a scenario


class Outcome(NamedTuple):
    seconds: float
    ok: bool
    text: str
    maya: int  # the Maya's number; 0 for the one at --url


def load_runs(path: Path) -> list[Run]:
    """The table of runs, the longest first."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Refused(f"no table of runs: {error}") from error
    entries = data.get("run", [])
    if not isinstance(entries, list) or not entries:
        raise Refused(f"{path}: no [[run]] entries")
    measured: list[tuple[float, Run]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise Refused(f"{path}: {entry!r}: a run is a [[run]] table")
        unknown = sorted(set(entry) - set(RUN_KEYS))
        scenario, control, expected = entry.get("scenario"), entry.get("control", ""), entry.get("expect", "")
        seconds = entry.get("seconds", 0)
        if unknown or not isinstance(scenario, str) or not scenario.startswith(PREFIX):
            raise Refused(f"{path}: {entry}: a run is {', '.join(RUN_KEYS)}, its scenario {PREFIX}<name>")
        if not isinstance(control, str) or not isinstance(expected, str) or bool(control) != bool(expected):
            raise Refused(f"{path}: {entry}: a control and its expect go together, as strings")
        if not isinstance(seconds, int | float) or isinstance(seconds, bool):
            raise Refused(f"{path}: {entry}: seconds must be a number")
        if (scenario, control) in seen:
            raise Refused(f"{path}: {entry}: the run is listed twice")
        seen.add((scenario, control))
        measured.append((seconds, Run(scenario, {"control": control} if control else {}, expected)))
    return [run for _, run in sorted(measured, key=lambda item: -item[0])]


def parse(items: list[str], table: list[Run]) -> list[Run]:
    """Scenario names, or one scenario and its `name=value` parameters; `all` is the table."""
    names = [item for item in items if "=" not in item]
    params = dict(item.split("=", 1) for item in items if "=" in item)
    # A control or a section among several names would replace every run of its scenario: `all section=film`
    # would make 20 runs of it.
    if params and (len(names) != 1 or names == ["all"]):
        raise Refused("A control or a section runs alone: <scenario> control=<value> | section=<name>.")
    if names == ["all"]:
        return list(table)
    expected = {(run.scenario, run.params["control"]): run.expected for run in table if run.params}
    return [Run(name, params, expected.get((name, params.get("control", "")), "")) for name in names]


def refuse_unknown(runs: list[Run], scenarios: config.Scenarios) -> None:
    """A run of a scenario that is not there, or of a control the table does not list: a typo
    would run the plain scenario."""
    for run in runs:  # `all` among other names is caught here too: there is no all.py
        if not run.scenario.startswith(PREFIX) or not (scenarios.dir / f"{run.scenario}.py").is_file():
            raise Refused(f"no scenario {run.scenario!r} in {scenarios.dir}")
        if set(run.params) == {"section"}:  # the scenario itself refuses a section it has not
            continue
        if run.params and (set(run.params) != {"control"} or not run.expected):
            raise Refused(
                f"{run.scenario} has no control {run.params}: the controls are listed in {scenarios.runs}, "
                "or section=<name>"
            )


def code_of(run: Run, directory: Path, reload: tuple[str, ...]) -> str:
    return CODE.format(directory=str(directory), reload=reload, prefix=PREFIX, module=run.scenario, params=run.params)


def run_scenario(url: str, code: str) -> tuple[list[str] | None, str]:
    """The scenario's failures, None when it raised or returned no report with a list of failures, and its
    output: the caller prints it in one piece, and the output of runs in other Mayas does not get between its
    lines."""
    result = transport.execute(url, code, TIMEOUT)
    output = [text.rstrip() for text in (result.stdout, result.stderr) if text]
    if not result.success:
        output.append(result.error or json.dumps(result.raw, ensure_ascii=False)[-6000:])
        return None, "\n".join(output)
    report = json.loads(result.output)
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("failures"), list)
        or not all(isinstance(failure, str) for failure in report["failures"])
    ):
        output.append(f"run() returned no report with a list of failures: {result.output[-6000:]}")
        return None, "\n".join(output)
    output.append(json.dumps(report, ensure_ascii=False, indent=2))
    return list(report["failures"]), "\n".join(output)


def verdict(expected: str, failures: list[str] | None) -> tuple[bool, str]:
    """A control passes when its scenario fails first on the expected failure: a failure elsewhere
    means the control broke something else, or the check it stands for no longer holds. A scenario that
    raised fails either way.

    A failure's name is up to its first `:`; it must equal the expected one, as a prefix
    `fingers.gloves` would match `fingers.gloves_rad`."""
    if failures is None:
        return False, "FAIL: raised"
    if not expected:
        return (True, "ok") if not failures else (False, f"FAIL: {len(failures)} failures")
    if not failures:
        return False, "FAIL: the control passed"
    first = failures[0].split(":", 1)[0]
    if first != expected:
        return False, f"FAIL: the control failed on {first}, expected {expected}"
    return True, f"ok: the control failed on {first}"


def label_of(run: Run) -> str:
    return " ".join([run.scenario, *(f"{name}={value}" for name, value in run.params.items())])


def print_summary(runs: list[Run], outcomes: dict[int, Outcome], out: TextIO) -> bool:
    """The summary in the order of the runs; a run no Maya got to is «not run». True when every run passed."""
    print(file=out)
    for index, run in enumerate(runs):
        label = label_of(run)
        if index in outcomes:
            seconds, _, text, maya = outcomes[index]
            print(f"{label:<36} maya{maya} {seconds:6.1f} s  {text}", file=out, flush=True)
        else:
            print(f"{label:<36} {'':>14}  not run", file=out, flush=True)
    return passed(runs, outcomes)


def passed(runs: list[Run], outcomes: dict[int, Outcome]) -> bool:
    return len(outcomes) == len(runs) and all(outcome.ok for outcome in outcomes.values())


def summary(runs: list[Run], outcomes: dict[int, Outcome]) -> dict[str, Any]:
    """The summary for `--json`."""
    rows = []
    for index, run in enumerate(runs):
        row: dict[str, Any] = {"scenario": run.scenario, "params": run.params, "expect": run.expected}
        outcome = outcomes.get(index)
        if outcome is None:
            row |= {"ok": False, "text": "not run"}
        else:
            row |= {"ok": outcome.ok, "text": outcome.text, "seconds": round(outcome.seconds, 1), "maya": outcome.maya}
        rows.append(row)
    return {"success": passed(runs, outcomes), "runs": rows}


def run_at_url(url: str, work: Path, runs: list[Run], codes: list[str], out: TextIO) -> dict[int, Outcome]:
    """The runs one after another in the Maya at `url`, a session `start` left running; the summary is printed
    even when a run is cut short. A call Maya does not answer fails its run, and the runs after it are «not run»."""
    outcomes: dict[int, Outcome] = {}
    try:
        for index, (run, code) in enumerate(zip(runs, codes, strict=True)):
            print(f"=== {label_of(run)}", file=out, flush=True)
            start = time.monotonic()
            session.touch(work)
            try:
                failures, output = run_scenario(url, code)
            except (OSError, transport.TransportError) as error:  # Maya is gone or busy: the runs after it wait
                text = f"FAIL: no reply from {url} — {type(error).__name__}: {error}"
                outcomes[index] = Outcome(time.monotonic() - start, False, text, 0)
                break
            finally:
                session.touch(work)
            print(output, file=out, flush=True)
            outcomes[index] = Outcome(time.monotonic() - start, *verdict(run.expected, failures), 0)
    finally:
        print_summary(runs, outcomes, out)
    return outcomes


@dataclass
class Slot:
    """One Maya of the pool and its session directory."""

    root: Path
    maya: process.MayaProcess
    url: str

    @property
    def work(self) -> Path:
        return self.root / "work"


class Pool:
    """Mayas of this run, each driven by a worker thread that takes runs from one queue in their order.

    A Maya that crashes or times out fails its run and leaves; the others take the rest of the queue. A stop
    (Ctrl+C) lets no worker take another run."""

    def __init__(self, runs: list[Run], codes: list[str], slots: list[Slot], out: TextIO) -> None:
        self.runs = runs
        self.codes = codes
        self.slots = slots
        self.out = out
        self.queue: queue.Queue[int] = queue.Queue()
        for index in range(len(runs)):
            self.queue.put(index)
        self.outcomes: dict[int, Outcome] = {}
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.ready: set[int] = set()  # the indexes of the Mayas that started and connected

    def run(self) -> bool:
        workers = [threading.Thread(target=self.work, args=(number,), daemon=True) for number in range(len(self.slots))]
        for worker in workers:
            worker.start()
        try:
            # join with a timeout: a plain join holds Ctrl+C until the worker ends.
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    worker.join(0.2)
        except KeyboardInterrupt:
            self.stop.set()
            for slot in self.slots:
                titles = slot.maya.window_titles()
                print(f"KeyboardInterrupt: the windows of Maya {slot.maya.pid}: {titles}", file=sys.stderr)
            raise
        finally:
            with self.lock:
                result = print_summary(self.runs, self.outcomes, self.out)
        return result

    def work(self, number: int) -> None:
        slot = self.slots[number]
        maya = slot.maya
        name = f"maya{number + 1}"
        try:
            transport.wait_for_server(slot.url, slot.work, maya.running, transport.STARTUP_TIMEOUT)
            transport.connect(slot.url, transport.STARTUP_TIMEOUT, slot.work)
        except (transport.TransportError, TimeoutError, OSError) as error:
            if not self.stop.is_set():
                with self.lock:
                    print(f"{name}: Maya {maya.pid} did not start: {error}", file=sys.stderr, flush=True)
            maya.close()
            return
        self.ready.add(number)
        while not self.stop.is_set():
            try:
                index = self.queue.get_nowait()
            except queue.Empty:
                return
            run = self.runs[index]
            start = time.monotonic()
            try:
                failures, output = run_scenario(slot.url, self.codes[index])
            except (OSError, transport.TransportError) as error:  # Maya crashed, or the scenario timed out
                if self.stop.is_set():  # the harness closed it: the summary is out already, the run is «not run»
                    return
                text = (
                    f"FAIL: Maya {maya.pid} lost — {type(error).__name__}: {error}; its windows "
                    f"{maya.window_titles()}; its logs {[str(log) for log in transport.server_logs(maya.pid)]}"
                )
                self.finish(index, name, Outcome(time.monotonic() - start, False, text, number + 1), "")
                maya.close()
                return
            outcome = Outcome(time.monotonic() - start, *verdict(run.expected, failures), number + 1)
            self.finish(index, name, outcome, output)

    def finish(self, index: int, name: str, outcome: Outcome, output: str) -> None:
        """The run's report in one piece and a line of progress."""
        label = label_of(self.runs[index])
        with self.lock:
            self.outcomes[index] = outcome
            print(f"=== {name} {label}", file=self.out, flush=True)
            if output:
                print(output, file=self.out)
            print(
                f"[{len(self.outcomes)}/{len(self.runs)}] {name} {label} {outcome.text} {outcome.seconds:.1f} s",
                file=self.out,
                flush=True,
            )


def run_in_pool(
    project: config.Config,
    runs: list[Run],
    codes: list[str],
    count: int,
    visible: bool,
    start_maya: Callable[[dict[str, str], bool, int], process.MayaProcess],
    module: Path,
    temp: Path,
    out: TextIO,
) -> Pool:
    """Starts `count` Mayas, runs `runs` in them and closes them. `start_maya(env, visible, number)`
    starts one."""
    roots: list[Path] = []
    slots: list[Slot] = []
    pool = Pool(runs, codes, slots, out)
    try:
        for number in range(1, count + 1):
            roots.append(session.make_session_dir(project.project, temp))
            port = session.free_port()
            env = session.prepare(roots[-1], project.params(), module, os.environ, port)
            maya = start_maya(env, visible, number)
            for log in transport.server_logs(maya.pid):  # Windows reuses pids: a stale log holds a stale address
                log.unlink(missing_ok=True)
            slots.append(Slot(roots[-1], maya, f"http://127.0.0.1:{port}/mcp"))
        pool.run()
    finally:
        for slot in slots:  # separate loops: a directory whose Maya failed to start has no Maya
            slot.maya.close()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
    failed = {outcome.maya for outcome in pool.outcomes.values() if not outcome.ok}
    for number, slot in enumerate(slots, start=1):  # the log of a Maya whose run failed stays
        if number not in failed and number - 1 in pool.ready:
            for log in transport.server_logs(slot.maya.pid):
                log.unlink(missing_ok=True)
    return pool
