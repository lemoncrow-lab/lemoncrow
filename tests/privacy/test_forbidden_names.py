"""Guard: names that must not appear in the product, in file contents or (where stated) in file paths.

Each row of ``FORBIDDEN`` is one forbidden name with the places it is scanned and the paths allowed to keep it:

* **An unrelated project's name.** LemonCrow once posted per-session usage to that project's host. The call now goes
  through the telemetry rollup client; this keeps the name from coming back anywhere in the repository. The test spells
  it in two halves so that this file does not contain it.
* **A competitor's name** (and the config file names built on it). The product never names it: it reads no competitor's
  config and maps none of its settings, and the benchmark harness is reviewer-agnostic. The name may live only in
  marketing and strategy material (``docs/marketing/``, ``product.md``, ``differential.html``, ``research_notes/``)
  and the private plans under ``docs/planning/``, none of which is scanned. The test spells the name in two halves so
  that this file does not contain it either. Two narrow exceptions, both for the published code-review benchmark
  table, which is marketing copy that has to name the tools it compares: ``README.md`` may carry the name between its
  one pair of ``<!-- lemoncrow:benchmark:start -->`` / ``<!-- lemoncrow:benchmark:end -->`` markers and nowhere else,
  and the one data file that table is rendered from (``evals/review/published/code_review_benchmark.json``) may name
  the tools it lists, as may the raw run reports it links (``.json`` files directly under
  ``evals/review/published/runs/``, written by the benchmark harness, which quote the other reviewer's comments).
  Anything else in those places, or any other file, still fails.

Matching is case-insensitive. The scan covers the files git lists under the roots (tracked ones, and new ones not yet
added, never ignored ones), or a plain directory walk when git is not available.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
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
#: The published benchmark table (marketing copy) and the one file it is rendered from. See the module docstring.
BENCHMARK_MARKERS = (b"<!-- lemoncrow:benchmark:start -->", b"<!-- lemoncrow:benchmark:end -->")
BENCHMARK_DATA = "evals/review/published/code_review_benchmark.json"
BENCHMARK_RUNS = ("evals/review/published/runs/", ".json")


@dataclass(frozen=True, slots=True)
class ForbiddenName:
    """One forbidden name: where it is scanned, and which paths may keep it."""

    label: str
    needle: bytes  # lowercase; compared with the lowercased bytes of each file
    roots: tuple[str, ...]  # directories and files, relative to the repository root
    why: str
    allowed: frozenset[str] = frozenset()  # repository-relative paths exempt from the scan
    scan_paths: bool = False  # also forbid the name in a file's path, not only its contents
    # (directory prefix, file suffix): files directly in that directory with that suffix are exempt from the scan
    allowed_files_in: tuple[tuple[str, str], ...] = ()
    # repository-relative path -> (start, end) marker bytes: the name may sit between that file's one pair of markers
    marked_regions: Mapping[str, tuple[bytes, bytes]] = field(default_factory=dict)


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
        allowed=frozenset({BENCHMARK_DATA}),
        allowed_files_in=(BENCHMARK_RUNS,),
        marked_regions={"README.md": BENCHMARK_MARKERS},
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
        if path.resolve() == THIS_FILE or relative in rule.allowed or _in_allowed_directory(relative, rule):
            continue
        if rule.scan_paths and rule.needle in relative.lower().encode():
            found.append(f"{relative} (path)")
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if relative in rule.marked_regions:
            content = _outside_markers(content, *rule.marked_regions[relative])
        if rule.needle in content.lower():
            found.append(f"{relative} (contents)")
    return sorted(found)


def _in_allowed_directory(relative: str, rule: ForbiddenName) -> bool:
    """Whether *relative* is a file directly inside one of the rule's allowed (directory, suffix) pairs."""

    return any(
        relative.startswith(prefix) and "/" not in relative[len(prefix) :] and relative.endswith(suffix)
        for prefix, suffix in rule.allowed_files_in
    )


