"""What the bot has been told not to flag on this pull request (G9).

A person's feedback becomes a :class:`Suppression` kept in the summary's hidden state, and takes effect on the spot
for this pull request: the finding it came from, and any finding a later push reports again, is held back
(``suppressed``) instead of being posted or counted. Three signals make one, and an explicit command makes a fourth:

* ``reaction``: a thumbs-down on the bot's inline comment;
* ``resolved``: the thread was resolved by a person while the finding still stands and its code has not changed;
* ``reply``: a reply in the thread that says it is not a bug, will not be fixed, is intentional, a duplicate, ...;
* ``command``: ``@lemoncrow ignore <category or words> [in <path glob>]``.

A suppression is scoped to this pull request and nothing else. It never changes the repository: wider learning is a
*suggested rule* the summary offers as YAML to copy into ``.lemoncrow/review.yaml`` (:mod:`learning`), which the
Action reads from the merge base, so it governs reviews only once a person has merged it.

What can be suppressed is bounded by one rule: a violation of a rule the repository wrote (category ``critical_rule``)
is never held back by feedback. That rule is the trusted base's, and a comment on a pull request is not allowed to
loosen it; change the rule instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.pr_bot.findings import BotFinding

SOURCES: Final[tuple[str, ...]] = ("reaction", "resolved", "reply", "command")
#: The vocabulary of a Review's finding dismissal (``session_models.FINDING_DISMISSAL_REASONS``), reused as is.
REASONS: Final[tuple[str, ...]] = ("", "incorrect", "accepted_risk", "duplicate", "not_applicable")
#: A dismissal reason as it reads in a sentence.
REASON_WORDS: Final[dict[str, str]] = {
    "incorrect": "not a bug",
    "accepted_risk": "intended or an accepted risk",
    "duplicate": "a duplicate",
    "not_applicable": "not applicable",
    "": "not wanted",
}
#: Categories a finding can have (``ai_review.FindingCategory``); ``critical_rule`` is the one feedback cannot silence.
CATEGORIES: Final[tuple[str, ...]] = (
    "correctness",
    "security",
    "api_compatibility",
    "concurrency_state",
    "persistence_migration",
    "performance",
    "verification",
    "critical_rule",
)
PROTECTED: Final[frozenset[str]] = frozenset({"critical_rule"})
#: What people write for a category, to the one it means.
CATEGORY_WORDS: Final[dict[str, str]] = {
    "bug": "correctness",
    "bugs": "correctness",
    "logic": "correctness",
    "auth": "security",
    "api": "api_compatibility",
    "compatibility": "api_compatibility",
    "concurrency": "concurrency_state",
    "race": "concurrency_state",
    "races": "concurrency_state",
    "state": "concurrency_state",
    "persistence": "persistence_migration",
    "migration": "persistence_migration",
    "migrations": "persistence_migration",
    "perf": "performance",
    "test": "verification",
    "tests": "verification",
    "testing": "verification",
    "coverage": "verification",
}
#: At most this many are kept; the oldest go first. Each is a few hundred bytes of the 65,536-character summary.
MAX_SUPPRESSIONS: Final[int] = 40
MAX_NOTE: Final[int] = 240
MAX_PATTERN: Final[int] = 80
MAX_GLOBS: Final[int] = 4
_GLOB = re.compile(r"[A-Za-z0-9_./*?\[\]{}@+~=,-]{1,200}")
_UNSAFE = re.compile(r"[<>`\x00-\x1f\x7f\u2028\u2029]")


def fragment(text: str, limit: int) -> str:
    """*text* on one line, fit to sit inside a code fence, a YAML scalar or a comment: no angle bracket (so no HTML
    comment that could forge a marker), no backtick, no control character, no image or link syntax, cut to *limit*
    with an ellipsis."""

    # (`![` and `](` are what make an image or a link target of text; a space makes them words)
    plain = " ".join(_UNSAFE.sub(" ", text).replace("![", "! [").replace("](", "] (").split())
    return plain if len(plain) <= limit else plain[: limit - 1].rstrip() + "…"


def category_named(text: str) -> str:
    """The category a person's words name (``security``, ``concurrency``, ``Persistence Migration``), else ``""``."""

    word = re.sub(r"[\s-]+", "_", text.strip().strip("`'\"").lower())
    if word in CATEGORIES:
        return word
    return CATEGORY_WORDS.get(word.replace("_", ""), CATEGORY_WORDS.get(word, ""))


