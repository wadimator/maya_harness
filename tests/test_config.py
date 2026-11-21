from pathlib import Path

import pytest

from maya_harness.config import ConfigError, load


def write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(text, encoding="utf-8")
    return root


def test_empty_section_takes_the_project_name(tmp_path: Path) -> None:
    root = write(tmp_path / "plugin", '[project]\nname = "ue_exporter"\n')
    config = load(root)
    assert config.project == "ue_exporter"
    assert config.root == root.resolve()
    assert config.sys_path == config.plug_in_path == ()
    assert config.plugins == config.reload == ()


def test_all_keys_from_a_subdirectory(tmp_path: Path) -> None:
    root = write(
        tmp_path / "plugin",
        '[project]\nname = "ue_exporter"\n'
        "[tool.maya_harness]\n"
        'project = "UE_exporter"\n'
        'sys_path = ["..", "vendor"]\n'
        'plugins = ["fbxmaya", "gameFbxExporter"]\n'
        'plug_in_path = ["maya_plugins"]\n'
        'reload = ["UE_exporter"]\n',
    )
    (root / "sub").mkdir()
    config = load(root / "sub")
    assert config.project == "UE_exporter"
    assert config.root == root.resolve()
    assert config.sys_path == (tmp_path.resolve(), (root / "vendor").resolve())
    assert config.plugins == ("fbxmaya", "gameFbxExporter")
    assert config.plug_in_path == ((root / "maya_plugins").resolve(),)
    assert config.reload == ("UE_exporter",)
    assert config.params().sys_path == config.sys_path


def test_unknown_key_names_the_known_ones(tmp_path: Path) -> None:
    root = write(tmp_path, '[tool.maya_harness]\nplugin = ["fbxmaya"]\n')
    with pytest.raises(ConfigError, match=r"unknown keys .*: plugin; known: project, sys_path, plugins"):
        load(root)


def test_wrong_type_is_an_error(tmp_path: Path) -> None:
    root = write(tmp_path, '[tool.maya_harness]\nplugins = "fbxmaya"\n')
    with pytest.raises(ConfigError, match="list of strings"):
        load(root)


def test_no_pyproject_is_the_directory_itself(tmp_path: Path) -> None:
    root = tmp_path / "loose"
    root.mkdir()
    config = load(root)
    assert config.project == "loose"
    assert config.root == root.resolve()
    assert config.sys_path == ()


def test_scenarios_are_paths_from_the_root(tmp_path: Path) -> None:
    root = write(
        tmp_path / "plugin",
        '[tool.maya_harness.scenarios]\ndir = "dev_tools/checks"\nruns = "dev_tools/checks/runs.toml"\n',
    )
    scenarios = load(root).scenarios
    assert scenarios is not None
    assert (scenarios.dir, scenarios.runs) == (
        (root / "dev_tools" / "checks").resolve(),
        (root / "dev_tools" / "checks" / "runs.toml").resolve(),
    )
    assert load(write(tmp_path / "bare", "[tool.maya_harness]\n")).scenarios is None


@pytest.mark.parametrize(
    "table",
    ['dir = "checks"\n', 'dir = "checks"\nruns = "r.toml"\nprefix = "check_"\n', 'dir = "checks"\nruns = 1\n'],
)
def test_scenarios_take_dir_and_runs_only(tmp_path: Path, table: str) -> None:
    root = write(tmp_path, f"[tool.maya_harness.scenarios]\n{table}")
    with pytest.raises(ConfigError, match=r"\[tool.maya_harness.scenarios\]"):
        load(root)
