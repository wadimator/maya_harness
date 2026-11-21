"""What `run` runs and whether a run passed; the Mayas themselves are checked by running it."""

from pathlib import Path

import pytest

from maya_harness import cli, config, session
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


def test_run_removes_the_session_of_a_maya_that_failed_to_start(
    monkeypatch: pytest.MonkeyPatch, project: Path, tmp_path: Path
) -> None:
    def fail_to_start(*_: object, **__: object) -> None:
        raise OSError("no maya.exe")

    monkeypatch.setattr(cli, "maya_and_module", lambda: (tmp_path / "maya.exe", tmp_path / "x.mod"))
    monkeypatch.setattr(session, "prepare", lambda *_, **__: {})
    monkeypatch.setattr(cli.process, "MayaProcess", fail_to_start)
    assert cli.main(["run", "check_fps"]) == cli.NOT_RUN
    assert list((tmp_path / "temp").iterdir()) == []


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
