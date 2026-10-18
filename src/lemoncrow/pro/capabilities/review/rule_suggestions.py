"""Conservative repeated-human-feedback to inert Review rule suggestions.

A suggestion is never policy. Adopting one (:func:`propose_rule_adoption`) only
*produces a patch* against the trusted-base policy file the repository already
uses (``.lemoncrow/review.yaml`` ``rules:``); nothing is written, no rule becomes
effective, and no state is recorded. The rule governs later Reviews only after a
human lands that patch on the trusted base through the repository's own review.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal

import yaml

from lemoncrow.pro.capabilities.review.rules import resolve_review_governance
from lemoncrow.pro.capabilities.review.session_models import Annotation
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

RuleSuggestionState = Literal["suggested", "dismissed"]
_SPACE = re.compile(r"\s+")
_LINEISH = re.compile(r"\b(?:line|lines|l)\s*\d+(?:\s*[-:]\s*\d+)?\b", re.IGNORECASE)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_feedback(body: str) -> str:
    text = _SPACE.sub(" ", body.strip()).lower()
    text = _LINEISH.sub("<line>", text)
    return text[:2000]


def _path_family(paths: Sequence[str]) -> tuple[str, ...]:
    clean = [str(PurePosixPath(path)) for path in paths if path and not PurePosixPath(path).is_absolute()]
    if not clean:
        return ()
    if len(set(clean)) == 1:
        return (clean[0],)
    split = [path.split("/") for path in clean]
    common: list[str] = []
    for columns in zip(*split, strict=False):
        if len(set(columns)) != 1:
            break
        common.append(columns[0])
    return ("/".join(common) + "/**",) if common else ("**",)


@dataclass(frozen=True, slots=True)
class RuleSuggestion:
    id: str
    repository_scope: str
    candidate_statement: str
    path_scope: tuple[str, ...]
    supporting_feedback_refs: tuple[str, ...]
    state: RuleSuggestionState = "suggested"
    created_at: str = ""
    updated_at: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_scope": self.repository_scope,
            "candidate_statement": self.candidate_statement,
            "paths": list(self.path_scope),
            "supporting_feedback_refs": list(self.supporting_feedback_refs),
            "supporting_count": len(self.supporting_feedback_refs),
            "state": self.state,
            "active": False,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def suggestion_identity(*, repository_scope: str, normalized_feedback: str, path_scope: Sequence[str]) -> str:
    payload = {
        "v": 1,
        "repository_scope": repository_scope,
        "feedback": normalized_feedback,
        "paths": list(path_scope),
    }
    return "rsg-" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:32]


def repeated_human_feedback_suggestions(
    annotations: Iterable[Annotation],
    *,
    repository_scope: str,
    dismissed_ids: Iterable[str] = (),
    minimum_reviews: int = 2,
) -> tuple[RuleSuggestion, ...]:
    """Group explicit human feedback deterministically across distinct Reviews."""

    dismissed = set(dismissed_ids)
    groups: dict[str, list[Annotation]] = {}
    for annotation in annotations:
        if (
            annotation.source != "human"
            or annotation.created_by_actor != "human"
            or annotation.kind not in {"comment", "request_change", "suggestion"}
            or not annotation.body.strip()
            or annotation.parent_id
        ):
            continue
        normalized = _normalize_feedback(annotation.body)
        if len(normalized) < 12:
            continue
        groups.setdefault(normalized, []).append(annotation)

    out: list[RuleSuggestion] = []
    for normalized, rows in groups.items():
        reviews = {row.review_id for row in rows}
        if len(reviews) < minimum_reviews:
            continue
        paths = _path_family([row.anchor.path for row in rows])
        sid = suggestion_identity(
            repository_scope=repository_scope,
            normalized_feedback=normalized,
            path_scope=paths,
        )
        counts = Counter(row.body.strip() for row in rows)
        statement = sorted(counts, key=lambda value: (-counts[value], value.casefold(), value))[0]
        ordered = sorted(rows, key=lambda row: (row.created_at, row.id))
        out.append(
            RuleSuggestion(
                id=sid,
                repository_scope=repository_scope,
                candidate_statement=statement,
                path_scope=paths,
                supporting_feedback_refs=tuple(f"annotation:{row.id}" for row in ordered),
                state="dismissed" if sid in dismissed else "suggested",
                created_at=ordered[0].created_at,
                updated_at=ordered[-1].updated_at or ordered[-1].created_at,
            )
        )
    return tuple(sorted(out, key=lambda row: (row.state != "suggested", row.created_at, row.id)))


# --------------------------------------------------------------------------- #
# Adoption: an explicit, reviewable patch to the trusted-base policy file       #
# --------------------------------------------------------------------------- #

_RULES_KEY = re.compile(r"^rules\s*:\s*(#.*)?$")
_RULES_KEY_ANY = re.compile(r"^rules\s*:")
_MAX_INSTRUCTION = 4000
_NO_NEWLINE = "\\ No newline at end of file\n"


class RuleAdoptionError(ValueError):
    """The suggestion cannot be turned into a patch that provably only adds its rule."""


@dataclass(frozen=True, slots=True)
class RuleAdoptionProposal:
    """The reviewable change that would adopt one suggestion; it is never applied here."""

    suggestion_id: str
    stable_key: str
    instruction: str
    paths: tuple[str, ...]
    target_path: str
    change: Literal["added", "changed"]
    base_sha256: str
    proposed_content: str
    patch: str
    supporting_feedback_refs: tuple[str, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "state": "proposed",
            "effective": False,
            "stable_key": self.stable_key,
            "instruction": self.instruction,
            "paths": list(self.paths),
            "target_path": self.target_path,
            "change": self.change,
            "base_sha256": self.base_sha256,
            "patch": self.patch,
            "proposed_content": self.proposed_content,
            "supporting_feedback_refs": list(self.supporting_feedback_refs),
            "how_to_land": (
                f"Commit this change to {self.target_path} on the repository's trusted base (its default branch) "
                "through your normal review. Nothing governs any Review until it lands there."
            ),
        }


def adoption_stable_key(suggestion_id: str) -> str:
    """The rule key an adopted suggestion gets: stable, and distinct per suggestion."""

    return "learned-" + suggestion_id.removeprefix("rsg-")[:16]


_YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")


def _quoted(text: str) -> str:
    """A double-quoted YAML scalar for *text*; every character YAML would fold or reject is escaped."""

    return _YAML_UNSAFE.sub(lambda match: f"\\u{ord(match.group()):04x}", json.dumps(text, ensure_ascii=False))


def _rule_block(indent: str, key: str, instruction: str, paths: Sequence[str]) -> str:
    # Quoted scalars cannot be broken out of: no instruction text can start a new row or key.
    lines = [f"{indent}- stable_key: {_quoted(key)}", f"{indent}  instruction: {_quoted(instruction)}"]
    if paths:
        lines.append(f"{indent}  paths:")
        lines.extend(f"{indent}    - {_quoted(path)}" for path in paths)
    return "\n".join(lines) + "\n"


def _insertions(content: str, key: str, instruction: str, paths: Sequence[str]) -> list[tuple[str, str]]:
    """Candidate ``(proposed text, inserted lines)`` that add one rule row to the top-level ``rules:`` list.

    Each candidate is *content* with the inserted lines spliced in and nothing else changed (comments, order and
    line endings kept). Two positions are offered when they differ: right after the last rule line, and after the
    blank lines that follow it (a ``|+`` scalar keeps its trailing blank lines only that way).
    """

    newline = "\r\n" if "\r\n" in content else "\n"

    def block(indent: str) -> str:
        return _rule_block(indent, key, instruction, paths).replace("\n", newline)

    if not content.strip():
        added = "rules:" + newline + block("  ")
        return [(content + added, added)]
    if not content.endswith("\n"):
        raise RuleAdoptionError("the policy file does not end with a newline; add one first, then propose again")
    lines = re.findall(r"[^\n]*\n", content)
    start = next((index for index, line in enumerate(lines) if _RULES_KEY.match(line.rstrip("\r\n"))), None)
    if start is None:
        if any(_RULES_KEY_ANY.match(line) for line in lines):
            raise RuleAdoptionError("the policy file writes `rules:` inline; add the rule by hand")
        added = newline + "rules:" + newline + block("  ")
        return [(content + added, added)]
    last = start
    indent = ""
    seen_item = False
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[0] in " \t-":
            if not seen_item:
                if not stripped.startswith("-"):
                    raise RuleAdoptionError("`rules:` is not a block list; add the rule by hand")
                indent = line[: len(line) - len(line.lstrip())]
                seen_item = True
            last = index
            continue
        break
    added = block(indent if seen_item else "  ")
    positions = [last + 1]
    after_blanks = last + 1
    while after_blanks < len(lines) and not lines[after_blanks].strip():
        after_blanks += 1
    if after_blanks != last + 1:
        positions.append(after_blanks)
    return [("".join(lines[:at]) + added + "".join(lines[at:]), added) for at in positions]


def _is_exact_addition(before: str, after: str, added: str, row: Mapping[str, Any]) -> bool:
    """Whether *after* is *before* plus *added* lines, and parses to *before*'s document plus exactly *row*.

    Nothing is normalized: every existing rule and key must compare equal as parsed (a ``|+`` scalar's trailing
    blank lines included), and removing the inserted lines must give back *before* byte for byte.
    """

    if after.replace(added, "", 1) != before:
        return False
    try:
        old = yaml.safe_load(before.lstrip("\ufeff"))
        new = yaml.safe_load(after.lstrip("\ufeff"))
    except (yaml.YAMLError, ValueError):
        return False
    old = {} if old is None else old
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    expected = {**old, "rules": [*(old.get("rules") or []), dict(row)]}
    return bool(new == expected)


def _patch(path: str, before: str, after: str, *, added: bool) -> str:
    def split(text: str) -> list[str]:
        return text.splitlines(keepends=True)

    body: list[str] = []
    for line in difflib.unified_diff(
        split(before), split(after), fromfile="/dev/null" if added else f"a/{path}", tofile=f"b/{path}"
    ):
        body.append(line if line.endswith("\n") else line + "\n" + _NO_NEWLINE)
    head = f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if added else "")
    return head + "".join(body)


def _effective_rules(
    document: TrustedBaseDocument,
) -> tuple[list[tuple[str, str, tuple[str, ...]]], list[tuple[Any, ...]], list[str]]:
    absent = TrustedBaseDocument(path="", content="", state="absent")
    governance = resolve_review_governance(document, read_base_document=lambda _path: absent)
    rules = [(row.stable_key, row.instruction, row.path_scope) for row in governance.rules]
    access = sorted((row.mode, row.path_scope, row.origin, row.trust_class) for row in governance.content_access)
    return sorted(rules), access, list(governance.errors)


def propose_rule_adoption(suggestion: RuleSuggestion, base: TrustedBaseDocument) -> RuleAdoptionProposal:
    """The patch that adopts *suggestion* as a rule in the trusted-base policy file *base*.

    Pure: reads *base* (the trusted base's bytes, never the Change's head),
    writes nothing and changes no policy. The patch is a pure line insertion
    (the base file, byte for byte, with the new lines removed) and is verified
    twice: the parsed document must equal the base's plus exactly the one new
    rule (nothing normalized, so no existing value may change), and resolving the
    policy before and after must differ only by that rule. Otherwise it is refused.
    """

    if suggestion.state != "suggested":
        raise RuleAdoptionError("only an open suggestion can be adopted")
    if base.state == "provisional":
        raise RuleAdoptionError("the trusted base cannot be read right now, so a patch against it cannot be proposed")
    instruction = suggestion.candidate_statement.strip()
    if not instruction or len(instruction) > _MAX_INSTRUCTION:
        raise RuleAdoptionError(f"a rule is 1 to {_MAX_INSTRUCTION} characters; write this one by hand")
    key = adoption_stable_key(suggestion.id)
    before = base.content if base.state == "present" else ""
    old_rules, old_access, old_errors = _effective_rules(base)
    if any(row[0] == key for row in old_rules):
        raise RuleAdoptionError(f"rule {key} is already in the trusted base")
    if old_errors:
        raise RuleAdoptionError("the trusted-base policy file has errors; fix it before adding a rule")
    row: dict[str, Any] = {"stable_key": key, "instruction": instruction}
    if suggestion.path_scope:
        row["paths"] = list(suggestion.path_scope)
    expected = sorted([*old_rules, (key, instruction, tuple(suggestion.path_scope))])
    after = ""
    for candidate, inserted in _insertions(before, key, instruction, suggestion.path_scope):
        new_rules, new_access, new_errors = _effective_rules(replace_content(base, candidate))
        if (
            not new_errors
            and new_access == old_access
            and new_rules == expected
            and _is_exact_addition(before, candidate, inserted, row)
        ):
            after = candidate
            break
    if not after:
        raise RuleAdoptionError(
            "the rule cannot be added mechanically without changing the file's meaning; add it by hand"
        )
    added = base.state != "present"
    return RuleAdoptionProposal(
        suggestion_id=suggestion.id,
        stable_key=key,
        instruction=instruction,
        paths=tuple(suggestion.path_scope),
        target_path=base.path,
        change="added" if added else "changed",
        base_sha256=hashlib.sha256(before.encode("utf-8")).hexdigest(),
        proposed_content=after,
        patch=_patch(base.path, before, after, added=added),
        supporting_feedback_refs=suggestion.supporting_feedback_refs,
    )


def replace_content(base: TrustedBaseDocument, content: str) -> TrustedBaseDocument:
    """*base* as a present document holding *content* (what landing the patch would make the trusted base)."""

    return TrustedBaseDocument(path=base.path, content=content, state="present", source=f"base Snapshot {base.path}")


__all__ = [
    "RuleAdoptionError",
    "RuleAdoptionProposal",
    "RuleSuggestion",
    "RuleSuggestionState",
    "adoption_stable_key",
    "propose_rule_adoption",
    "repeated_human_feedback_suggestions",
    "replace_content",
    "suggestion_identity",
]
