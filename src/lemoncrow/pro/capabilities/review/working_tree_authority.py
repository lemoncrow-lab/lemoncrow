"""Where a developer's own Review configuration is read from: the working tree.

A pull request review reads ``.lemoncrow/review.yaml``, the rules files it imports and ``AGENTS.md`` from the **trusted
base**, because the pull request's author must not be able to loosen the review of their own change. A review of the
change in *your own checkout* has no such author: the person who runs ``lc review --ai`` is the person who wrote the
configuration, and a file they just edited and have not committed is the one they expect to apply. So a local review
(``capture_range`` with no pull request) hands the Review API this reader, and the API takes the configuration documents
from it instead of from the base commit. Nothing about a pull request, the Action or a hosted Review reaches it.

Standard library only; it never writes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

REVIEW_CONFIG = ".lemoncrow/review.yaml"
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_LISTED_PATHS = 200_000


@dataclass(frozen=True)
class WorkingTreeAuthority:
    """The configuration documents of the checkout at *repo_root*, as they are on disk now."""

    repo_root: Path

    def document(self, path: str) -> TrustedBaseDocument:
        """The file at repository-relative *path*: ``present``, ``absent``, or ``provisional`` when it cannot be read."""

        source = f"your working tree {path}"
        root = self.repo_root.resolve()
        try:
            target = (root / path).resolve()
            inside = target.is_relative_to(root)
        except (OSError, ValueError):
            return TrustedBaseDocument(path=path, content="", state="absent", source=source)
        if not inside or not target.is_file():
            return TrustedBaseDocument(path=path, content="", state="absent", source=source)
        try:
            if target.stat().st_size > MAX_DOCUMENT_BYTES:
                return TrustedBaseDocument(path=path, content="", state="provisional", source=source)
            content = target.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return TrustedBaseDocument(path=path, content="", state="provisional", source=source)
        if not content:
            return TrustedBaseDocument(path=path, content="", state="absent", source=source)
        return TrustedBaseDocument(path=path, content=content, state="present", source=source)

    def paths(self) -> list[str] | None:
        """Repository files (tracked, and untracked that are not ignored), for expanding ``import.rules_files`` globs."""

        listed = subprocess.run(
            ["git", "-C", str(self.repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
        if listed.returncode != 0:
            return None
        names = listed.stdout.decode("utf-8", errors="replace").split("\0")
        return [name for name in names[:MAX_LISTED_PATHS] if name]


def config_line(repo_root: Path) -> tuple[str, dict[str, object]]:
    """``(one line for the review output, the same as data)``: which configuration this review used and whether it is committed."""

    present = WorkingTreeAuthority(repo_root).document(REVIEW_CONFIG).state == "present"
    if not present:
        return (
            f"Config: no {REVIEW_CONFIG} in this working tree; built-in defaults.",
            {"source": "defaults", "path": REVIEW_CONFIG, "state": "absent", "committed": False},
        )
    committed = _committed_unchanged(repo_root, REVIEW_CONFIG)
    tail = "." if committed else " (not committed yet: pull request reviews read the committed copy)."
    return (
        f"Config: {REVIEW_CONFIG} from your working tree{tail}",
        {"source": "working_tree", "path": REVIEW_CONFIG, "state": "present", "committed": committed},
    )


def _committed_unchanged(repo_root: Path, path: str) -> bool:
    """Whether *path* is tracked and identical to ``HEAD``'s copy."""

    base = ["git", "-C", str(repo_root)]
    tracked = subprocess.run([*base, "ls-files", "--error-unmatch", "--", path], capture_output=True, check=False)
    if tracked.returncode != 0:
        return False
    return (
        subprocess.run([*base, "diff", "--quiet", "HEAD", "--", path], capture_output=True, check=False).returncode == 0
    )


__all__ = ["MAX_DOCUMENT_BYTES", "REVIEW_CONFIG", "WorkingTreeAuthority", "config_line"]
