"""The project's settings: `[tool.maya_harness]` of the nearest `pyproject.toml` up from a directory.

```toml
[tool.maya_harness]
project = "UE_exporter"         # the name in `status` and in the session directory;
                                # by default [project].name
sys_path = ["..", "vendor"]     # relative to the project root; first on Maya's sys.path
plugins = ["fbxmaya"]           # Maya plugins loaded on startup, before dcc-mcp
plug_in_path = ["maya_plugins"] # MAYA_PLUG_IN_PATH, relative to the project root
reload = ["UE_exporter"]        # top packages dropped from sys.modules by `exec --reload`

[tool.maya_harness.scenarios]   # for `run`; both keys are needed
dir = "dev_tools/maya_checks"   # the check_*.py scenarios; first on Maya's sys.path at a run
runs = "dev_tools/maya_checks/runs.toml"  # what `run all` runs: see run.py
```

Without a `pyproject.toml` the project is the directory itself, with nothing to add to Maya.
An unknown key is an error: a typo would otherwise start a Maya without what it names.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .session import SessionParams

KNOWN = ("project", "sys_path", "plugins", "plug_in_path", "reload", "scenarios")
SCENARIO_KEYS = ("dir", "runs")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Scenarios:
    """`[tool.maya_harness.scenarios]`: the directory of the scenarios and the table of the runs."""

    dir: Path
    runs: Path


@dataclass(frozen=True)
class Config:
    project: str
    root: Path
    sys_path: tuple[Path, ...] = ()
    plugins: tuple[str, ...] = ()
    plug_in_path: tuple[Path, ...] = ()
    reload: tuple[str, ...] = ()
    scenarios: Scenarios | None = None

    def params(self) -> SessionParams:
        return SessionParams(self.project, self.root, self.sys_path, self.plugins, self.plug_in_path)


def find_pyproject(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _strings(section: dict[str, object], key: str, pyproject: Path) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{pyproject}: [tool.maya_harness].{key} must be a list of strings")
    return tuple(value)


def _scenarios(section: dict[str, object], root: Path, pyproject: Path) -> Scenarios | None:
    value = section.get("scenarios")
    if value is None:
        return None
    if not isinstance(value, dict) or sorted(value) != sorted(SCENARIO_KEYS):
        raise ConfigError(f"{pyproject}: [tool.maya_harness.scenarios] takes exactly {', '.join(SCENARIO_KEYS)}")
    if not all(isinstance(value[key], str) for key in SCENARIO_KEYS):
        raise ConfigError(f"{pyproject}: [tool.maya_harness.scenarios] {', '.join(SCENARIO_KEYS)} must be strings")
    return Scenarios(dir=(root / value["dir"]).resolve(), runs=(root / value["runs"]).resolve())


def load(start: Path) -> Config:
    """The settings of the project that holds `start`."""
    start = start.resolve()
    pyproject = find_pyproject(start)
    if pyproject is None:
        return Config(project=start.name, root=start)
    root = pyproject.parent
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{pyproject}: {error}") from error
    section = data.get("tool", {}).get("maya_harness", {})
    unknown = sorted(set(section) - set(KNOWN))
    if unknown:
        raise ConfigError(
            f"{pyproject}: unknown keys in [tool.maya_harness]: {', '.join(unknown)}; known: {', '.join(KNOWN)}"
        )
    project = section.get("project") or data.get("project", {}).get("name") or root.name
    if not isinstance(project, str):
        raise ConfigError(f"{pyproject}: [tool.maya_harness].project must be a string")
    return Config(
        project=project,
        root=root,
        sys_path=tuple((root / path).resolve() for path in _strings(section, "sys_path", pyproject)),
        plugins=_strings(section, "plugins", pyproject),
        plug_in_path=tuple((root / path).resolve() for path in _strings(section, "plug_in_path", pyproject)),
        reload=_strings(section, "reload", pyproject),
        scenarios=_scenarios(section, root, pyproject),
    )
