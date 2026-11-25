"""Where the runs of `maya-harness run` go: a pool of Mayas of their own, or one at `--url`.

* The pool: up to `--jobs` worker threads; each, while runs are left, starts a Maya of its own (a
  session in the job of this process), waits for it and takes the next run from one queue. A
  Maya leaves as soon as the queue is empty, or when it crashes or times out: it fails its run,
  and the others take the rest. Ctrl+C lets no Maya take another run. A Maya that leaves is
  killed and its session directory removed at once, any left at the end too; a session is
  never released and writes no `state.json`, so `exec` and `stop` never pick a Maya of a run.
* Each Maya is started and waited for in its own worker, not all started first and closed at
  the end: a worker that cannot start its Maya yet (every slot of the machine is taken, see
  `slots`) holds up no other, and a Maya with nothing left to run holds nothing up to the end of
  the run. A worker that waits for a slot gives up once no run is left or the run is stopped.
* Mayas at once: on 8 cores / 16 threads a Maya of a scenario takes ~1.2 cores: 6 Mayas run
  `all` in 494 s against 791 s for 3, 8 Mayas in 475 s (2026-11).
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from . import call, config, session, transport
from .scenarios import Outcome, Run, label_of, print_summary, verdict

JOBS = 6
MAX_JOBS = 6
TIMEOUT = 900  # seconds per scenario: a run may reopen scenes with heavy rigs


def run_scenario(execute: Callable[[str, float], transport.Result], code: str) -> tuple[list[str] | None, str]:
    """The scenario's failures, None when it raised or returned no report with a list of failures, and its
    output: the caller prints it in one piece, and the output of runs in other Mayas does not get between its
    lines. `execute` is the call of a Maya: one that stands on a modal dialog ends in `CallAborted`."""
    result = execute(code, TIMEOUT)
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


def run_at(target: call.Target, runs: list[Run], codes: list[str], out: TextIO) -> dict[int, Outcome]:
    """The runs one after another in the Maya `target`, a session `start` left running; the summary is printed
    even when a run is cut short. A call Maya does not answer fails its run, and the runs after it are «not run»."""
    outcomes: dict[int, Outcome] = {}
    try:
        for index, (run, code) in enumerate(zip(runs, codes, strict=True)):
            print(f"=== {label_of(run)}", file=out, flush=True)
            start = time.monotonic()
            try:
                failures, output = run_scenario(target.execute, code)
            # Maya is gone, busy or on a dialog: the runs after it wait
            except (OSError, transport.TransportError) as error:
                text = f"FAIL: no reply from {target.url} — {type(error).__name__}: {error}"
                outcomes[index] = Outcome(time.monotonic() - start, False, text, 0)
                break
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
                started.wait()
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
                failures, output = run_scenario(started.execute, self.codes[index])
            # Maya crashed, the scenario timed out, or Maya stands on a dialog nobody on its hidden desktop
            # answers: the `finally` of `work` kills it
            except (OSError, transport.TransportError) as error:
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
