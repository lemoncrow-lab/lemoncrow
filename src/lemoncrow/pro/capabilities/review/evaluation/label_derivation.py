"""Derive a benchmark label from git alone: where a bug fix edited, mapped back to the change that introduced the bug.

A label is ground truth only if nobody had to decide it. So nothing here reads a diff for meaning. Given a **fix
commit** F and the **change that introduced the bug** B (a squash or ordinary commit, or a merge commit for a pull
request with several commits), it does three mechanical things:

1. takes the lines F removed or changed (or, for a pure insertion, the line before it), in F's parent;
2. maps each of them back to the reviewed head H of B's change, by the diff between H and F's parent, and drops a line
   that diff changed (it is no longer the code B shipped);
3. keeps only source files (not tests, docs or changelogs), and lists the identifiers on the lines F touched as
   *candidate* symbols for a person to trim to the distinctive ones.

A fix that edits far more than the reviewed change added would otherwise label code the pull request never touched.
``within_change`` keeps only the fix sites that overlap lines the reviewed change itself added or changed (still
mechanical: it is the diff between the base and the reviewed head), and says so in the label's derivation text.

Whether B really introduced the bug is the part git cannot decide. The evidence is one of: F's message names B (or B's
pull request), or ``git blame`` attributes F's removed lines to B's commits. The label records which
(``attribution``); a case where neither holds is not a case.

Used by ``lc dev eval review derive-label`` and by the tests; the corpus in ``evals/review/real_prs.yaml`` was made
with it.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.git_env import git_environment
from lemoncrow.pro.capabilities.review.repo_context import is_test_path

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE = re.compile(r"^diff --git a/(.*) b/(.*)$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".swift",
    }
)
_NOISE: Final[frozenset[str]] = frozenset("""
    self cls none true false null and or not the for while with from import return def class if else elif try except
    finally raise pass lambda yield await async in is as del assert global nonlocal print len int str list dict set
    tuple bool float range isinstance type object super var let const function new this typeof void undefined
    """.split())


class DerivationError(RuntimeError):
    """The pair cannot yield a label; the reason says what to check."""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env=git_environment(),
    )
    if completed.returncode != 0:
        raise DerivationError(f"git {' '.join(args[:3])} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout


def _hunks(
    repo: Path, old: str, new: str, path: str | None = None
) -> dict[tuple[str, str], list[tuple[int, int, int, int]]]:
    out = _git(repo, "diff", "-U0", "-M", old, new, *(["--", path] if path else []))
    files: dict[tuple[str, str], list[tuple[int, int, int, int]]] = {}
    current: tuple[str, str] | None = None
    for line in out.splitlines():
        header = _FILE.match(line)
        if header:
            current = (header.group(1), header.group(2))
            files[current] = []
            continue
        hunk = _HUNK.match(line)
        if hunk and current is not None:
            files[current].append(
                (
                    int(hunk.group(1)),
                    int(hunk.group(2)) if hunk.group(2) is not None else 1,
                    int(hunk.group(3)),
                    int(hunk.group(4)) if hunk.group(4) is not None else 1,
                )
            )
    return files


def _map_back(hunks: Sequence[tuple[int, int, int, int]], line: int) -> int | None:
    """A line of the diff's new side as the old side numbers it; ``None`` when the diff changed that line."""

    shift = 0
    for _old_start, old_count, new_start, new_count in hunks:
        if new_count > 0 and new_start <= line < new_start + new_count:
            return None
        if (new_count > 0 and new_start + new_count <= line) or (new_count == 0 and new_start < line):
            shift += old_count - new_count
    return line + shift


def is_source_path(path: str) -> bool:
    """A file a reviewer would be expected to find a bug in: source code, not tests, docs or changelogs."""

    return Path(path).suffix.lower() in _SOURCE_SUFFIXES and not is_test_path(path)