_CATEGORY_LABEL: Final[dict[str, str]] = {
    "api_compatibility": "API compatibility",
    "concurrency_state": "concurrency and state",
    "persistence_migration": "persistence and migration",
}


def category_label(category: str) -> str:
    """A category as a person says it."""

    return _CATEGORY_LABEL.get(category, category.replace("_", " "))


def safe_glob(text: str) -> str:
    """A path glob a person wrote, or ``""`` if it is not one: repository-relative, no ``..``, no odd characters."""

    glob = text.strip().strip("`'\"").rstrip(".,;:")
    if not _GLOB.fullmatch(glob) or glob.startswith("/") or ".." in glob.split("/"):
        return ""
    return glob


@dataclass(frozen=True, slots=True)
class Suppression:
    """One thing the bot will not flag on this pull request, how it was learned and from whom.

    Exactly one of ``fingerprint`` (the very concern a finding raised), ``category`` or ``pattern`` (words in a
    finding's title or explanation) says *what*; ``paths`` (globs) narrows *where*, and is empty for "anywhere".
    ``path`` and ``category`` of a fingerprint suppression are only what the finding it came from sat on, for the
    rule it may become.
    """

    id: str
    source: str
    fingerprint: str = ""
    category: str = ""
    pattern: str = ""
    paths: tuple[str, ...] = ()
    path: str = ""
    reason: str = ""
    note: str = ""
    by: str = ""
    title: str = ""
    thread_id: int = 0
    added_in: str = ""

    @property
    def explicit(self) -> bool:
        """Whether a person said it in words (a command, a reply, a note), not only with a reaction or a resolve."""

        return self.source in {"command", "reply"} or bool(self.note)

    def matches(self, finding: BotFinding) -> bool:
        """Whether *finding* is one this suppression holds back. A rule violation never is."""

        if finding.category in PROTECTED:
            return False
        if self.fingerprint:
            return bool(finding.fingerprint) and finding.fingerprint == self.fingerprint
        if self.paths and not _in_paths(finding.path, self.paths):
            return False
        if self.category:
            return finding.category == self.category
        if self.pattern:
            return self.pattern.lower() in f"{finding.title} {finding.explanation}".lower()
        return False

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"id": self.id, "source": self.source}
        for key, value in (
            ("fingerprint", self.fingerprint),
            ("category", self.category),
            ("pattern", self.pattern),
            ("paths", list(self.paths)),
            ("path", self.path),
            ("reason", self.reason),
            ("note", self.note),
            ("by", self.by),
            ("title", self.title),
            ("thread_id", self.thread_id),
            ("added_in", self.added_in),
        ):
            if value:
                wire[key] = value
        return wire


def _in_paths(path: str, globs: tuple[str, ...]) -> bool:
    from lemoncrow.pro.capabilities.review.rules import path_matches

    try:
        return path_matches(path, globs)
    except ValueError:  # a path the rule loader would not accept matches no scope
        return False


def suppression_id(*, fingerprint: str = "", category: str = "", pattern: str = "", paths: tuple[str, ...] = ()) -> str:
    """Deterministic, so the same instruction given twice is one suppression."""

    document = json.dumps([fingerprint, category, pattern.lower(), sorted(paths)], separators=(",", ":"))
    return "sup-" + hashlib.sha256(document.encode("utf-8")).hexdigest()[:10]


