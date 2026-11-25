"""What `run` runs and whether a run passed; the Mayas themselves are checked by running it."""

import io
import itertools
import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from maya_harness import cli, config, session, slots
from maya_harness import run as scenario_run
from maya_harness.run import Run

TABLE = """
[[run]]
scenario = "check_fps"
seconds = 6

[[run]]
scenario = "check_bake"
seconds = 34

[[run]]
scenario = "check_bake"
control = "no_round"
expect = "film.whole_frames"
seconds = 8
"""
ALL = [
    Run("check_bake", {}, ""),
    Run("check_bake", {"control": "no_round"}, "film.whole_frames"),
    Run("check_fps", {}, ""),
]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with two scenarios and its table of runs; the current directory, its sessions in `temp`."""
    root = tmp_path / "plugin"
    checks = root / "checks"
    checks.mkdir(parents=True)
    for name in ("check_fps", "check_bake"):
        (checks / f"{name}.py").write_text("def run(**params): ...\n", encoding="utf-8")
    (checks / "runs.toml").write_text(TABLE, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.maya_harness]\nreload = ["plugin"]\n'
        '[tool.maya_harness.scenarios]\ndir = "checks"\nruns = "checks/runs.toml"\n',
        encoding="utf-8",
    )
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(cli, "temp_dir", lambda: tmp_path / "temp")
    monkeypatch.chdir(root)
    return root


def scenarios(root: Path) -> config.Scenarios:
    found = config.load(root).scenarios
    assert found is not None
    return found


def test_load_runs_takes_the_longest_first(project: Path) -> None:
    assert scenario_run.load_runs(scenarios(project).runs) == ALL


@pytest.mark.parametrize(
    ("entry", "refusal"),
    [
        ('scenario = "fps"', "its scenario check_<name>"),
        ('scenario = "check_fps"\nsecond = 6', "a run is scenario, control, expect, seconds"),
        ('scenario = "check_fps"\ncontrol = "x"', "a control and its expect go together"),
        ('scenario = "check_fps"\nexpect = "x"', "a control and its expect go together"),
        ('scenario = "check_fps"\nseconds = "6"', "seconds must be a number"),
        ('scenario = "check_bake"\ncontrol = "no_round"\nexpect = "x"', "listed twice"),
    ],
)
def test_load_runs_refuses_a_wrong_entry(tmp_path: Path, entry: str, refusal: str) -> None:
    table = tmp_path / "runs.toml"
    table.write_text(f"{TABLE}\n[[run]]\n{entry}\n", encoding="utf-8")
    with pytest.raises(scenario_run.Refused, match=refusal):
        scenario_run.load_runs(table)


@pytest.mark.parametrize(
    ("expected", "failures", "result"),
    [
        ("", [], (True, "ok")),
        ("", ["load.scan: [] != ['a']", "load.raised: boom"], (False, "FAIL: 2 failures")),
        (
            "film.whole_frames",
            ["film.whole_frames: [10.4] != []"],
            (True, "ok: the control failed on film.whole_frames"),
        ),
        ("film.whole_frames", [], (False, "FAIL: the control passed")),
        (
            "film.whole_frames",
            ["film.keys: (10, 147) != (11, 147)", "film.whole_frames: [10.4] != []"],
            (False, "FAIL: the control failed on film.keys, expected film.whole_frames"),
        ),
        (
            "fingers.gloves",
            ["fingers.gloves_rad: False != True"],
            (False, "FAIL: the control failed on fingers.gloves_rad, expected fingers.gloves"),
        ),
        ("", None, (False, "FAIL: raised")),
        ("film.whole_frames", None, (False, "FAIL: raised")),
    ],
)
def test_verdict(expected: str, failures: list[str] | None, result: tuple[bool, str]) -> None:
    assert scenario_run.verdict(expected, failures) == result


@pytest.mark.parametrize(
    ("argv", "runs"),
    [
        (["check_fps"], [Run("check_fps", {}, "")]),
        (["check_fps", "check_bake"], [Run("check_fps", {}, ""), Run("check_bake", {}, "")]),
        (["check_bake", "section=film"], [Run("check_bake", {"section": "film"}, "")]),
        (["check_bake", "control=no_round"], [Run("check_bake", {"control": "no_round"}, "film.whole_frames")]),
        (["check_bake", "control=nope"], [Run("check_bake", {"control": "nope"}, "")]),
        (["all"], ALL),
    ],
)
def test_parse(argv: list[str], runs: list[Run]) -> None:
    assert scenario_run.parse(argv, ALL) == runs


@pytest.mark.parametrize(
    "argv",
    [
        ["all", "section=film"],
        ["all", "control=no_round"],
        ["check_bake", "check_fps", "section=film"],
        ["check_bake", "check_fps", "control=no_round"],
    ],
)
def test_parse_refuses_parameters_of_several_runs(argv: list[str]) -> None:
    with pytest.raises(scenario_run.Refused, match="A control or a section runs alone"):
        scenario_run.parse(argv, ALL)


def test_code_drops_the_scenarios_and_the_reload_packages(project: Path) -> None:
    code = scenario_run.code_of(Run("check_bake", {"control": "no_round"}, "x"), project / "checks", ("plugin",))
    compile(code, "run", "exec")
    assert repr(str(project / "checks")) in code
    assert "in ('plugin',) or _name.startswith('check_')" in code
    assert code.rstrip().endswith(
        "json.dumps(check_bake.run(**{'control': 'no_round'}), ensure_ascii=False, default=str)"
    )


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (["run", "check_bake", "control=no_rnd"], "check_bake has no control {'control': 'no_rnd'}"),
        (["run", "check_bake", "film=1"], "check_bake has no control {'film': '1'}"),
        (["run", "check_fps", "all"], "no scenario 'all'"),
        (["run", "check_nope"], "no scenario 'check_nope'"),
        (["run", "check_fps", "--jobs", "0"], "--jobs takes 1 or more"),
    ],
)
def test_run_refuses_before_maya_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], project: Path, argv: list[str], refusal: str
) -> None:
    monkeypatch.setattr(session, "prepare", lambda *_, **__: pytest.fail("Maya started"))
    # The text too: code 2 alone passes on a refusal for another reason.
    assert (cli.main(argv), refusal in capsys.readouterr().err) == (cli.NOT_RUN, True)


def test_run_without_scenario_settings_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.maya_harness]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "all"]) == cli.NOT_RUN
    assert "no [tool.maya_harness.scenarios]" in capsys.readouterr().err


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `maya.exe` and a dcc-mcp `.mod` for `session.start` to find."""
    (tmp_path / "maya.exe").touch()
    (tmp_path / "x.mod").write_text("+ MAYAVERSION:2025 PLATFORM:win64 dcc_mcp_maya 1 C:/m\n", encoding="utf-8")
    monkeypatch.setenv("MAYAPY_EXE", str(tmp_path / "mayapy.exe"))
    monkeypatch.setenv(session.MODULE_VARIABLE, str(tmp_path / "x.mod"))
    monkeypatch.setattr(session.process, "give", lambda *_: None)  # a fake Maya has no process to hold a slot