@dataclass(frozen=True, slots=True)
class DerivedRegion:
    path: str
    start_line: int
    end_line: int
    kind: str  # changed | insertion
    in_change: int  # how many of the lines git blame attributes to the introducing change

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "lines": [self.start_line, self.end_line], "role": "fix_site"}


@dataclass(frozen=True, slots=True)
class Derivation:
    fix_commit: str
    fix_subject: str
    introduced_by: str
    base_sha: str
    head_sha: str
    fix_parent: str
    merge: bool
    regions: tuple[DerivedRegion, ...]
    symbols: tuple[str, ...]
    attribution: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    within_change: bool = False

    def label(self, label_id: str, symbols: Sequence[str] | None = None) -> dict[str, Any]:
        """The ``labels:`` entry for a corpus file, with *symbols* (default: the candidates) as chosen by a person."""

        return {
            "id": label_id,
            "summary": self.fix_subject,
            "symbols": list(self.symbols if symbols is None else symbols),
            "regions": [region.to_wire() for region in self.regions],
            "evidence": {
                "fix_commit": self.fix_commit,
                "introduced_by": self.introduced_by,
                "attribution": self.attribution,
                "derivation": (
                    "lines the fix edited, taken at the fix's parent and mapped back to the reviewed head "
                    "(git diff head..parent); fix_site regions are where the fix edited"
                    + (
                        ", kept only where the reviewed change itself added or changed the line"
                        if self.within_change
                        else ""
                    )
                ),
            },
        }


def derive_label(
    repo: Path, fix: str, introduced: str, *, head: str | None = None, pull: int = 0, within_change: bool = False
) -> Derivation:
    """Derive the label for *fix* fixing a bug that *introduced* shipped. *repo* needs the history between them.

    *pull* is the introducing pull request's number: a fix message that says "introduced in #N" names it.
    *within_change* keeps only fix sites that overlap lines the reviewed change (base..head) added or changed.
    """

    fix_sha = _git(repo, "rev-parse", f"{fix}^{{commit}}").strip()
    introduced_sha = _git(repo, "rev-parse", f"{introduced}^{{commit}}").strip()
    parent = _git(repo, "rev-parse", f"{fix_sha}^").strip()
    subject = _git(repo, "log", "-1", "--format=%s", fix_sha).strip()
    message = _git(repo, "log", "-1", "--format=%B", fix_sha)
    parents = _git(repo, "rev-list", "--parents", "-n1", introduced_sha).split()[1:]
    if not parents:
        raise DerivationError(f"{introduced_sha[:10]} is a root commit: there is nothing before it to review against")
    if len(parents) == 2:
        base, reviewed = parents
        change = set(_git(repo, "rev-list", f"{parents[0]}..{parents[1]}").split()) | {introduced_sha}
    else:
        base, reviewed = parents[0], introduced_sha
        change = {introduced_sha}
    if head:
        reviewed = _git(repo, "rev-parse", f"{head}^{{commit}}").strip()
    notes: list[str] = []
    regions: list[DerivedRegion] = []
    blamed_to_change = 0
    for (old_path, new_path), hunks in _hunks(repo, parent, fix_sha).items():
        if old_path != new_path:
            notes.append(f"{old_path}: renamed by the fix; skipped")
            continue
        if not is_source_path(old_path):
            continue
        drift = next(iter(_hunks(repo, reviewed, parent, old_path).values()), [])
        added: list[tuple[int, int]] = []
        if within_change:
            added = [
                (new_start, new_start + new_count - 1)
                for _o, _oc, new_start, new_count in next(iter(_hunks(repo, base, reviewed, old_path).values()), [])
                if new_count > 0
            ]
        for old_start, old_count, _new_start, _new_count in hunks:
            kind = "insertion" if old_count == 0 else "changed"
            first, last = (
                (max(old_start, 1), max(old_start, 1)) if old_count == 0 else (old_start, old_start + old_count - 1)
            )
            mapped = [_map_back(drift, line) for line in range(first, last + 1)]
            kept = [line for line in mapped if line is not None]
            if not kept:
                notes.append(
                    f"{old_path}:{first}-{last}: changed between the reviewed head and the fix's parent; skipped"
                )
                continue
            if within_change and not any(low <= max(kept) and min(kept) <= high for low, high in added):
                notes.append(f"{old_path}:{min(kept)}-{max(kept)}: not on a line the reviewed change added; skipped")
                continue
            in_change = 0
            if kind == "changed":
                blame = _git(repo, "blame", "--porcelain", "-L", f"{first},{last}", parent, "--", old_path)
                for header in re.finditer(r"^([0-9a-f]{40}) \d+ \d+", blame, re.MULTILINE):
                    if header.group(1) in change:
                        in_change += 1
                blamed_to_change += in_change
            regions.append(DerivedRegion(old_path, min(kept), max(kept), kind, in_change))
    if not regions:
        raise DerivationError(
            "the fix edits no source line that the reviewed change added and that is still as it was in the reviewed head"
            if within_change
            else "the fix edits no source line that is still as it was in the reviewed head"
        )
    names = {introduced_sha[:7], introduced_sha[:10], introduced_sha}
    named = any(name in message for name in names) or bool(pull and re.search(rf"(?<![\w#]){'#'}{pull}(?!\d)", message))
    if not named and blamed_to_change == 0:
        raise DerivationError(
            "neither the fix's message names the introducing change nor does git blame attribute any line the fix "
            "removed to it: this pair is not evidence of a regression"
        )
    attribution = "fix_message" if named else "blame"
    return Derivation(
        fix_commit=fix_sha,
        fix_subject=subject,
        introduced_by=introduced_sha,
        base_sha=base,
        head_sha=reviewed,
        fix_parent=parent,
        merge=len(parents) == 2,
        regions=tuple(regions),
        symbols=candidate_symbols(repo, parent, fix_sha),
        attribution=attribution,
        notes=tuple(notes),
        within_change=within_change,
    )