def make_suppression(
    source: str,
    *,
    fingerprint: str = "",
    category: str = "",
    pattern: str = "",
    paths: tuple[str, ...] = (),
    path: str = "",
    reason: str = "",
    note: str = "",
    by: str = "",
    title: str = "",
    thread_id: int = 0,
    added_in: str = "",
) -> Suppression:
    """A suppression with its text made safe and its id derived from what it holds back."""

    clean_paths = tuple(dict.fromkeys(glob for glob in (safe_glob(p) for p in paths) if glob))[:MAX_GLOBS]
    clean_pattern = fragment(pattern, MAX_PATTERN)
    return Suppression(
        id=suppression_id(fingerprint=fingerprint, category=category, pattern=clean_pattern, paths=clean_paths),
        source=source,
        fingerprint=fingerprint,
        category=category,
        pattern=clean_pattern,
        paths=clean_paths,
        path=fragment(path, 300),
        reason=reason if reason in REASONS else "",
        note=fragment(note, MAX_NOTE),
        by=fragment(by, 40),
        title=fragment(title, 160),
        thread_id=thread_id,
        added_in=added_in[:12],
    )


def suppression_from_wire(raw: object) -> Suppression | None:
    """A suppression from a state row, or ``None`` when the row is not a well-formed one (the state is untrusted text)."""

    if not isinstance(raw, dict):
        return None
    source = raw.get("source")
    identity = raw.get("id")
    if source not in SOURCES or not isinstance(identity, str) or not 0 < len(identity) <= 40:
        return None

    def text(key: str, limit: int) -> str:
        value = raw.get(key)
        return value[:limit] if isinstance(value, str) else ""

    paths = raw.get("paths")
    thread = raw.get("thread_id")
    reason = raw.get("reason")
    suppression = Suppression(
        id=identity,
        source=str(source),
        fingerprint=text("fingerprint", 80),
        category=text("category", 40) if text("category", 40) in CATEGORIES else "",
        pattern=text("pattern", MAX_PATTERN),
        paths=(
            tuple(item[:200] for item in paths[:MAX_GLOBS] if isinstance(item, str)) if isinstance(paths, list) else ()
        ),
        path=text("path", 300),
        reason=reason if isinstance(reason, str) and reason in REASONS else "",
        note=text("note", MAX_NOTE),
        by=text("by", 40),
        title=text("title", 160),
        thread_id=thread if isinstance(thread, int) and not isinstance(thread, bool) and thread > 0 else 0,
        added_in=text("added_in", 12),
    )
    # A row that holds nothing back is not a suppression.
    return suppression if suppression.fingerprint or suppression.category or suppression.pattern else None


def apply(findings: Sequence[BotFinding], suppressions: Sequence[Suppression], head: str) -> list[BotFinding]:
    """*findings* with every open one a suppression matches set to ``suppressed``, naming the suppression.

    The first match in the order given wins, and a finding already closed is left alone.
    """

    held: list[BotFinding] = []
    for finding in findings:
        match = next((s for s in suppressions if finding.is_open and s.matches(finding)), None)
        if match is None:
            held.append(finding)
        else:
            held.append(finding.changed(state="suppressed", suppressed_by=match.id, last_seen=head, resolved_in=head))
    return held


def merge(existing: Sequence[Suppression], added: Sequence[Suppression]) -> tuple[Suppression, ...]:
    """The suppressions after adding *added*: one per id (the first said wins), the newest :data:`MAX_SUPPRESSIONS`."""

    rows: dict[str, Suppression] = {}
    for row in (*existing, *added):
        rows.setdefault(row.id, row)
    return tuple(list(rows.values())[-MAX_SUPPRESSIONS:])


__all__ = [
    "CATEGORIES",
    "MAX_SUPPRESSIONS",
    "PROTECTED",
    "REASON_WORDS",
    "SOURCES",
    "Suppression",
    "apply",
    "category_label",
    "category_named",
    "fragment",
    "make_suppression",
    "merge",
    "safe_glob",
    "suppression_from_wire",
    "suppression_id",
]
