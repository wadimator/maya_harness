"""Prepare a release of maya_harness: the version commit, its tag, the wheel and a draft of the notes.

`just release-prep X.Y.Z` runs this. Everything that can fail runs before the commit: the version
bump, `just check`, the wheel and the draft. A failure rolls the three files back, so a repeat
after it is a plain run from scratch. Nothing is pushed or published: the script prints the
commands for that.

Outside `src/`: the package knows nothing about its repository, and the wheel has no place for it.
"""

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["pyproject.toml", "uv.lock", "README.md"]
DIST = ROOT / "dist"

VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SEMVER = r"\d+\.\d+\.\d+"
README_WHEEL = re.compile(rf"releases/download/v{SEMVER}/maya_harness-{SEMVER}-py3-none-any\.whl")

# One commit per record: parents, then the whole message.
LOG_FORMAT = "%P%x1f%B%x1e"
# The merge body: "what and why" first, then these sections (docs/contributing.md, the merge).
NOTES_STOP = ("Вместе с ни", "Проверено")
DRAFT_MARKER = "<!-- draft: отредактируй и удали эту строку -->"


class Refusal(Exception):
    """A check before any change failed: nothing is touched, exit code 2."""


def parse_version(text: str) -> tuple[int, ...]:
    if not VERSION.match(text):
        raise Refusal(f"version {text!r} is not X.Y.Z")
    return tuple(int(part) for part in text.split("."))


def check_newer(new: str, current: str) -> None:
    """Compares as tuples of numbers: as strings, 0.10.0 would be older than 0.9.0."""
    if parse_version(new) <= parse_version(current):
        raise Refusal(f"version {new} is not newer than the current {current}")


def replace_once(pattern: re.Pattern[str], replacement: str, text: str, where: str) -> str:
    """Two matches mean the file is not what the script expects: a wrong guess is worse than a refusal."""
    new, count = pattern.subn(replacement, text)
    if count != 1:
        raise ValueError(f"{where}: {count} matches of {pattern.pattern!r}, expected 1")
    return new


def bump_pyproject(text: str, current: str, new: str) -> str:
    pattern = re.compile(rf'^version = "{re.escape(current)}"$', re.MULTILINE)
    return replace_once(pattern, f'version = "{new}"', text, "pyproject.toml")


def bump_readme(text: str, new: str) -> str:
    return replace_once(README_WHEEL, f"releases/download/v{new}/{wheel_name(new)}", text, "README.md")


def wheel_name(version: str) -> str:
    return f"maya_harness-{version}-py3-none-any.whl"


def wheel_version(path: Path) -> str:
    with zipfile.ZipFile(path) as wheel:
        name = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        for line in wheel.read(name).decode("utf-8").splitlines():
            if line.startswith("Version: "):
                return line.removeprefix("Version: ")
    raise ValueError(f"{path.name}: no Version in METADATA")


def release_notes(log: str) -> str:
    """The draft from `git log --first-parent --reverse --format=LOG_FORMAT`.

    Every commit of `main` since the last tag: a merge gives its title and the paragraphs before
    "Вместе с ним/ними:" or "Проверено:", a direct commit only its title. Nothing is made up: the
    text comes from the history, and a person edits it before the release.
    """
    lines = [DRAFT_MARKER]
    for record in log.split("\x1e"):
        if not record.strip():
            continue
        parents, message = record.strip("\n").split("\x1f")
        title, *paragraphs = re.split(r"\n\s*\n", message.strip())
        lines += ["", f"## {title.strip()}"]
        if len(parents.split()) < 2:
            continue
        for paragraph in paragraphs:
            if paragraph.startswith(NOTES_STOP):
                break
            # Commit bodies wrap at ~70 columns; a release page would show each line break.
            lines += ["", " ".join(line.strip() for line in paragraph.splitlines())]
    return "\n".join(lines) + "\n"


def write_lf(path: Path, text: str) -> None:
    """Bytes, not `write_text`: on Windows it turns every LF into CRLF, and the files are LF."""
    path.write_bytes(text.encode("utf-8"))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def run(*args: str) -> None:
    """A step whose output the developer should see as it goes."""
    subprocess.run(list(args), cwd=ROOT, check=True)


def check_ready(new: str) -> str:
    """Every check before any change; returns the current version."""
    if git("branch", "--show-current") != "main":
        raise Refusal("not on main")
    if git("status", "--porcelain"):
        raise Refusal("the working tree is not clean")
    git("fetch", "origin")
    if git("rev-parse", "main") != git("rev-parse", "origin/main"):
        raise Refusal("main differs from origin/main")
    current: str = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    check_newer(new, current)
    if git("tag", "--list", f"v{new}"):
        raise Refusal(f"tag v{new} exists")
    if git("ls-remote", "--tags", "origin", f"refs/tags/v{new}"):
        raise Refusal(f"tag v{new} exists on origin")
    if not git("tag", "--list", f"v{current}"):
        raise Refusal(f"no tag v{current} of the current version: the draft starts there")
    return current


def prepare(current: str, new: str, notes: Path) -> None:
    """Everything that can fail, the commit last: the tree then equals the version commit."""
    pyproject = ROOT / "pyproject.toml"
    write_lf(pyproject, bump_pyproject(pyproject.read_text(encoding="utf-8"), current, new))
    readme = ROOT / "README.md"
    write_lf(readme, bump_readme(readme.read_text(encoding="utf-8"), new))
    run("uv", "lock")
    numstat = git("diff", "--numstat", "uv.lock")
    if numstat.split()[:2] != ["1", "1"]:
        raise ValueError(f"uv.lock changed beyond the version: {numstat!r}")
    run("just", "check")
    run("uv", "build", "--wheel", "-o", "dist")
    built = wheel_version(DIST / wheel_name(new))
    if built != new:
        raise ValueError(f"the wheel has version {built}, expected {new}")
    log = git("log", "--first-parent", "--reverse", f"--format={LOG_FORMAT}", f"v{current}..HEAD")
    write_lf(notes, release_notes(log))
    git("add", "--", *FILES)
    run("git", "commit", "-m", f"build(version): {current} -> {new}")


def rollback(new: str, notes: Path) -> None:
    """From HEAD, not from the index: after `git add` the index already holds the new version."""
    git("checkout", "HEAD", "--", *FILES)
    (DIST / wheel_name(new)).unlink(missing_ok=True)
    notes.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: release.py X.Y.Z", file=sys.stderr)
        return 2
    new = argv[0]
    try:
        current = check_ready(new)
    except Refusal as error:
        print(f"release-prep: {error}", file=sys.stderr)
        return 2
    notes = DIST / f"release-notes-v{new}.md"
    try:
        prepare(current, new, notes)
    except BaseException as error:
        # Ctrl+C during `just check` too: otherwise the tree stays dirty and a repeat is refused.
        rollback(new, notes)
        if not isinstance(error, (subprocess.CalledProcessError, ValueError, OSError)):
            raise
        print(f"release-prep: {error}; the files are rolled back, nothing is committed", file=sys.stderr)
        return 1
    git("tag", "-a", f"v{new}", "-m", f"v{new}")
    wheel = f"dist/{wheel_name(new)}"
    print(
        f"""
Ready: commit and tag v{new}, {wheel}, dist/{notes.name}. Nothing is pushed. Next, by hand:
  1. edit dist/{notes.name} and delete its first line, the draft marker;
  2. git push --atomic origin main v{new}
  3. gh release create v{new} {wheel} --verify-tag --title v{new} --notes-file dist/{notes.name}
  4. check that the wheel link of README.md answers HTTP 200."""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
