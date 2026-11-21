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
