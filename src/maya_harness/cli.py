"""`maya-harness`: start, stop, look at and run code in the isolated Mayas of this machine.

Exit codes: 0 — done; 1 — the code failed or a check did not pass; 2 — nothing was run: no
Maya, no dcc-mcp module, no session, Maya did not start in time. An `exec` with no reply (a
timeout, a reset, an unreadable reply) is 1: the code may have run; a refused connection is 2.
A call cut short because Maya shows a modal dialog is 1 once the code went to Maya, and 2 when
the connection (its handshake and marker) was cut short.
`--json` prints one JSON line on stdout and nothing else there.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import call, config, pool, process, registry, scenarios, session, slots, transport
from .registry import Record

CALL_TIMEOUT = 900  # seconds per `exec`, by default
MAX_CALL_TIMEOUT = 86400  # a day: a longer wait is a typo, and past 2147483 s the socket overflows
HEARTBEAT = 30  # seconds between the signs of life of an `exec` that waits for Maya
DEFAULT_TTL = 60  # minutes
PATCH_VARIABLE = "MAYA_HARNESS_DCC_MCP_PATCH"

OK, FAILED, NOT_RUN = 0, 1, 2


def seconds(text: str) -> float:
    value = float(text)
    if not value >= 0:  # nan too: a wait of nan seconds never ends
        raise argparse.ArgumentTypeError(f"{text}: takes 0 or more seconds")
    return value


def call_seconds(text: str) -> float:
    """The time of a call: a socket with timeout 0 is non-blocking, and connect fails with
    WinError 10035; above 2147483 s (INT_MAX ms) `settimeout` raises OverflowError."""
    value = float(text)
    if not 0 < value <= MAX_CALL_TIMEOUT:
        raise argparse.ArgumentTypeError(f"{text}: takes more than 0 and at most {MAX_CALL_TIMEOUT} seconds")
    return value


# Not --timeout: this is the wait for a slot, not the time of a call.
SLOT_WAIT: dict[str, Any] = {
    "type": seconds,
    "metavar": "SEC",
    "help": "wait this long for a free Maya slot of this machine; without a limit by default",
}


class NotRun(Exception):
    """Nothing was run: exit code 2."""


def temp_dir() -> Path:
    return Path(tempfile.gettempdir())


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="maya-harness", description=__doc__.splitlines()[0])
    commands = main.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a Maya for this project and leave it running")
    start.add_argument("--visible", action="store_true", help="on the usual desktop, to look at it")
    start.add_argument(
        "--ttl",
        type=int,
        default=DEFAULT_TTL,
        metavar="MIN",
        help=f"Maya quits after this many idle minutes, {DEFAULT_TTL} by default; 0 for never",
    )
    start.add_argument("--slot-wait", **SLOT_WAIT)

    stop = commands.add_parser("stop", help="kill a Maya; by default the latest one of this project")
    which = stop.add_mutually_exclusive_group()
    which.add_argument("url", nargs="?", help="the Maya at this address")
    which.add_argument("--project", action="store_true", help="every Maya of this project")
    which.add_argument("--all-mine", action="store_true", help="every Maya of maya_harness of this user")

    status = commands.add_parser("status", help="the running Mayas of maya_harness")
    status.add_argument("--json", action="store_true")

    run = commands.add_parser("exec", help="run code in a Maya; by default the latest one of this project")
    run.add_argument("url", nargs="?", help="the Maya at this address")
    code = run.add_mutually_exclusive_group(required=True)
    code.add_argument(
        "-c", dest="code", metavar="CODE", help="the code; its last expression, if it ends in one, is printed"
    )
    code.add_argument("-f", dest="file", type=Path, metavar="FILE", help="the code from a file")
    run.add_argument("--reload", action="store_true", help="drop the project's packages from sys.modules first")
    run.add_argument(
        "--timeout",
        type=call_seconds,
        default=CALL_TIMEOUT,
        metavar="SEC",
        help=f"give up waiting for the reply after this long, {CALL_TIMEOUT} by default",
    )
    run.add_argument("--json", action="store_true")

    checks = commands.add_parser(
        "run", help="run check scenarios in Mayas of their own", epilog="See docs/scenarios.md."
    )
    checks.add_argument(
        "items", nargs="+", help="scenario names, all, scenarios or controls, and name=value parameters"
    )
    checks.add_argument(
        "--jobs",
        type=int,
        default=pool.JOBS,
        help=f"Mayas at once, {pool.JOBS} by default; one with --visible",
    )
    checks.add_argument("--url", help="run in this Maya, which `start` left running")
    checks.add_argument("--visible", action="store_true", help="start the Maya on the usual desktop")
    checks.add_argument("--slot-wait", **SLOT_WAIT)
    checks.add_argument("--json", action="store_true", help="the summary as one JSON line; the reports go to stderr")

    commands.add_parser("doctor", help="check Maya, the dcc-mcp module and its patch")
    return main


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # redirected to a file, they would write cp1251
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = parser().parse_args(argv)
    commands = {"start": start, "stop": stop, "status": status, "exec": run, "run": run_scenarios, "doctor": doctor}
    try:
        return commands[args.command](args)
    except (
        NotRun,
        session.StartError,
        slots.NoSlot,
        config.ConfigError,
        scenarios.Refused,
        transport.TransportError,
        TimeoutError,
        OSError,
    ) as error:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": str(error)}, ensure_ascii=False))
        else:
            print(f"maya-harness: {error}", file=sys.stderr)
        return NOT_RUN
    except KeyboardInterrupt:
        print("maya-harness: interrupted", file=sys.stderr)
        return 130


def start(args: argparse.Namespace) -> int:
    project = config.load(Path.cwd())
    if args.ttl < 0:
        raise NotRun("--ttl takes 0 or more minutes")
    params = project.params()
    with session.start(
        params, visible=args.visible, ttl=args.ttl, command="start", temp=temp_dir(), slot_wait=args.slot_wait
    ) as running:
        running.wait()
        running.release()
    print(f"Maya {running.pid} of {project.project} is running at {running.url}")
    return OK


def select(url: str | None) -> tuple[str, Record | None]:
    """The address to call: `url`, or the latest live session of the project here. The project's
    config is read only without `url`: `exec <url>` runs from any directory."""
    found = registry.running(temp_dir())
    root = None if url else config.load(Path.cwd()).root
    record = registry.select(found, url, root)
    if url:
        return url, record
    if record is None:
        raise NotRun(f"no running Maya of the project at {root}; `maya-harness start` first")
    return record.state.url, record


def reload_code(packages: Sequence[str]) -> str:
    """Drops `packages` and their submodules from `sys.modules`, so the code imports them from disk."""
    return (
        f"from maya_harness import inmaya as _maya_harness_inmaya\n_maya_harness_inmaya.reload({tuple(packages)!r})\n"
    )


def heartbeat(pid: int, watch: transport.Watch) -> transport.Watch:
    """`watch`, and every `HEARTBEAT` seconds a line on stderr: a long call is not taken for a hung
    one. stderr with `--json` too: stdout stays one JSON line."""
    begin = time.monotonic()
    beaten = [begin]

    def beat() -> None:
        watch()
        now = time.monotonic()
        if now - beaten[0] >= HEARTBEAT:
            beaten[0] = now
            titles = ", ".join(process.window_titles(pid))
            print(f"waiting for Maya {pid}: {round(now - begin)} s, windows [{titles}]", file=sys.stderr, flush=True)

    return beat


def run(args: argparse.Namespace) -> int:
    code = args.code if args.code is not None else args.file.read_text(encoding="utf-8")
    url, record = select(args.url)
    packages = config.load(Path.cwd()).reload if args.reload else ()
    # Only a Maya of a session: the pid of a Maya at a foreign address is unknown.
    pid = record.state.maya_pid if record else None
    watch = heartbeat(pid, call.watcher(pid, url)) if pid is not None else None
    # A connect cut short on a dialog is a TransportError out of `main`: the code was not sent.
    target = call.connect(url, args.timeout, work=record.work if record else None, pid=pid, watch=watch)
    try:
        # A call of its own, not a prefix of the code: `from __future__` must open the code, and
        # the line numbers of its traceback must be the file's.
        result = target.execute(reload_code(packages), args.timeout, watch) if packages else transport.Result(True)
        if result.success:
            result = target.execute(code, args.timeout, watch)
    except OSError as error:  # a timeout, a reset: the code reached Maya and may have run
        result = transport.Result(False, type(error).__name__, stderr=f"no reply from {url}: {error}")
    except transport.CallAborted as error:  # the code reached Maya and runs on after the dialog
        result = transport.Result(False, type(error).__name__, stderr=str(error))
    if args.json:
        fields = ("success", "error", "stdout", "stderr", "output")
        print(json.dumps({name: getattr(result, name) for name in fields} | {"url": url}, ensure_ascii=False))
    else:
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
        # code that raised: its traceback is the error
        if not result.success and not (result.stderr and result.error == "RuntimeError"):
            print(result.error or json.dumps(result.raw, ensure_ascii=False), file=sys.stderr)
        elif result.output:
            print(result.output)
    return OK if result.success else FAILED


def run_scenarios(args: argparse.Namespace) -> int:
    project = config.load(Path.cwd())
    if project.scenarios is None:
        raise NotRun(f"no [tool.maya_harness.scenarios] in the pyproject.toml of {project.root}")
    runs = scenarios.parse(args.items, scenarios.load_runs(project.scenarios.runs))
    scenarios.refuse_unknown(runs, project.scenarios)
    if args.jobs < 1:
        raise NotRun("--jobs takes 1 or more")
    if args.jobs > pool.MAX_JOBS:
        print(
            f"--jobs {args.jobs}: more than {pool.MAX_JOBS} Mayas gain little on 8 cores and take ~1.8 GB each.",
            file=sys.stderr,
        )
    codes = [scenarios.code_of(item, project.scenarios.dir, project.reload) for item in runs]
    out = sys.stderr if args.json else sys.stdout
    if args.url:
        _, record = select(args.url)
        target = call.connect(args.url, CALL_TIMEOUT, pid=record.state.maya_pid if record else None)
        outcomes = pool.run_at(target, runs, codes, out)
    else:
        count = 1 if args.visible else max(1, min(args.jobs, len(runs)))
        ran = pool.run_in_pool(project, runs, codes, count, args.visible, temp_dir(), out, args.slot_wait)
        if not ran.ready:
            raise NotRun("no Maya started; see the errors above")
        outcomes = ran.outcomes
    if args.json:
        print(json.dumps(scenarios.summary(runs, outcomes), ensure_ascii=False))
    return OK if scenarios.passed(runs, outcomes) else FAILED


def stop(args: argparse.Namespace) -> int:
    found = registry.running(temp_dir())
    if args.all_mine:
        targets = found
    elif args.project:
        targets = registry.of_project(found, config.load(Path.cwd()).root)
    elif args.url:
        targets = [item for item in found if item.state.url == args.url]
        if not targets:
            raise NotRun(f"no Maya of maya_harness at {args.url}")
    else:
        targets = registry.of_project(found, config.load(Path.cwd()).root)[-1:]
    if not targets:
        print("no running Maya to stop")
        return OK
    # A kill, not a `quit`: a busy Maya takes a quit only after its current call, one on a modal
    # dialog never; the session is thrown away, so a quit has nothing to save. What Maya shows
    # is printed first: a dialog on the hidden desktop is seen only this way.
    running = False
    now = time.time()
    for item in targets:
        pid = item.state.maya_pid
        row = registry.describe(item, now)
        print(f"Maya {pid} {item.state.url} {item.state.project}: {activity(row)}", flush=True)
        if registry.stop(item):
            print(f"Maya {pid}: killed")
        else:  # the directory stays: its logs tell why
            running = True
            print(f"Maya {pid}: still running")
    return FAILED if running else OK


def activity(row: dict[str, Any]) -> str:
    """`busy` while a call runs, else `idle`, and Maya's windows."""
    doing = f"idle {row['idle'] // 60} min" if row["busy"] is None else f"busy {row['busy'] // 60} min"
    return f"{doing}, windows [{', '.join(row['windows'])}]"


