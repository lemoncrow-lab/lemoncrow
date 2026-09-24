"""Test-contract weakening guard shared by every edit surface.

The reward hack this catches is an agent "fixing" a red test by deleting or
loosening an assertion, or marking the test skip/xfail, instead of fixing the
code. The guard trips only on that signal: additive edits (new tests, new
assertions) and in-place assertion changes pass freely. It is heuristic and
language-fuzzy by design and errs toward allowing. Operator off-switch:
``LEMONCROW_TEST_CONTRACT_GUARD=0``.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Container, Mapping, Sequence
from typing import Final

from .fsio import FileSnapshot

__all__ = [
    "classify_weakening",
    "detect_weakening",
    "guard_enabled",
    "looks_like_test_path",
    "weakening_message",
]

logger = logging.getLogger(__name__)

_ASSERTION_RE: Final[re.Pattern[str]] = re.compile(r"\bassert|\bexpect\s*\(|\bEXPECT_|\bASSERT_|\.should\b")
_SKIP_XFAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"@(?:pytest\.mark\.)?(?:skip|skipif|xfail)\b|@unittest\.skip|\bt\.Skip[a-zA-Z]*\("
    r"|\bxit\s*\(|\bxdescribe\s*\(|\.skip\s*\("
)
_TEST_DIRECTORIES: Final[frozenset[str]] = frozenset({"test", "tests", "spec", "specs", "__tests__"})


def guard_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the guard runs: on unless the operator switch turns it off."""
    env = os.environ if environ is None else environ
    return env.get("LEMONCROW_TEST_CONTRACT_GUARD", "").strip().lower() not in ("0", "false", "no", "off")


def looks_like_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    name = parts[-1]
    return (
        any(part in _TEST_DIRECTORIES for part in parts[:-1])
        or name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or ".spec." in name
    )


def classify_weakening(old_content: str, new_content: str) -> str | None:
    """Return why ``old -> new`` weakens a test contract, or ``None``.

    Weakening is a NET removal of assertion lines, or a NET addition of
    skip/xfail markers. Changing an expected value nets to zero and is not
    flagged; a purely additive edit is not flagged.
    """
    removed: list[str] = []
    added: list[str] = []
    for line in difflib.unified_diff(old_content.splitlines(), new_content.splitlines(), lineterm=""):
        if line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    removed_asserts = sum(1 for line in removed if _ASSERTION_RE.search(line))
    added_asserts = sum(1 for line in added if _ASSERTION_RE.search(line))
    if removed_asserts > added_asserts:
        return f"net removal of {removed_asserts - added_asserts} assertion(s)"
    added_skips = sum(1 for line in added if _SKIP_XFAIL_RE.search(line))
    removed_skips = sum(1 for line in removed if _SKIP_XFAIL_RE.search(line))
    if added_skips > removed_skips:
        return f"added {added_skips - removed_skips} skip/xfail marker(s)"
    return None


def detect_weakening(
    snapshots: Mapping[str, FileSnapshot],
    *,
    session_created: Container[str],
) -> list[dict[str, str]]:
    """Find edits to existing test files that weaken a test contract.

    A genuine contract change almost always rides with a production-code change
    in the same batch (the contract moved with the code); a reward hack weakens
    the test WITHOUT fixing code. So when the batch also changed a non-test
    file, weakening is allowed, and only a test-ONLY weakening is reported.
    Files that did not exist before the edit, and tests created this session
    (``session_created`` holds resolved paths), are the agent's own work in
    progress rather than a contract, and are skipped.
    """
    findings: list[dict[str, str]] = []
    changed_non_test = False
    for display, (path, _existed, old_content) in snapshots.items():
        try:
            new_content = path.read_text(encoding="utf-8") if path.exists() else None
        except (OSError, UnicodeDecodeError):
            logger.debug("contract guard could not read %s", path, exc_info=True)
            continue
        if not looks_like_test_path(display):
            if new_content != old_content:
                changed_non_test = True
            continue
        if old_content is None or str(path.resolve()) in session_created:
            continue
        if new_content is None or new_content == old_content:
            continue
        reason = classify_weakening(old_content, new_content)
        if reason:
            findings.append({"path": display, "reason": reason})
    if changed_non_test:
        return []
    return findings


def weakening_message(findings: Sequence[Mapping[str, str]]) -> str:
    """The refusal an agent sees when the guard rolls an edit back."""
    return (
        "Edit rolled back: it weakened an existing test contract ("
        + "; ".join(f"{finding['path']}: {finding['reason']}" for finding in findings)
        + "). This was a test-only edit -- fix the production code in the SAME edit so "
        "the original assertions pass (a genuine contract change that also edits code is "
        "allowed)."
    )
