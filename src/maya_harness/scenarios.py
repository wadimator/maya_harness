"""The check scenarios of `maya-harness run`: the table of runs, what to run, and whether a run passed.

* A scenario is a module `check_*.py` in `[tool.maya_harness.scenarios].dir`; its
  `run(**params)` returns a report whose `failures` are `<name>: <message>` (`inmaya.Check`).
  Each run imports it fresh (`inmaya.run_scenario`): the scenario modules and the project's
  `reload` packages are dropped from `sys.modules` first, so the code on disk is what runs.
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

Where the runs go — `pool`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from . import config, inmaya

PREFIX = inmaya.SCENARIO_PREFIX
RUN_KEYS = ("scenario", "control", "expect", "seconds")


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
    """Scenario names, or one scenario and its `name=value` parameters; `all` is the table, `scenarios` its
    runs without a control, `controls` its controls."""
    names = [item for item in items if "=" not in item]
    params = dict(item.split("=", 1) for item in items if "=" in item)
    # A control or a section among several names would replace every run of its scenario: `all section=film`
    # would make 20 runs of it.
    if params and (len(names) != 1 or names[0] in ("all", "scenarios", "controls")):
        raise Refused("A control or a section runs alone: <scenario> control=<value> | section=<name>.")
    if names == ["all"]:
        return list(table)
    if names == ["scenarios"]:
        return [run for run in table if not run.params]
    if names == ["controls"]:
        return [run for run in table if run.params]
    expected = {(run.scenario, run.params["control"]): run.expected for run in table if run.params}
    return [Run(name, params, expected.get((name, params.get("control", "")), "")) for name in names]


def refuse_unknown(runs: list[Run], scenarios: config.Scenarios) -> None:
    """A run of a scenario that is not there, or of a control the table does not list: a typo
    would run the plain scenario."""
    for run in runs:  # `all`, `scenarios` or `controls` among other names is caught here too: there is no all.py
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
    """Runs in Maya: `inmaya.run_scenario`, its last expression the report as JSON."""
    arguments = f"{str(directory)!r}, {run.scenario!r}, {run.params!r}, {reload!r}"
    return f"from maya_harness import inmaya as _maya_harness_inmaya\n_maya_harness_inmaya.run_scenario({arguments})\n"


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
