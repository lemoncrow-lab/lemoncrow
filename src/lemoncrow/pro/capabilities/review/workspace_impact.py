"""Which other repositories of the reviewer's workspace a change reaches (P4 of
docs/planning/2026-11-01-multi-repo-workspace.md, section 11.4).

A reviewed repository belongs to a workspace when it is one of several member repositories of:

1. ``LEMONCROW_WORKSPACE_ROOT``, when it is set and lists the repository as a member;
2. else the folder holding the repository (or, for a submodule, its outermost superproject), when that folder is
   not itself inside a repository;
3. else its outermost superproject, when that has submodules.

The cross-repository graph (``lemoncrow_client.workspace_graph``) then says which other members depend on what
changed. The answer is the packet's ``workspace_impact`` slice: it is shown as its own section after the findings,
is never a finding and never raises one. A repository with no workspace has an empty slice and no degraded name; a
scan that fails names ``workspace_impact_failed``, and one that could not see every member names
``workspace_impact_partial``.

The scan reads every member's tracked files (a few seconds on a large folder), so it runs on a thread of its own
while the rest of the packet is built (:class:`PendingWorkspaceImpact`), and only where a person asked for a review
(``lc review``, ``lc review --ai`` at the command), never in a git hook or on a pull request.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

WORKSPACE_ENV: Final[str] = "LEMONCROW_WORKSPACE_ROOT"
FAILED: Final[str] = "workspace_impact_failed"
PARTIAL: Final[str] = "workspace_impact_partial"
UNAVAILABLE: Final[str] = "workspace_impact_unavailable"
#: Reached repositories shown without ``--all``, and evidence links shown per edge.
SHOWN_REPOSITORIES: Final[int] = 5
SHOWN_LINKS: Final[int] = 2
_GIT_TIMEOUT_S: Final[float] = 10.0
#: Superprojects followed upwards; deeper nesting is not a workspace anyone builds.
_MAX_NESTING: Final[int] = 16
#: Paths and names come from other repositories' files: no control or escape characters reach a terminal.
_CONTROL: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _git(cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _outermost_superproject(repo: Path) -> Path:
    top = repo
    for _ in range(_MAX_NESTING):
        parent = _git(top, "rev-parse", "--show-superproject-working-tree")
        if not parent:
            break
        top = Path(parent).resolve()
    return top


def workspace_for(repo_root: Path, environ: Mapping[str, str] | None = None) -> tuple[Path, str] | None:
    """``(workspace root, the repository's member path in it)``, or ``None`` when it has no workspace."""

    from lemoncrow_client.workspace_members import discover_members, git_toplevel, resolve_root

    env = os.environ if environ is None else environ
    repo = repo_root.resolve()
    candidates: list[Path] = []
    explicit = env.get(WORKSPACE_ENV, "").strip()
    if explicit:
        candidates.append(resolve_root(Path(explicit)))
    top = _outermost_superproject(repo)
    folder = top.parent
    if folder != top and git_toplevel(folder) is None:
        candidates.append(folder)
    candidates.append(top)
    seen: set[Path] = set()
    for root in candidates:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        membership = discover_members(root)
        member = membership.member(repo)
        if member is not None and len(membership.members) > 1:
            return root, member.relpath
    return None


def _submodule_changes(root: Path, member: str, pointer_path: str, old: str, new: str) -> tuple[str, list[str]]:
    """The submodule member a gitlink change moved, and the files the move changed (empty when unknown)."""

    relpath = pointer_path if member == "." else f"{member}/{pointer_path}"
    checkout = root / relpath
    if not old or not new or not (checkout / ".git").exists():
        return relpath, []
    listed = _git(checkout, "diff", "--name-only", "--no-renames", old, new, "--")
    return relpath, [line for line in (listed or "").splitlines() if line]


def changed_by_member(
    root: Path, member: str, files: Iterable[tuple[str, str | None, tuple[str, str] | None]]
) -> dict[str, list[str]]:
    """``{member: [member-relative files]}`` from ``(path, old_path, submodule_pointer)`` rows of a packet.

    A renamed file changed at both paths. A submodule pointer that moved changed the submodule's own files between
    the two commits, when its checkout has both.
    """

    changed: dict[str, list[str]] = {}
    for path, old_path, pointer in files:
        if pointer is not None:
            submodule, moved = _submodule_changes(root, member, path, *pointer)
            if moved:
                changed.setdefault(submodule, []).extend(moved)
            continue
        changed.setdefault(member, []).extend(item for item in (path, old_path) if item)
    return changed


def collect_workspace_impact(
    repo_root: Path,
    files: Sequence[tuple[str, str | None, tuple[str, str] | None]],
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """``(slice, degraded names)`` for a change to ``repo_root``; never raises."""

    if not files:
        return {}, ()
    try:
        from lemoncrow_client.workspace_graph import impact
    except ImportError:
        return {}, (UNAVAILABLE,)
    try:
        found = workspace_for(repo_root, environ)
        if found is None:
            return {}, ()
        root, member = found
        report = impact(root, changed_by_member(root, member, files))
    except Exception:  # an enrichment: a broken scan costs a degraded name, never the review
        return {}, (FAILED,)
    payload = {"repo": member, **report.to_dict()}
    partial = bool(report.errors or report.incomplete or report.members_truncated)
    return payload, ((PARTIAL,) if partial else ())


class PendingWorkspaceImpact:
    """:func:`collect_workspace_impact` on a daemon thread, so its scan overlaps the rest of the packet build.

    A daemon thread, not a pool: a build that fails must not wait for the scan before its error is reported.
    """

    def __init__(
        self,
        repo_root: Path,
        files: Sequence[tuple[str, str | None, tuple[str, str] | None]],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._result: tuple[dict[str, Any], tuple[str, ...]] = ({}, (FAILED,))
        rows = tuple(files)
        env = dict(os.environ if environ is None else environ)

        def run() -> None:
            self._result = collect_workspace_impact(repo_root, rows, environ=env)

        # Not `_thread`: mypyc names the C struct field `__thread`, which is a C keyword.
        self._scan = threading.Thread(target=run, name="lc-review-workspace-impact", daemon=True)
        self._scan.start()

    def result(self) -> tuple[dict[str, Any], tuple[str, ...]]:
        self._scan.join()
        return self._result


def packet_rows(files: Iterable[Any]) -> list[tuple[str, str | None, tuple[str, str] | None]]:
    """``(path, old_path, submodule_pointer)`` of each changed file (:class:`models.ChangedFile`)."""

    return [(item.path, item.old_path, item.submodule_pointer) for item in files]


def reader_payload(packet: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored packet's slice as the Review Reader receives it; ``{}`` when it has none.

    Every value but the source lines: each link's ``detail`` (a line of another repository's file) is left out.
    """

    found = packet.get("workspace_impact") if isinstance(packet, Mapping) else None
    if not isinstance(found, Mapping) or not found:
        return {}
    payload = dict(found)
    reached: list[Any] = []
    for hit in found.get("reached") or []:
        if not isinstance(hit, Mapping):
            continue
        edges: list[Any] = []
        for edge in hit.get("edges") or []:
            if not isinstance(edge, Mapping):
                continue
            links = [
                {
                    **link,
                    **{
                        side: {key: value for key, value in link[side].items() if key != "detail"}
                        for side in ("provider", "consumer")
                        if isinstance(link.get(side), Mapping)
                    },
                }
                for link in edge.get("links") or []
                if isinstance(link, Mapping)
            ]
            edges.append({**edge, "links": links})
        reached.append({**hit, "edges": edges})
    payload["reached"] = reached
    return payload


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #


def _home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + os.sep) else path


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def heading(payload: Mapping[str, Any]) -> str:
    """One line naming the workspace and this repository's place in it."""

    reached = payload.get("reached") or []
    where = f"{_home(str(payload.get('root', '')))}, this repository is {payload.get('repo', '.')}"
    if not reached:
        return _CONTROL.sub("", f"no other repository depends on what changed ({where})")
    count = len(reached)
    return _CONTROL.sub("", f"{count} other {'repository' if count == 1 else 'repositories'} can be affected ({where})")


def impact_lines(payload: Mapping[str, Any], *, show_all: bool = False) -> list[str]:
    """The section body, unindented: one line per reached repository, its strongest evidence, and the gaps.

    No source line is quoted (each link's ``detail`` is left out): every value is a path, a name or a line number
    from the scan, with control characters removed.
    """

    lines: list[str] = []
    reached = list(payload.get("reached") or [])
    visible = reached if show_all else reached[:SHOWN_REPOSITORIES]
    for hit in visible:
        hops = int(hit.get("hops", 1))
        level = "direct" if hit.get("direct") else "repository-level"
        via = " -> ".join(str(step) for step in hit.get("path") or [])
        lines.append(
            f"{hit.get('repo')}  {_plural(hops, 'hop')}  {hit.get('confidence')}  {level}"
            + (f"  via {via}" if hops > 1 else "")
        )
        for edge in hit.get("edges") or []:
            links = list(edge.get("links") or [])
            shown = links if show_all else links[:SHOWN_LINKS]
            for link in shown:
                consumer = link.get("consumer") or {}
                provider = link.get("provider") or {}
                lines.append(f"  {edge.get('kind')} {link.get('key')}: {_where(consumer)} uses {_where(provider)}")
            more = len(links) - len(shown) + int(edge.get("more") or 0)
            if more > 0:
                lines.append(f"  ... {more} more {edge.get('kind')} evidence")
    if len(visible) < len(reached):
        lines.append(f"... {len(reached) - len(visible)} more (--all lists every one)")
    for error in payload.get("errors") or []:
        lines.append(f"partial: {error.get('repo')}: changes not listed")
    for repo in payload.get("incomplete") or []:
        lines.append(f"partial: {repo} was not fully scanned; reach through it may be missing")
    if payload.get("members_truncated"):
        lines.append("partial: the repository bound was reached; later repositories were not scanned")
    if reached:
        lines.append("Not findings: repositories that depend on what changed, for a person to check.")
    return [_CONTROL.sub("", line) for line in lines]


def _where(evidence: Mapping[str, Any]) -> str:
    path = str(evidence.get("path", ""))
    line = int(evidence.get("line") or 0)
    return f"{path}:{line}" if line else path


__all__ = [
    "FAILED",
    "PARTIAL",
    "UNAVAILABLE",
    "WORKSPACE_ENV",
    "PendingWorkspaceImpact",
    "changed_by_member",
    "collect_workspace_impact",
    "heading",
    "impact_lines",
    "packet_rows",
    "reader_payload",
    "workspace_for",
]