def test_run_removes_the_session_of_a_maya_that_failed_to_start(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path, installed: None
) -> None:
    def fail_to_start(*_: object, **__: object) -> None:
        raise OSError("no maya.exe")

    monkeypatch.setattr(session.process, "MayaProcess", fail_to_start)
    assert cli.main(["run", "check_fps"]) == cli.NOT_RUN
    assert list((tmp_path / "temp").iterdir()) == []


def test_the_pool_starts_each_maya_on_a_desktop_of_its_own(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path, installed: None
) -> None:
    desktops: set[int] = set()

    class Maya:
        def __init__(self, exe: Path, env: dict[str, str], visible: bool, number: int) -> None:
            self.pid = number
            self.handle = self.created = 0
            desktops.add(number)

        def running(self) -> bool:
            return False

        def close(self) -> None: ...

        def window_titles(self) -> list[str]:
            return []

    def wait_for_server(*_: object) -> None:
        raise TimeoutError("no server")

    monkeypatch.setattr(session.process, "MayaProcess", Maya)
    monkeypatch.setattr(session.transport, "wait_for_server", wait_for_server)
    monkeypatch.setattr(session.transport, "server_logs", lambda pid: [])  # the fake pids are not its Mayas
    assert cli.main(["run", "all", "--jobs", "3"]) == cli.NOT_RUN
    assert len(desktops) == 3
    assert list((tmp_path / "temp").iterdir()) == []


