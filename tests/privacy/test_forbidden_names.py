"""Guard: names that must not appear in the product, in file contents or (where stated) in file paths.

Each row of ``FORBIDDEN`` is one forbidden name with the places it is scanned and the paths allowed to keep it:

* **An unrelated project's name.** LemonCrow once posted per-session usage to that project's host. The call now goes
  through the telemetry rollup client; this keeps the name from coming back anywhere in the repository. The test spells
  it in two halves so that this file does not contain it.
* **A competitor's name** (and the config file names built on it). The product never names it: it reads no competitor's
  config and maps none of its settings, and the benchmark harness is reviewer-agnostic. The name may live only in
  marketing and strategy material (``docs/marketing/``, ``product.md``, ``differential.html``, ``research_notes/``)
  and the private plans under ``docs/planning/``, none of which is scanned. The test spells the name in two halves so
  that this file does not contain it either.

Matching is case-insensitive. The scan covers the files git lists under the roots (tracked ones, and new ones not yet
added, never ignored ones), or a plain directory walk when git is not available.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".docusaurus",
        "dist",
        "build",
    }
)
THIS_FILE = Path(__file__).resolve()


@dataclass(frozen=True, slots=True)
class ForbiddenName:
    """One forbidden name: where it is scanned, and which paths may keep it."""

    label: str
    needle: bytes  # lowercase; compared with the lowercased bytes of each file
    roots: tuple[str, ...]  # directories and files, relative to the repository root
    why: str
    allowed: frozenset[str] = frozenset()  # repository-relative paths exempt from the scan
    scan_paths: bool = False  # also forbid the name in a file's path, not only its contents


FORBIDDEN: tuple[ForbiddenName, ...] = (
    ForbiddenName(
        label="unrelated project name",
        needle=("be" + "seam").encode(),
        roots=(".",),
        why="LemonCrow has nothing to do with this unrelated project",
        scan_paths=True,
    ),
    ForbiddenName(
        label="competitor name",
        needle=("grep" + "tile").encode(),
        roots=(
            "src",
            "client",
            "enterprise",
            "integrations",
            "server",
            "frontend",
            "tests",
            "evals",
            "scripts",
            "docs/setup",
            "docs/reference",
            "docs/status",
            "docs-site",
            "action.yml",
            "CHANGELOG.md",
            "README.md",
        ),
        why="the product names no competitor; only marketing and strategy material may",
        scan_paths=True,
    ),
)


def _git_files(repo: Path, roots: tuple[str, ...]) -> list[Path] | None:
    """The files git lists under *roots* (tracked, and untracked but not ignored), or ``None`` without git."""

    try:
        listed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *roots],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [name for name in listed.stdout.decode("utf-8", errors="replace").split("\0") if name]
    return [repo / name for name in names] if names else None


def _walked_files(repo: Path, roots: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for name in roots:
        top = repo / name
        if top.is_file():
            found.append(top)
            continue
        for directory, subdirs, names in os.walk(top):
            subdirs[:] = [d for d in subdirs if d not in SKIPPED_DIRS]
            found.extend(Path(directory, n) for n in names)
    return found


def scan_files(repo: Path, rule: ForbiddenName) -> list[Path]:
    files = _git_files(repo, rule.roots)
    return files if files is not None else _walked_files(repo, rule.roots)


def offenders(repo: Path, rule: ForbiddenName, files: list[Path]) -> list[str]:
    """Repository-relative paths that carry the rule's name, in their contents or (if asked) their path."""

    found: list[str] = []
    for path in files:
        relative = path.relative_to(repo).as_posix()
        if path.resolve() == THIS_FILE or relative in rule.allowed:
            continue
        if rule.scan_paths and rule.needle in relative.lower().encode():
            found.append(f"{relative} (path)")
            continue
        try:
            if rule.needle in path.read_bytes().lower():
                found.append(f"{relative} (contents)")
        except OSError:
            continue
    return sorted(found)


@pytest.mark.parametrize("rule", FORBIDDEN, ids=[rule.label for rule in FORBIDDEN])
def test_the_forbidden_name_appears_nowhere_it_is_scanned(rule: ForbiddenName) -> None:
    files = scan_files(REPO, rule)
    assert files, f"the scan found no files for {rule.label}; its roots are wrong"
    found = offenders(REPO, rule, files)
    assert not found, f"{rule.why}; remove the {rule.label} from: {found}"


def test_the_scan_catches_contents_and_paths_in_any_case_and_honours_the_allow_list(tmp_path: Path) -> None:
    """The guard itself: a name hidden in a file's text, in mixed case, or only in its path is found."""

    probe = ForbiddenName(
        label="probe",
        needle=b"zzprobe",
        roots=("area", "top.md", "absent"),
        why="probe",
        allowed=frozenset({"area/kept.md"}),
        scan_paths=True,
    )
    for name, text in {
        "area/clean.md": "nothing here",
        "area/text.md": "mentions ZzProbe once",
        "area/kept.md": "the zzprobe, but allowed",
        "area/zzprobe-notes.md": "clean text, forbidden path",
        "area/nested/deep.txt": "ZZPROBE",
        "elsewhere/ignored.md": "zzprobe outside the roots",
        "top.md": "zzprobe at the top",
    }.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    files = _walked_files(tmp_path, probe.roots)
    assert offenders(tmp_path, probe, files) == [
        "area/nested/deep.txt (contents)",
        "area/text.md (contents)",
        "area/zzprobe-notes.md (path)",
        "top.md (contents)",
    ]
    contents_only = ForbiddenName("probe", b"zzprobe", probe.roots, "probe", probe.allowed)
    assert "area/zzprobe-notes.md (path)" not in offenders(tmp_path, contents_only, files)


def test_the_skipped_directories_are_not_scanned(tmp_path: Path) -> None:
    probe = ForbiddenName("probe", b"zzprobe", ("area",), "probe")
    for name in ("area/node_modules/pkg/index.js", "area/.venv/lib/x.py", "area/ok.py"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("zzprobe" if "ok" not in name else "fine", encoding="utf-8")
    assert offenders(tmp_path, probe, _walked_files(tmp_path, probe.roots)) == []
