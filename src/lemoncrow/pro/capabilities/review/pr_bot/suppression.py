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

What can be suppressed is bounded by two rules. A violation of a rule the repository wrote (category ``critical_rule``)
is never held back by feedback: that rule is the trusted base's, and a comment on a pull request is not allowed to
loosen it; change the rule instead. And a must-fix (P0) is held back only by a suppression whose giver may dismiss one
(:attr:`Suppression.dismisses_p0`): an owner, member or collaborator who did not open the pull request. Feedback from
the author, from the bot's own account and from anyone with less standing is still recorded, and still holds back the
findings that are not P0, but it leaves a P0 open, because with ``fail-on: p0`` an author who could dismiss the finding
that blocks their own merge would hold the key to their own gate.
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
#: The severity of a must-fix (``findings.SEVERITIES``).
MUST_FIX: Final[str] = "p0"
#: The ``author_association`` values that may dismiss a must-fix, when they are also on the feedback allow-list
#: (``chat-allowed-associations``). ``CONTRIBUTOR`` is deliberately not here, though it is on that list by default: it
#: means only "has a commit in this repository", with no write access, so it cannot merge, approve a required review or
#: change what a check blocks. Letting it dismiss a P0 would let an outside contributor, or a friend of one, clear a
#: must-fix that someone with write access has not looked at. Such a person still teaches the bot about P1 and P2.
P0_DISMISSERS: Final[frozenset[str]] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
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
    #: Whether the person behind it may dismiss a must-fix (:data:`P0_DISMISSERS`, and not the pull request's author).
    #: Without it the suppression holds back P1 and P2 findings and leaves a P0 open (:meth:`declines`).
    dismisses_p0: bool = False

    @property
    def explicit(self) -> bool:
        """Whether a person said it in words (a command, a reply, a note), not only with a reaction or a resolve."""

        return self.source in {"command", "reply"} or bool(self.note)

    def matches(self, finding: BotFinding) -> bool:
        """Whether *finding* is one this suppression holds back. A rule violation never is, and a P0 only if its giver may."""

        return self.covers(finding) and (self.dismisses_p0 or finding.severity != MUST_FIX)

    def declines(self, finding: BotFinding) -> bool:
        """Whether *finding* is a P0 this suppression is about and may not hold back: it is recorded, and the P0 stays."""

        return finding.severity == MUST_FIX and not self.dismisses_p0 and self.covers(finding)

    def covers(self, finding: BotFinding) -> bool:
        """Whether *finding* is what this suppression is about, whatever its severity. A rule violation never is."""

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
            ("p0", self.dismisses_p0),
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
    dismisses_p0: bool = False,
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
        dismisses_p0=dismisses_p0,
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
        # A row written before the flag existed, or with anything but `true`, cannot hold back a P0.
        dismisses_p0=raw.get("p0") is True,
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
    """The suppressions after adding *added*: one per id (the first said wins), the newest :data:`MAX_SUPPRESSIONS`.

    The exception is a row that may hold back a must-fix: when the same instruction was first given by someone who may
    not (the author, say) and is given again by someone who may, the second is the one kept, in the first's place.
    """

    rows: dict[str, Suppression] = {}
    for row in (*existing, *added):
        held = rows.get(row.id)
        if held is None or (row.dismisses_p0 and not held.dismisses_p0):
            rows[row.id] = row
    return tuple(list(rows.values())[-MAX_SUPPRESSIONS:])


__all__ = [
    "CATEGORIES",
    "MAX_SUPPRESSIONS",
    "MUST_FIX",
    "P0_DISMISSERS",
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