class FakeSession:
    """A session of the pool without Maya: what the pool did with it."""

    def __init__(self, pid: int, closed: threading.Event) -> None:
        self.pid = pid
        self.url = f"http://127.0.0.1:{pid}/mcp"
        self.waited = False
        self.closed = False
        self.any_closed = closed

    def watch(self) -> None:
        pass

    def wait(self, timeout: float) -> None:
        self.waited = True

    def close(self) -> None:
        self.closed = True
        self.any_closed.set()


def fake_pool(
    monkeypatch: pytest.MonkeyPatch, runs: int, count: int, start: Callable[[], None] = lambda: None
) -> tuple[scenario_run.Pool, list[FakeSession], io.StringIO]:
    """A pool of `count` workers and `runs` runs; `start` runs inside each `session.start`."""
    out = io.StringIO()
    pool = scenario_run.Pool(
        [Run(f"check_{index}", {}, "") for index in range(runs)],
        [""] * runs,
        count,
        session.SessionParams("p", Path("C:/p")),
        False,
        Path("C:/temp"),
        out,
    )
    started: list[FakeSession] = []
    closed = threading.Event()

    def fake_start(*_: object, **__: object) -> FakeSession:
        start()
        with pool.lock:
            started.append(FakeSession(len(started) + 1, closed))
            return started[-1]

    monkeypatch.setattr(scenario_run.session, "start", fake_start)
    return pool, started, out


def test_a_maya_with_nothing_left_to_run_leaves_before_the_others_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run of one Maya ends only once the other Maya, with nothing left to run, is closed."""
    pool, started, out = fake_pool(monkeypatch, runs=2, count=2)
    calls: list[bool] = []

    def run_scenario(url: str, code: str, watch: object = None) -> tuple[list[str], str]:
        assert watch is not None  # a Maya of the pool on a dialog is cut short too
        with pool.lock:
            first = not calls
            calls.append(True)
        if first:
            calls.append(started[0].any_closed.wait(5))
        return [], ""

    monkeypatch.setattr(scenario_run, "run_scenario", run_scenario)
    assert pool.run()
    assert calls == [True, True, True]  # the first run saw a Maya closed before it ended
    assert all(item.waited and item.closed for item in started)
    text = out.getvalue()
    assert "maya1 pid" in text and ": started" in text and ": ready in" in text and ": check_0" in text


def test_a_maya_started_when_the_queue_is_empty_is_closed_without_a_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both workers find the run in the queue; the second Maya is up only after the first took it."""
    second_in, taken = threading.Event(), threading.Event()
    calls = itertools.count()

    def start() -> None:
        if next(calls) == 0:
            second_in.wait(5)
        else:
            second_in.set()
            taken.wait(5)

    pool, started, _ = fake_pool(monkeypatch, runs=1, count=2, start=start)

    def run_scenario(url: str, code: str, watch: object = None) -> tuple[list[str], str]:
        taken.set()
        return [], ""

    monkeypatch.setattr(scenario_run, "run_scenario", run_scenario)
    assert pool.run()
    first, second = started
    assert first.waited and first.closed
    assert not second.waited and second.closed


