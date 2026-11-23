"""A change that edits the review policy cannot be approved by whoever it helps (SPEC 4.19, P7-3).

Every Review is judged against the base branch's policy, never the change's own copy. A change that edits the
rules file, CODEOWNERS or the allowed signers is therefore a decision of its own (its parts are always decisions),
is said so on its row, Overview and Wrap up, and may be approved only by a CODEOWNER of ``.lemoncrow/**`` who is not
the author. Pure rules here; the composition that knows its people (hosted only, SPEC 7.1 P0-2) supplies the owners.
A local install has one reviewer and no identities, so nothing gates there: the banner and the decision still show.
"""

from __future__ import annotations

import hashlib
import hmac
import posixpath
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lemoncrow.pro.capabilities.review.second_reviewer import OwnerRef

#: The files a change may not use to loosen its own review: the rules file, who owns what, and who may sign.
POLICY_FILES: Final[tuple[str, ...]] = (
    ".lemoncrow/review.yaml",
    ".lemoncrow/allowed_signers",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)
#: The owners SPEC names for the gate.
OWNER_SCOPE: Final[str] = ".lemoncrow/**"


def is_policy_path(path: str) -> bool:
    return posixpath.normpath(path.replace("\\", "/").lstrip("/")) in POLICY_FILES


def policy_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """The policy files among *paths*, once each, in the order of :data:`POLICY_FILES`."""

    present = {posixpath.normpath(path.replace("\\", "/").lstrip("/")) for path in paths}
    return tuple(path for path in POLICY_FILES if path in present)


@dataclass(frozen=True, slots=True)
class PolicyGate:
    """May this viewer approve a change that edits the policy?"""

    allowed: bool
    reason: str = ""
    needs: tuple[OwnerRef, ...] = ()
    #: Who the approval is signed as, when the viewer may approve (SPEC 4.19: "signed by Maya Chen, CODEOWNER of ...").
    signer: OwnerRef | None = None
    #: The scope the signer owns, in the words of the reason.
    scope: str = ""

    def to_wire(self, asked: Iterable[str] = (), review_id: str = "", token_key: bytes = b"") -> dict[str, object]:
        """``asked`` is the principals this Review has already asked. ``key`` is an opaque token, per Review, that
        names a person to the ask route: a reader of this document learns no principal id from it."""

        taken = set(asked)
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "needs": [
                {
                    "name": owner.name,
                    "handle": owner.handle,
                    "key": owner_token(review_id, owner.principal_id, token_key) if review_id else "",
                    "asked": owner.principal_id in taken,
                }
                for owner in self.needs
            ],
            "signer": (
                None
                if self.signer is None
                else {"name": _label(self.signer), "handle": self.signer.handle, "scope": self.scope}
            ),
        }


def owner_token(review_id: str, principal_id: str, key: bytes = b"") -> str:
    """An opaque, stable name for one CODEOWNER on one Review (the ask route's key): not their principal id. Keyed (HMAC),
    so it cannot be linked to a candidate principal id without the server's secret."""

    return hmac.new(key, f"policy-ask\0{review_id}\0{principal_id}".encode(), hashlib.sha256).hexdigest()[:24]


def _label(owner: OwnerRef) -> str:
    return owner.name or (f"@{owner.handle}" if owner.handle else owner.principal_id)


def required_scope(path: str) -> str:
    """The CODEOWNER scope that governs one edited policy file: `.lemoncrow/**` for the rules file and the signers."""

    return (
        OWNER_SCOPE
        if posixpath.normpath(path.lstrip("/")).startswith(".lemoncrow/")
        else posixpath.normpath(path.lstrip("/"))
    )


def evaluate_gate(
    owners: Mapping[str, Sequence[OwnerRef]], *, viewer: str, barred: Iterable[str], files: Iterable[str]
) -> PolicyGate:
    """Allowed only for someone who is an eligible owner of EVERY edited policy file (decided file by file).

    Owning one edited file does not let you approve another, even one under the same `.lemoncrow/**` scope. A barred
    person (the author, an authoring agent) is never eligible. Fails closed: no edited file named, or a file nobody
    eligible owns, means nobody may approve. The scope is only the words of the reason.
    """

    blocked = set(barred)
    edited = list(dict.fromkeys(files))
    if not edited:
        return PolicyGate(False, "You can't approve this: which policy files it edits can't be checked")
    eligible: dict[str, dict[str, OwnerRef]] = {}
    for path in edited:
        eligible[path] = {
            owner.principal_id: owner
            for owner in owners.get(path, ())
            if owner.principal_id and owner.principal_id not in blocked
        }
    short = [path for path in edited if viewer in blocked or viewer not in eligible[path]]
    if not short:
        mine = next((eligible[path][viewer] for path in edited if viewer in eligible[path]), None)
        return PolicyGate(True, signer=mine, scope=" and ".join(dict.fromkeys(required_scope(path) for path in edited)))
    named = " and ".join(dict.fromkeys(required_scope(path) for path in short))
    if any(not eligible[path] for path in short):
        return PolicyGate(False, f"You can't approve this: no CODEOWNER of {named} is available to approve it")
    people: dict[str, OwnerRef] = {}
    for path in short:
        people.update(eligible[path])
    needs = tuple(sorted(people.values(), key=lambda owner: (_label(owner), owner.principal_id)))
    names = ", ".join(_label(owner) for owner in needs)
    return PolicyGate(False, f"You can't approve this: needs {names} (CODEOWNER of {named})", needs)