def _outside_markers(content: bytes, start: bytes, end: bytes) -> bytes:
    """*content* without the text between its one well-formed pair of markers.

    A file that does not carry exactly one start and one end marker, in that order, gets nothing exempted: the
    exception is for the one section a script writes, so a stray, repeated, swapped or unterminated marker must not
    widen it.
    """

    if content.count(start) != 1 or content.count(end) != 1:
        return content
    head, rest = content.split(start, 1)
    if end not in rest:
        return content
    return head + rest.split(end, 1)[1]


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


# --- the published benchmark table: the competitor's name may appear in exactly two places ------------------------

COMPETITOR = next(rule for rule in FORBIDDEN if rule.label == "competitor name")
NAME = COMPETITOR.needle.decode()
START, END = (marker.decode() for marker in BENCHMARK_MARKERS)


def _competitor_offenders(tmp_path: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return offenders(tmp_path, COMPETITOR, [tmp_path / name for name in files])


def test_the_benchmark_exceptions_are_exactly_the_readme_markers_the_data_file_and_its_run_reports() -> None:
    assert COMPETITOR.allowed == frozenset({"evals/review/published/code_review_benchmark.json"})
    assert COMPETITOR.allowed_files_in == (("evals/review/published/runs/", ".json"),)
    assert dict(COMPETITOR.marked_regions) == {"README.md": BENCHMARK_MARKERS}
    # Nothing else is loosened: every other rule is as strict as before.
    assert all(
        not rule.allowed and not rule.allowed_files_in and not rule.marked_regions
        for rule in FORBIDDEN
        if rule is not COMPETITOR
    )
    assert "README.md" in COMPETITOR.roots and "evals" in COMPETITOR.roots


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (f"intro\n{START}\n| {NAME} | 45% |\n{END}\noutro\n", False),
        (f"intro\n{START}\n{END}\noutro\n", False),
        (f"{NAME} up front\n{START}\n{END}\n", True),
        (f"{START}\n{END}\nand {NAME} afterwards\n", True),
        (f"{START}\n{NAME}\n{END}\nand {NAME} again\n", True),
        (f"{START}\n{END}\n{START}\n{NAME}\n{END}\n", True),  # a second pair is not exempt
        (f"{START}\n{NAME}\n", True),  # unterminated
        (f"{END}\n{NAME}\n{START}\n", True),  # swapped
        (f"{START}\n{NAME}\n{START}\n{END}\n", True),  # a stray extra start
        (f"{START.upper()}\n{NAME}\n{END.upper()}\n", True),  # the markers are matched exactly
        (f"just {NAME.upper()} in prose\n", True),
    ],
)
def test_the_readme_may_name_the_competitor_only_between_its_one_pair_of_benchmark_markers(
    tmp_path: Path, text: str, flagged: bool
) -> None:
    assert _competitor_offenders(tmp_path, {"README.md": text}) == (["README.md (contents)"] if flagged else [])


def test_the_exceptions_do_not_travel_to_other_files(
    tmp_path: Path,
) -> None:
    marked = f"{START}\n{NAME}\n{END}\n"
    assert _competitor_offenders(
        tmp_path,
        {
            "docs/setup/page.md": marked,  # the markers do not travel to another file
            "CHANGELOG.md": marked,
            "evals/review/published/code_review_benchmark.json": f'{{"tool": "{NAME}"}}',  # the one data file
            "evals/review/published/README.md": f"mentions {NAME}",  # its neighbours are not exempt
            "evals/review/published/other.json": f'{{"tool": "{NAME}"}}',
            "evals/review/published/runs/rescored-x-2026-11-02.json": f'{{"tool": "{NAME}"}}',  # a raw run report
            "evals/review/published/runs/notes.md": f"mentions {NAME}",  # only the reports, only .json
            "evals/review/published/runs/nested/deeper.json": f'{{"tool": "{NAME}"}}',  # only directly inside
            "evals/review/other-runs/report.json": f'{{"tool": "{NAME}"}}',
        },
    ) == [
        "CHANGELOG.md (contents)",
        "docs/setup/page.md (contents)",
        "evals/review/other-runs/report.json (contents)",
        "evals/review/published/README.md (contents)",
        "evals/review/published/other.json (contents)",
        "evals/review/published/runs/nested/deeper.json (contents)",
        "evals/review/published/runs/notes.md (contents)",
    ]
