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

* The pool: up to `--jobs` worker threads; each, while runs are left, starts a Maya of its own (a
  session in the job of this process), waits for it and takes the next run from one queue. A
  Maya leaves as soon as the queue is empty, or when it crashes or times out: it fails its run,
  and the others take the rest. Ctrl+C lets no Maya take another run. A Maya that leaves is
  killed and its session directory removed at once, any left at the end too; a session writes no
  `state.json`, so `exec` and `stop` never pick a Maya of a run.
* Each Maya is started and waited for in its own worker, not all started first and closed at
  the end: a worker that cannot start its Maya yet (every slot of the machine is taken, see
  `slots`) holds up no other, and a Maya with nothing left to run holds nothing up to the end of
  the run. A worker that waits for a slot gives up once no run is left or the run is stopped.
* Mayas at once: on 8 cores / 16 threads 3 Mayas slow a scenario by 14%, 4 by 19%, 7 by 16-60%
  (Retarget_Master, 2026).
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from . import config, session, transport

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


class Pool:
    """`count` worker threads; each starts a Maya of its own while runs are left, waits for it and takes
    runs from one queue in their order.

    A Maya leaves as soon as the queue is empty, or when it crashes or times out: it fails its run, and the
    others take the rest of the queue. A stop (Ctrl+C) lets no worker take another run."""

    def __init__(
        self,
        runs: list[Run],
        codes: list[str],
        count: int,
        params: session.SessionParams,
        visible: bool,
        temp: Path,
        out: TextIO,
        slot_wait: float | None = None,
    ) -> None:
        self.runs = runs
        self.codes = codes
        self.count = count
        self.params = params
        self.visible = visible
        self.temp = temp
        self.out = out
        self.slot_wait = slot_wait
        self.queue: queue.Queue[int] = queue.Queue()
        for index in range(len(runs)):
            self.queue.put(index)
        self.outcomes: dict[int, Outcome] = {}
        self.stop = threading.Event()
        self.lock = threading.Lock()
        # By the number of the worker: the workers start their Mayas in any order.
        self.sessions: dict[int, session.Session] = {}
        self.ready: set[int] = set()  # the numbers of the workers whose Maya started and connected

    def run(self) -> bool:
        workers = [threading.Thread(target=self.work, args=(number,), daemon=True) for number in range(self.count)]
        for worker in workers:
            worker.start()
        try:
            # join with a timeout: a plain join holds Ctrl+C until the worker ends.
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    worker.join(0.2)
        except KeyboardInterrupt:
            self.stop.set()
            with self.lock:
                for started in self.sessions.values():
                    titles = started.maya.window_titles()
                    print(f"KeyboardInterrupt: the windows of Maya {started.pid}: {titles}", file=sys.stderr)
            raise
        finally:
            with self.lock:
                result = print_summary(self.runs, self.outcomes, self.out)
        return result

    def work(self, number: int) -> None:
        name = f"maya{number + 1}"
        if self.queue.empty():
            return
        try:
            started = session.start(
                self.params, visible=self.visible, temp=self.temp, slot_wait=self.slot_wait, slot_unneeded=self.done
            )
        except (session.StartError, OSError) as error:
            if not self.done():
                self.say(f"{name}: {error}", sys.stderr)
            return
        with self.lock:
            self.sessions[number] = started
        try:
            self.say(f"{name} pid {started.pid}: started", self.out)
            # The queue may have emptied, or the run been stopped, while this Maya started: no wait
            # for a Maya with nothing to run, nor for one the harness is past closing.
            if self.done():
                return
            begin = time.monotonic()
            try:
                started.wait(transport.STARTUP_TIMEOUT)
            except session.StartError as error:
                if not self.stop.is_set():
                    self.say(f"{name}: {error}", sys.stderr)
                return
            with self.lock:
                self.ready.add(number)
            self.say(f"{name}: ready in {time.monotonic() - begin:.0f} s", self.out)
            self.take_runs(number, name, started)
        finally:
            started.close()  # at once: a Maya that leaves holds nothing up to the end of the run

    def take_runs(self, number: int, name: str, started: session.Session) -> None:
        while not self.stop.is_set():
            try:
                index = self.queue.get_nowait()
            except queue.Empty:
                return
            run = self.runs[index]
            self.say(f"{name}: {label_of(run)}", self.out)
            start = time.monotonic()
            try:
                failures, output = run_scenario(started.url, self.codes[index])
            except (OSError, transport.TransportError) as error:  # Maya crashed, or the scenario timed out
                if self.stop.is_set():  # the harness closed it: the summary is out already, the run is «not run»
                    return
                logs = [str(log) for log in transport.server_logs(started.pid)]
                text = (
                    f"FAIL: Maya {started.pid} lost — {type(error).__name__}: {error}; its windows "
                    f"{started.maya.window_titles()}; its logs {logs}"
                )
                self.finish(index, name, Outcome(time.monotonic() - start, False, text, number + 1), "")
                return
            outcome = Outcome(time.monotonic() - start, *verdict(run.expected, failures), number + 1)
            self.finish(index, name, outcome, output)

    def done(self) -> bool:
        """No run is left to take, or the run was stopped: a Maya would have nothing to do."""
        return self.queue.empty() or self.stop.is_set()

    def say(self, line: str, out: TextIO) -> None:
        """A line of progress; under the lock, so that it does not get into a report of another worker."""
        with self.lock:
            print(line, file=out, flush=True)

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
    temp: Path,
    out: TextIO,
    slot_wait: float | None = None,
) -> Pool:
    """Runs `runs` in up to `count` Mayas; each is closed when it leaves, any left at the end too."""
    pool = Pool(runs, codes, count, project.params(), visible, temp, out, slot_wait)
    try:
        pool.run()
    finally:  # Ctrl+C, an error: the workers are daemons and may still hold their Mayas
        with pool.lock:
            left = list(pool.sessions.values())
        for started in left:
            started.close()
    failed = {outcome.maya for outcome in pool.outcomes.values() if not outcome.ok}
    for number, started in pool.sessions.items():  # the log of a Maya whose run failed stays
        if number + 1 not in failed and number in pool.ready:
            for log in transport.server_logs(started.pid):
                log.unlink(missing_ok=True)
    return pool
