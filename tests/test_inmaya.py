import json
import os
import sys
import types
from pathlib import Path

import pytest

from maya_harness import inmaya
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
    monkeypatch.setenv(inmaya.MARKER, r"C:\s\work")
    assert inmaya.work_dir() == Path(r"C:\s\work")


def test_work_dir_outside_a_session_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(inmaya.MARKER, raising=False)
    with pytest.raises(RuntimeError, match="run it with maya-harness"):
        inmaya.work_dir()


def test_reload_drops_the_packages_their_submodules_and_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("fake_pkg", "fake_pkg.sub", "fake_pkg_other", "check_fake", "other_check_fake"):
        monkeypatch.setitem(sys.modules, name, sys)
    inmaya.reload(["fake_pkg"], "check_")
    assert [name for name in ("fake_pkg", "fake_pkg.sub", "check_fake") if name in sys.modules] == []
    assert sys.modules["fake_pkg_other"] is sys
    assert sys.modules["other_check_fake"] is sys


def test_reload_without_a_prefix_keeps_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "check_fake", sys)
    inmaya.reload([])
    assert sys.modules["check_fake"] is sys


def test_run_scenario_imports_the_scenario_and_its_package_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", [str(tmp_path), "elsewhere", str(tmp_path)])
    for name in ("fake_plugin", "check_fake"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    (tmp_path / "fake_plugin.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    scenario = (
        "import fake_plugin\n\ndef run(**params):\n    return {'failures': [], 'value': fake_plugin.VALUE, **params}\n"
    )
    (tmp_path / "check_fake.py").write_text(scenario, encoding="utf-8")
    report = inmaya.run_scenario(str(tmp_path), "check_fake", {"control": "x"}, ["fake_plugin"])
    assert json.loads(report) == {"failures": [], "value": "old", "control": "x"}
    assert sys.path == [str(tmp_path), "elsewhere"]
    (tmp_path / "fake_plugin.py").write_text(
        "VALUE = 'newer'  # another size: a .pyc of the same size and second is taken as fresh\n", encoding="utf-8"
    )
    (tmp_path / "check_fake.py").write_text(scenario.replace("'value'", "'changed'"), encoding="utf-8")
    report = inmaya.run_scenario(str(tmp_path), "check_fake", {}, ["fake_plugin"])
    assert json.loads(report) == {"failures": [], "changed": "newer"}
    monkeypatch.delitem(sys.modules, "fake_plugin")
    monkeypatch.delitem(sys.modules, "check_fake")


class FakeCmds:
    """`maya.cmds` for `startup`: what it was asked to do."""

    def __init__(self, missing: str = "") -> None:
        self.missing = missing
        self.plugins: list[str] = []
        self.deferred: list[object] = []
        self.options: list[object] = []

    def optionVar(self, **kwargs: object) -> None:  # noqa: N802 - the name of maya.cmds
        self.options.append(kwargs)

    def evalDeferred(self, function: object) -> None:  # noqa: N802
        self.deferred.append(function)

    def loadPlugin(self, name: str, quiet: bool) -> None:  # noqa: N802
        if name == self.missing:
            raise RuntimeError(f"Plug-in, {name}, was not found on MAYA_PLUG_IN_PATH.")
        self.plugins.append(name)


@pytest.fixture
def cmds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCmds:
    fake = FakeCmds()
    maya = types.ModuleType("maya")
    maya.cmds = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "maya", maya)
    monkeypatch.setitem(sys.modules, "maya.cmds", fake)  # type: ignore[arg-type]
    monkeypatch.setenv(inmaya.MARKER, str(tmp_path))
    return fake


def test_startup_loads_the_project_plugins_then_dcc_mcp(
    cmds: FakeCmds, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(inmaya.PLUGINS, os.pathsep.join(["fbxmaya", "", "gameFbxExporter"]))
    monkeypatch.setenv(inmaya.TTL, "0")
    inmaya.startup()
    assert cmds.plugins == ["fbxmaya", "gameFbxExporter", "dcc_mcp_maya_plugin"]
    assert cmds.options == [{"intValue": ("SafeModeUserSetupHashOption", 0)}]
    assert cmds.deferred == []  # no idle timer
    assert (tmp_path / inmaya.LAST_CALL).is_file()
    assert (tmp_path / inmaya.STARTUP).read_text(encoding="utf-8") == "dcc_mcp_maya_plugin loaded"


def test_startup_starts_the_idle_timer_with_a_ttl(cmds: FakeCmds, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(inmaya.PLUGINS, raising=False)
    monkeypatch.setenv(inmaya.TTL, "5")
    inmaya.startup()
    assert len(cmds.deferred) == 1
    assert cmds.plugins == ["dcc_mcp_maya_plugin"]


def test_startup_stops_at_a_missing_plugin(cmds: FakeCmds, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cmds.missing = "no_such_plugin"
    monkeypatch.setenv(inmaya.PLUGINS, "no_such_plugin")
    with pytest.raises(RuntimeError, match="no_such_plugin"):
        inmaya.startup()
    assert cmds.plugins == []
    assert (tmp_path / inmaya.STARTUP).read_text(encoding="utf-8") == "userSetup.py loaded"