_ITEM: Final[re.Pattern[str]] = re.compile(r"^[ +-](\s*)- +(?P<value>[^#\n]+?)\s*(?:#.*)?$")
_KEY: Final[re.Pattern[str]] = re.compile(r"^[ +-](\s*)(?P<key>[A-Za-z_][\w.-]*):\s*(?:#.*)?$")


def _unquote(value: str) -> str:
    return value.strip().strip("'\"")


def diff_question(path: str, patch: str) -> str:
    """The policy file's own diff, asked as a question (SPEC 4.19: "Remove `app/payments/**` from always a decision?").

    Read from the unified diff text only. In the rules file, a removed (or added) list item is asked about under the
    key that opens its list, named as the file names it; in CODEOWNERS and the allowed signers, a removed or added
    line is asked about by its first field. The first change is asked and the rest are counted. No change that can
    be read this way gives the plain question over the file, never a guess at what it means.
    """

    asks: list[str] = []
    keys: list[tuple[int, str]] = []  # (indent, key) of the lists the diff is inside
    name = posixpath.normpath(path.lstrip("/"))
    for line in patch.splitlines():
        if not line or line[0] not in " +-" or line.startswith(("+++", "---")):
            continue
        sign = line[0]
        key_match = _KEY.match(line)
        if name == RULES_FILE and key_match:
            indent = len(key_match.group(1))
            keys = [(depth, key) for depth, key in keys if depth < indent]
            keys.append((indent, key_match.group("key")))
            continue
        if sign == " ":
            continue
        if name == RULES_FILE:
            item = _ITEM.match(line)
            if item is None or not keys:
                continue
            value = _unquote(item.group("value"))
            if not value or ":" in value:
                continue
            asks.append(
                f"Remove `{value}` from `{keys[-1][1]}`?" if sign == "-" else f"Add `{value}` to `{keys[-1][1]}`?"
            )
        else:
            fields = line[1:].split("#", 1)[0].split()
            if not fields:
                continue
            if name.endswith("CODEOWNERS"):
                owners = ", ".join(fields[1:])
                asks.append(
                    f"Remove the owners of `{fields[0]}`?"
                    if sign == "-"
                    else f"Make {owners or 'nobody'} the owner of `{fields[0]}`?"
                )
            else:
                asks.append(
                    f"Remove `{fields[0]}` from the allowed signers?"
                    if sign == "-"
                    else f"Add `{fields[0]}` to the allowed signers?"
                )
    if not asks:
        return f"Accept the change to `{name}`?"
    return (
        asks[0]
        if len(asks) == 1
        else f"{asks[0]} (and {len(asks) - 1} more {'change' if len(asks) == 2 else 'changes'})"
    )


#: The rules file whose history the Review policy page lists.
RULES_FILE: Final[str] = ".lemoncrow/review.yaml"
_COMMIT: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{7,64}")
_FIELD: Final[str] = "\x1f"
HISTORY_LIMIT: Final[int] = 10


def read_policy_history(repo: Path, base_commit: str, *, limit: int = HISTORY_LIMIT) -> list[dict[str, str]] | None:
    """The commits that changed the rules file on the base branch, newest first: hash, date, author, subject.

    Read from the checkout's own git history at the review's base commit (the trusted base), never from the change.
    ``None`` when git cannot answer (no checkout, an unknown commit): the page then says so, it does not claim there
    is no history. Local only: a hosted server holds no checkout (SPEC 7.1 P7-3 G).
    """

    if not _COMMIT.fullmatch(base_commit):
        return None
    try:
        done = subprocess.run(
            [
                "git",
                "log",
                f"-n{int(limit)}",
                f"--format=%h{_FIELD}%as{_FIELD}%an{_FIELD}%s",
                base_commit,
                "--",
                RULES_FILE,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    rows: list[dict[str, str]] = []
    for line in done.stdout.splitlines():
        parts = line.split(_FIELD)
        if len(parts) == 4:
            rows.append({"commit": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return rows


__all__ = [
    "HISTORY_LIMIT",
    "OWNER_SCOPE",
    "POLICY_FILES",
    "RULES_FILE",
    "PolicyGate",
    "diff_question",
    "evaluate_gate",
    "is_policy_path",
    "owner_token",
    "policy_paths",
    "read_policy_history",
    "required_scope",
]
