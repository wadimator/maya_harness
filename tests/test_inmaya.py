from pathlib import Path

import pytest

from maya_harness import inmaya, session
from maya_harness.inmaya import Check


def test_check_keeps_the_actual_value_and_names_the_failure() -> None:
    check = Check("busy")
    check.expect("clicks.pairs", 2, 2)
    check.expect("clicks.rows", [1], [])
    check.fail("load", "no scene")
    assert check.failures == ["clicks.rows: [1] != []", "load: no scene"]
    assert check.report == {
        "control": "busy",
        "failures": check.failures,
        "clicks.pairs": 2,
        "clicks.rows": [1],
    }


def sections(ran: list[str]) -> dict[str, inmaya.Section]:
    def load(check: Check) -> None:
        ran.append("load")
        check.expect("load.ok", True, True)

    def bake(check: Check) -> None:
        ran.append("bake")
        raise KeyError("pair")

    def film(check: Check) -> None:
        ran.append("film")
        check.expect("film.keys", 3, 4)

    return {"load": load, "bake": bake, "film": film}


def test_run_sections_runs_all_and_goes_on_past_a_section_that_raised() -> None:
    ran: list[str] = []
    report = inmaya.run_sections(sections(ran), {"slow": "film"})
    assert ran == ["load", "bake", "film"]
    assert report["failures"] == ["bake.raised: KeyError: 'pair'", "film.keys: 3 != 4"]
    assert "KeyError" in str(report["bake.traceback"])
    assert list(report["sections"]) == ["load", "bake", "film"]


def test_run_sections_runs_the_section_of_a_control_alone() -> None:
    ran: list[str] = []
    report = inmaya.run_sections(sections(ran), {"slow": "film"}, control="slow")
    assert ran == ["film"]
    assert report["control"] == "slow"


def test_run_sections_runs_a_chosen_section_alone() -> None:
    ran: list[str] = []
    inmaya.run_sections(sections(ran), {}, section="load")
    assert ran == ["load"]


@pytest.mark.parametrize(
    ("controls", "control", "section", "refusal"),
    [
        ({"slow": "film"}, "fast", "", "No control 'fast': slow"),
        ({}, "", "glue", "No section 'glue': load, bake, film"),
        ({"slow": "gone"}, "slow", "", "No section 'gone'"),
    ],
)
def test_run_sections_refuses_what_the_scenario_has_not(
    controls: dict[str, str], control: str, section: str, refusal: str
) -> None:
    ran: list[str] = []
    with pytest.raises(ValueError, match=refusal):
        inmaya.run_sections(sections(ran), controls, control, section)
    assert ran == []


def test_work_dir_is_the_session_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(session.MARKER, r"C:\s\work")
    assert inmaya.work_dir() == Path(r"C:\s\work")


def test_work_dir_outside_a_session_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(session.MARKER, raising=False)
    with pytest.raises(RuntimeError, match="run it with maya-harness"):
        inmaya.work_dir()