def test_a_maya_started_after_a_stop_is_closed_without_a_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C while a Maya starts: it must not be waited for past the harness closing the others."""
    pool, started, _ = fake_pool(monkeypatch, runs=1, count=1, start=lambda: pool.stop.set())
    monkeypatch.setattr(scenario_run, "run_scenario", lambda *_: pytest.fail("a run after the stop"))
    assert not pool.run()
    (only,) = started
    assert not only.waited and only.closed
    assert pool.outcomes == {}


@pytest.mark.parametrize("output", ['{"x": 1}', "null", '{"failures": "load"}', '{"failures": [1]}'])
def test_a_report_without_a_list_of_failures_is_a_raised_scenario(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(
        scenario_run.transport, "execute", lambda *_: scenario_run.transport.Result(True, "", output=output)
    )
    failures, text = scenario_run.run_scenario("http://x", "")
    assert failures is None
    assert "no report with a list of failures" in text


def test_run_at_url_fails_the_run_maya_did_not_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], project: Path, tmp_path: Path
) -> None:
    replies = iter([scenario_run.transport.Result(True, "", output='{"failures": []}')])

    def execute(*_: object) -> scenario_run.transport.Result:
        for reply in replies:
            return reply
        raise scenario_run.transport.TransportError("Maya at x stayed maya-busy")

    monkeypatch.setattr(scenario_run.session, "sessions", lambda temp: [])
    monkeypatch.setattr(scenario_run.transport, "connect", lambda *_: tmp_path)
    monkeypatch.setattr(scenario_run.transport, "execute", execute)
    assert cli.main(["run", "--url", "http://x", "all"]) == cli.FAILED
    out = capsys.readouterr().out
    assert "FAIL: no reply from http://x — TransportError: Maya at x stayed maya-busy" in out
    assert "check_fps" in out.splitlines()[-1] and "not run" in out.splitlines()[-1]


def test_load_runs_refuses_a_run_that_is_not_a_table(tmp_path: Path) -> None:
    table = tmp_path / "runs.toml"
    table.write_text('run = ["check_fps"]\n', encoding="utf-8")
    with pytest.raises(scenario_run.Refused, match=r"a run is a \[\[run\]\] table"):
        scenario_run.load_runs(table)


def test_run_json_with_every_slot_taken_keeps_stdout_one_json_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], project: Path, installed: None
) -> None:
    from maya_harness import slots

    monkeypatch.setenv(slots.VARIABLE, "1")
    now = [0.0]
    monkeypatch.setattr(slots, "clock", lambda: now[0])
    monkeypatch.setattr(slots, "pause", lambda seconds: now.__setitem__(0, now[0] + seconds))
    held = slots.take(0)
    try:
        assert cli.main(["run", "check_fps", "--json", "--slot-wait", "5"]) == cli.NOT_RUN
    finally:
        held.release()
    out, err = capsys.readouterr()
    assert len(out.splitlines()) == 1 and json.loads(out)["success"] is False
    assert "all 1 Maya slots are taken, waiting for one:" in err
    assert "maya1: all 1 Maya slots stayed taken for 5 s" in err


GIVE = session.process.give  # the real one: `installed` replaces it
PROCESS_ALL_ACCESS = 0x1FFFFF


def test_run_ends_once_no_run_is_left_though_a_maya_still_waits_for_a_slot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    installed: None,
    stub: Callable[..., "subprocess.Popen[str]"],
) -> None:
    """Two slots, one held by another agent; `--jobs 2`: maya1 takes the other slot and both runs,
    maya2 waits. The slot maya1 frees goes to another harness first: maya2 must give up, not wait."""
    from maya_harness import slots

    monkeypatch.setenv(slots.VARIABLE, "2")
    monkeypatch.setattr(session.process, "give", GIVE)  # stubs are real processes: they hold the slot
    outside = [slots.take(0)]
    grabbed = threading.Event()

    class Maya:
        def __init__(self, exe: Path, env: dict[str, str], visible: bool, number: int) -> None:
            self.child = stub("import sys; sys.stdin.readline()", stdin=subprocess.PIPE)
            self.pid = self.child.pid
            self.created = 0
            self.desktop = None
            self.handle = session.process.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, self.pid)

        def running(self) -> bool:
            return self.child.poll() is None

        def close(self) -> None:
            if self.child.poll() is None:
                self.child.kill()
                self.child.wait(5)
                session.process.kernel32.CloseHandle(self.handle)
                if not grabbed.is_set():  # another harness takes the freed slot
                    taken = slots.try_take()
                    assert taken is not None
                    outside.append(taken)
                    grabbed.set()

        def window_titles(self) -> list[str]:
            return []

    def pause(seconds: float) -> None:  # the waiter polls again only once the slot is gone
        grabbed.wait(5)

    monkeypatch.setattr(session.process, "MayaProcess", Maya)
    monkeypatch.setattr(session.transport, "wait_for_server", lambda *_: None)
    monkeypatch.setattr(session.transport, "connect", lambda *_: None)
    monkeypatch.setattr(session.transport, "server_logs", lambda pid: [])
    monkeypatch.setattr(scenario_run, "run_scenario", lambda *_: ([], ""))
    monkeypatch.setattr(slots, "pause", pause)
    code: list[int] = []
    runner = threading.Thread(
        target=lambda: code.append(cli.main(["run", "check_fps", "check_bake", "--jobs", "2"])), daemon=True
    )
    runner.start()
    runner.join(5)
    hung = runner.is_alive()
    for slot in outside:  # let a hung run end
        slot.release()
    runner.join(5)
    out, err = capsys.readouterr()
    assert not hung, "run still running after its runs"
    assert code == [cli.OK]
    assert out.count(" ok ") == 2
    assert "no longer needed" not in err


def test_a_stopped_pool_stops_waiting_for_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C in a library runner: a worker that waits for a slot must not wait on."""
    from maya_harness import slots

    monkeypatch.setenv(slots.VARIABLE, "1")
    held = slots.take(0)
    pool = scenario_run.Pool(
        [Run("check_0", {}, "")], [""], 1, session.SessionParams("p", Path("C:/p")), False, Path("C:/t"), io.StringIO()
    )

    def fake_start(
        *_: object, slot_wait: float | None = None, slot_unneeded: Callable[[], bool] | None = None, **__: object
    ) -> None:
        try:
            slots.take(slot_wait, unneeded=slot_unneeded)
        except slots.NoSlot as error:
            raise session.StartError(str(error)) from error
        pytest.fail("got a slot")

    monkeypatch.setattr(scenario_run.session, "start", fake_start)
    monkeypatch.setattr(slots, "pause", lambda seconds: pool.stop.set())  # the stop comes while it waits
    worker = threading.Thread(target=pool.work, args=(0,), daemon=True)
    worker.start()
    worker.join(5)
    stuck = worker.is_alive()
    held.release()
    worker.join(5)
    assert not stuck
    assert pool.sessions == {}


