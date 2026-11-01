"""``lc workspace``: one command over every repository under a workspace root.

The root is the folder the command runs in (``--root DIR`` to name another): a Git repository with its checked-out
submodules, or a folder whose immediate children are Git repositories (:mod:`.workspace_members`). The change
workflow lives in :mod:`.workspace_changes`, the cross-repository graph in :mod:`.workspace_graph`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, TextIO

__all__ = ["workspace_main"]

WORKSPACE_USAGE: Final[str] = """usage:
  lc workspace status [--json]                     branch, changes, ahead/behind and submodule drift per repository
  lc workspace branch NAME [--only REPO,...]       switch every changed repository (or those named) to branch NAME
  lc workspace commit -m MESSAGE [--only ...]      commit each changed repository, submodules first, then record
                                                   their new commits in the repository that holds them
  lc workspace push [--only ...]                   push each repository its upstream is behind, submodules first
  lc workspace pr [--title T] [--body B] [--draft] open one pull request per pushed repository (gh), linked together
  lc workspace graph [--json]                      dependency edges between the repositories, with evidence
  lc workspace impact [--base REF] [--json] [PATH ...]
                                                   the repositories a change reaches, and through which edges

Every command takes --root DIR (default: the current folder; a folder inside a repository means that repository).
"""

_CHANGE_ACTIONS: Final[frozenset[str]] = frozenset({"status", "branch", "commit", "push", "pr"})
_GRAPH_ACTIONS: Final[frozenset[str]] = frozenset({"graph", "impact"})


def workspace_main(rest: Sequence[str], sink: TextIO) -> int:
    if not rest or rest[0] in {"-h", "--help", "help"}:
        sink.write(WORKSPACE_USAGE)
        return 0 if rest else 2
    action, arguments = rest[0], list(rest[1:])
    if action in _CHANGE_ACTIONS:
        from .workspace_changes import run as run_changes

        return run_changes(action, arguments, sink)
    if action in _GRAPH_ACTIONS:
        from .workspace_graph import run as run_graph

        return run_graph(action, arguments, sink)
    sink.write(f"unknown workspace command: {action}\n")
    sink.write(WORKSPACE_USAGE)
    return 2