def candidate_symbols(repo: Path, parent: str, fix: str, limit: int = 8) -> tuple[str, ...]:
    """Identifiers on the lines the fix removed or added in source files, most frequent first: candidates, not a choice."""

    counts: Counter[str] = Counter()
    for _old_path, new_path in _hunks(repo, parent, fix):
        if not is_source_path(new_path):
            continue
        for line in _git(repo, "diff", "-U0", parent, fix, "--", new_path).splitlines():
            if line.startswith(("---", "+++")) or not line.startswith(("+", "-")):
                continue
            counts.update(word for word in _IDENT.findall(line[1:]) if len(word) >= 4 and word.lower() not in _NOISE)
    return tuple(word for word, _n in sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:limit])


def pr_entry(
    derivation: Derivation,
    *,
    label_id: str,
    repository: str,
    pull: int,
    title: str,
    license_id: str,
    symbols: Sequence[str] | None = None,
) -> Mapping[str, Any]:
    """A full ``prs:`` entry for the corpus, ready to paste, from a derivation and what a person knows about the repo."""

    return {
        "id": label_id,
        "repository": repository,
        "pull": pull,
        "title": title,
        "license": license_id,
        "provenance": {
            "source": "public_repository",
            "repository": f"https://github.com/{repository}",
            "commit": derivation.head_sha,
            "note": f"{title}; the bug is fixed by {derivation.fix_commit[:10]} ({derivation.fix_subject})",
        },
        "base_sha": derivation.base_sha,
        "head_sha": derivation.head_sha,
        "labelled": True,
        "labels": [derivation.label(label_id, symbols)],
    }


__all__ = [
    "Derivation",
    "DerivationError",
    "DerivedRegion",
    "candidate_symbols",
    "derive_label",
    "is_source_path",
    "pr_entry",
]
