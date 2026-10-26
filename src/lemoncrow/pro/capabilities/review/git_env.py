"""The environment for running git against a repository chosen by path, not by what the caller inherited.

A process started by a git hook, ``git rebase -x`` or ``git bisect run`` carries ``GIT_DIR`` and its relatives. Left in
the environment of a child ``git`` they win over ``-C``/``cwd``, so a command meant for a throwaway clone runs on the
caller's own repository: ``git init`` rewrites its config, ``git remote set-url`` repoints its origin, ``git commit``
adds to its history. Code that runs git against any repository but the caller's builds its environment here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

#: The variables that tell git which repository, work tree, index or object store to use.
REPOSITORY_LOCATION_VARIABLES: Final[tuple[str, ...]] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_PREFIX",
)


def git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` without the repository-location variables, then *extra* (which may set one on purpose)."""

    env = {key: value for key, value in os.environ.items() if key not in REPOSITORY_LOCATION_VARIABLES}
    env.update(extra or {})
    return env


__all__ = ["REPOSITORY_LOCATION_VARIABLES", "git_environment"]