def test_run_at_url_fails_the_run_that_stands_on_a_dialog(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path
) -> None:
    def watch() -> None:
        raise scenario_run.transport.CallAborted("Maya 10 shows a modal dialog: ['Untrusted Plugin']")

    def execute(url: str, code: str, timeout: float, watch: Callable[[], None] | None = None) -> object:
        assert watch is not None and slots.in_call(tmp_path) is not None  # status: busy
        watch()
        raise AssertionError("the watch did not cut the call short")

    monkeypatch.setattr(scenario_run.transport, "execute", execute)
    out = io.StringIO()
    runs = [Run("check_fps", {}, ""), Run("check_fps", {"x": "1"}, "")]
    outcomes = scenario_run.run_at_url("http://x", tmp_path, runs, ["", ""], out, watch)
    assert not outcomes[0].ok and "modal dialog: ['Untrusted Plugin']" in outcomes[0].text
    assert 1 not in outcomes
    assert "not run" in out.getvalue().splitlines()[-1]
    assert slots.in_call(tmp_path) is None


def test_run_at_url_watches_the_maya_of_the_session_at_that_address(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path
) -> None:
    target = session.Record(tmp_path, session.State("p", str(tmp_path), 10, 1, "http://x", 0.0))
    watched: list[object] = []

    def watch() -> None:
        pass

    def execute(url: str, code: str, timeout: float, watch: object = None) -> object:
        watched.append(watch)
        return scenario_run.transport.Result(True, "", output='{"failures": []}')

    monkeypatch.setattr(scenario_run.session, "sessions", lambda temp: [target])
    monkeypatch.setattr(scenario_run.session, "watcher", lambda pid, url, desktop=None: watch)
    monkeypatch.setattr(scenario_run.transport, "connect", lambda *_: tmp_path)
    monkeypatch.setattr(scenario_run.transport, "execute", execute)
    assert cli.main(["run", "--url", "http://x", "check_fps"]) == cli.OK
    assert watched == [watch]
