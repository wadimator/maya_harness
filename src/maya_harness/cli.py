"""`maya-harness`: start, stop, look at and run code in the isolated Mayas of this machine.

Exit codes: 0 — done; 1 — the code failed or a check did not pass; 2 — nothing was run: no
Maya, no dcc-mcp module, no session, Maya did not start in time. An `exec` with no reply (a
timeout, a reset, an unreadable reply) is 1: the code may have run; a refused connection is 2.
`--json` prints one JSON line on stdout and nothing else there.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import config, process, session, transport
from .session import Session, State

STARTUP_TIMEOUT = 180  # seconds; a cold start takes 12-15
CALL_TIMEOUT = 900  # seconds per `exec`
QUIT_TIMEOUT = 60  # seconds for a quitting Maya to exit
DEFAULT_TTL = 60  # minutes
PATCH_VARIABLE = "MAYA_HARNESS_DCC_MCP_PATCH"

OK, FAILED, NOT_RUN = 0, 1, 2


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

    stop = commands.add_parser("stop", help="quit a Maya; by default the latest one of this project")
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
    run.add_argument("--json", action="store_true")

    commands.add_parser("doctor", help="check Maya, the dcc-mcp module and its patch")
    return main


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # redirected to a file, they would write cp1251
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    args = parser().parse_args(argv)
    commands = {"start": start, "stop": stop, "status": status, "exec": run, "doctor": doctor}
    try:
        return commands[args.command](args)
    except (NotRun, config.ConfigError, transport.TransportError, TimeoutError, OSError) as error:
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
    maya_exe = session.maya_exe(os.environ)
    if not maya_exe.is_file():
        raise NotRun(f"no Maya at {maya_exe}; set MAYAPY_EXE or MAYA_LOCATION")
    module = session.module_file(os.environ)
    if not module.is_file():
        raise NotRun(f"no dcc-mcp-maya module at {module}; set {session.MODULE_VARIABLE} to its .mod file")
    if args.ttl < 0:
        raise NotRun("--ttl takes 0 or more minutes")

    root = session.make_session_dir(project.project, temp_dir())
    port = session.free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    work = root / "work"
    maya: process.MayaProcess | None = None
    kept = False
    try:
        env = session.prepare(root, project.params(), module, os.environ, port, args.ttl)
        maya = process.MayaProcess(maya_exe, env, args.visible)
        for log in transport.server_logs(maya.pid):
            log.unlink(missing_ok=True)
        state = State(project.project, str(project.root), os.getpid(), maya.pid, url, time.time(), args.ttl)
        session.write_state(root, state)
        try:
            transport.wait_for_server(url, work, maya.running, STARTUP_TIMEOUT)
            transport.connect(url, STARTUP_TIMEOUT, work)
        except (transport.TransportError, TimeoutError, OSError) as error:
            titles = maya.window_titles() if maya.running() else []
            logs = [str(log) for log in transport.server_logs(maya.pid)]
            raise NotRun(f"Maya {maya.pid} did not start: {error}; its windows {titles}; its logs {logs}") from error
        maya.release()
        kept = True
        pid = maya.pid
    finally:
        if not kept:
            if maya is not None:
                maya.close()
            shutil.rmtree(root, ignore_errors=True)
    print(f"Maya {pid} of {project.project} is running at {url}")
    return OK


def find_project_sessions(found: list[Session], root: Path) -> list[Session]:
    """The sessions started from the project at `root`, the latest last. By the project root, not
    the project name: a worktree has the same name, and its Maya has the other tree on sys.path."""
    return [item for item in found if Path(item.state.project_root) == root]


def select(url: str | None) -> tuple[str, Session | None]:
    """The address to call: `url`, or the latest live session of the project here."""
    found = session.sessions(temp_dir())
    if url:
        return url, next((item for item in found if item.state.url == url), None)
    root = config.load(Path.cwd()).root
    mine = find_project_sessions(found, root)
    if not mine:
        raise NotRun(f"no running Maya of the project at {root}; `maya-harness start` first")
    return mine[-1].state.url, mine[-1]


def reload_code(packages: Sequence[str]) -> str:
    """Drops `packages` and their submodules from `sys.modules`, so the code imports them from disk."""
    return (
        "import sys as _maya_harness_sys\n"
        "for _maya_harness_name in tuple(_maya_harness_sys.modules):\n"
        f"    if _maya_harness_name.split('.')[0] in {tuple(packages)!r}:\n"
        "        del _maya_harness_sys.modules[_maya_harness_name]\n"
    )


def touch(work: Path) -> None:
    """Marks a call: the idle timer of the session counts from the mtime of `last_call`."""
    (work / "last_call").touch()


def run(args: argparse.Namespace) -> int:
    code = args.code if args.code is not None else args.file.read_text(encoding="utf-8")
    url, target = select(args.url)
    packages = config.load(Path.cwd()).reload if args.reload else ()
    work = transport.connect(url, CALL_TIMEOUT, target.work if target else None)
    touch(work)
    try:
        # A call of its own, not a prefix of the code: `from __future__` must open the code, and
        # the line numbers of its traceback must be the file's.
        result = transport.execute(url, reload_code(packages), CALL_TIMEOUT) if packages else transport.Result(True)
        if result.success:
            result = transport.execute(url, code, CALL_TIMEOUT)
    except OSError as error:  # a timeout, a reset: the code reached Maya and may have run
        result = transport.Result(False, type(error).__name__, stderr=f"no reply from {url}: {error}")
    finally:
        touch(work)
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


def stop(args: argparse.Namespace) -> int:
    found = session.sessions(temp_dir())
    if args.all_mine:
        targets = found
    elif args.project:
        targets = find_project_sessions(found, config.load(Path.cwd()).root)
    elif args.url:
        targets = [item for item in found if item.state.url == args.url]
        if not targets:
            raise NotRun(f"no Maya of maya_harness at {args.url}")
    else:
        targets = find_project_sessions(found, config.load(Path.cwd()).root)[-1:]
    if not targets:
        print("no running Maya to stop")
        return OK
    unreachable = []
    for item in targets:
        try:
            transport.connect(item.state.url, QUIT_TIMEOUT, item.work)
            transport.execute(item.state.url, transport.QUIT_CODE, QUIT_TIMEOUT)
        except (transport.TransportError, OSError) as error:  # a modal dialog, a Maya gone
            print(f"Maya {item.state.maya_pid} at {item.state.url} did not take quit: {error}", file=sys.stderr)
            unreachable.append(item)
    deadline = time.monotonic() + QUIT_TIMEOUT
    left = [item for item in targets if item not in unreachable]
    while left and time.monotonic() < deadline:
        left = [item for item in left if process.process_alive(item.state.maya_pid)]
        time.sleep(0.25)
    running = left + unreachable
    for item in targets:
        print(f"Maya {item.state.maya_pid} at {item.state.url}: {'still running' if item in running else 'closed'}")
    return FAILED if running else OK


def describe(item: Session, now: float) -> dict[str, Any]:
    last_call = item.work / "last_call"
    try:
        idle_since = last_call.stat().st_mtime
    except OSError:
        idle_since = item.state.started
    return {
        "project": item.state.project,
        "project_root": item.state.project_root,
        "pid": item.state.maya_pid,
        "url": item.state.url,
        "age": round(now - item.state.started),
        "idle": round(now - idle_since),
        "ttl": item.state.ttl,
        "dir": str(item.root),
    }


def status(args: argparse.Namespace) -> int:
    now = time.time()
    rows = [describe(item, now) for item in session.sessions(temp_dir())]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return OK
    if not rows:
        print("no running Maya of maya_harness")
        return OK
    for row in rows:
        print(
            f"{row['project']:<20} pid {row['pid']:<6} {row['url']:<28} "
            f"age {row['age'] // 60} min, idle {row['idle'] // 60} min, ttl {row['ttl']} min  {row['project_root']}"
        )
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