def status(args: argparse.Namespace) -> int:
    now = time.time()
    rows = [registry.describe(item, now) for item in registry.running(temp_dir())]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return OK
    if not rows:
        print("no running Maya of maya_harness")
    for row in rows:
        print(
            f"{row['project']:<20} pid {row['pid']:<6} {row['url']:<28} "
            f"age {row['age'] // 60} min, ttl {row['ttl']} min  {row['project_root']}  {activity(row)}"
        )
    taken = slots.holders()
    print(f"Maya slots: {len(taken)} of {slots.count()} taken")
    for holder in taken:
        print(f"  {holder.line(now)}")
    return OK


def patch_applied(module_dir: Path, patch: Path) -> bool:
    """`git apply -R --check`: the patch is on the module. GIT_CEILING_DIRECTORIES: inside another
    repository git apply skips the module's files and still exits 0."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(module_dir.parent))
    command = ["git", "apply", "-R", "--check", "-p1", str(patch)]
    return subprocess.run(command, cwd=module_dir, env=env, capture_output=True, check=False).returncode == 0


def doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[bool | None, str, str]] = []
    maya_exe = session.maya_exe(os.environ)
    checks.append((maya_exe.is_file(), "maya", str(maya_exe)))
    module_file = session.module_file(os.environ)
    module: session.Module | None = None
    if not module_file.is_file():
        checks.append((False, "dcc-mcp .mod", f"{module_file} not found; set {session.MODULE_VARIABLE}"))
    else:
        try:
            module = session.read_module(module_file)
        except (OSError, ValueError) as error:
            checks.append((False, "dcc-mcp .mod", str(error)))
    if module is not None:
        checks.append((True, "dcc-mcp .mod", f"{module_file}, {module.name} {module.version}"))
        for path in (module.directory, *module.paths):
            checks.append((path.is_dir(), "module path", str(path)))
        clone = os.environ.get(PATCH_VARIABLE)
        patches = sorted((Path(clone) / "patches").glob("*.diff")) if clone else []
        if not clone:
            checks.append((None, "dcc-mcp patch", f"not checked: {PATCH_VARIABLE} is not set"))
        elif not patches:
            checks.append((False, "dcc-mcp patch", f"no patches/*.diff in {clone}"))
        for patch in patches:
            applied = patch_applied(module.directory, patch)
            checks.append((applied, "dcc-mcp patch", f"{patch.name} {'applied' if applied else 'NOT applied'}"))
    for passed, name, detail in checks:
        label = "SKIP" if passed is None else "OK" if passed else "FAIL"
        print(f"{label:<5} {name:<14} {detail}")
    return OK if all(passed is not False for passed, _, _ in checks) else FAILED
