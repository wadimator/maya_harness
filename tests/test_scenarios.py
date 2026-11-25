"""What `run` runs and whether a run passed."""

from pathlib import Path

import pytest

from maya_harness import cli, config, inmaya, scenarios, session
from maya_harness.scenarios import Run

ALL = [
    Run("check_bake", {}, ""),
    Run("check_bake", {"control": "no_round"}, "film.whole_frames"),
    Run("check_fps", {}, ""),
]


def settings(root: Path) -> config.Scenarios:
    found = config.load(root).scenarios
    assert found is not None
    return found


def test_load_runs_takes_the_longest_first(project: Path) -> None:
    assert scenarios.load_runs(settings(project).runs) == ALL


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
def test_load_runs_refuses_a_wrong_entry(project: Path, entry: str, refusal: str) -> None:
    table = project / "checks" / "runs.toml"
    table.write_text(f"{table.read_text(encoding='utf-8')}\n[[run]]\n{entry}\n", encoding="utf-8")
    with pytest.raises(scenarios.Refused, match=refusal):
        scenarios.load_runs(table)


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
    assert scenarios.verdict(expected, failures) == result


@pytest.mark.parametrize(
    ("argv", "runs"),
    [
        (["check_fps"], [Run("check_fps", {}, "")]),
        (["check_fps", "check_bake"], [Run("check_fps", {}, ""), Run("check_bake", {}, "")]),
        (["check_bake", "section=film"], [Run("check_bake", {"section": "film"}, "")]),
        (["check_bake", "control=no_round"], [Run("check_bake", {"control": "no_round"}, "film.whole_frames")]),
        (["check_bake", "control=nope"], [Run("check_bake", {"control": "nope"}, "")]),
        (["all"], ALL),
        (["scenarios"], [ALL[0], ALL[2]]),
        (["controls"], [ALL[1]]),
    ],
)
def test_parse(argv: list[str], runs: list[Run]) -> None:
    assert scenarios.parse(argv, ALL) == runs


@pytest.mark.parametrize(
    "argv",
    [
        ["all", "section=film"],
        ["all", "control=no_round"],
        ["scenarios", "section=film"],
        ["controls", "control=no_round"],
        ["check_bake", "check_fps", "section=film"],
        ["check_bake", "check_fps", "control=no_round"],
    ],
)
def test_parse_refuses_parameters_of_several_runs(argv: list[str]) -> None:
    with pytest.raises(scenarios.Refused, match="A control or a section runs alone"):
        scenarios.parse(argv, ALL)


def test_code_runs_the_scenario_through_inmaya(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = scenarios.code_of(Run("check_bake", {"control": "no_round"}, "x"), project / "checks", ("plugin",))
    called: list[object] = []
    monkeypatch.setattr(inmaya, "run_scenario", lambda *args: called.append(args) or "{}")
    exec(code, {})
    assert called == [(str(project / "checks"), "check_bake", {"control": "no_round"}, ("plugin",))]
    assert code.rstrip().splitlines()[-1].startswith("_maya_harness_inmaya.run_scenario(")  # the report is its value


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


def test_load_runs_refuses_a_run_that_is_not_a_table(tmp_path: Path) -> None:
    table = tmp_path / "runs.toml"
    table.write_text('run = ["check_fps"]\n', encoding="utf-8")
    with pytest.raises(scenarios.Refused, match=r"a run is a \[\[run\]\] table"):
        scenarios.load_runs(table)
