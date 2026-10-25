"""Attribution trailers: what a finding relied on, read from its validated refs and from nothing else.

A comment that says only "this is wrong" cannot be audited. A trailer such as ``Rule used: `no-secrets-in-logs` ·
Context used: `src/handlers.py:12-18` (caller)`` tells a reader which rule the finding was held to and which other file
the investigator looked at, so they can check it or argue with it.

Where it comes from. An investigator names the evidence a finding stands on by *ref*, and the engine refuses a finding
whose refs are not in the registry it built for that run (``ai_review._validate_investigation``): a rule ref is one of
the rules in force (``rules.ReviewRule.ref``), an evidence ref is one the registry minted (a target, its exact source, a
recorded check, an artifact, a static finding, a repository-context excerpt). This module reads those strings and no
others. It never reads the finding's title, explanation or consequence, which are the model's words, so nothing a model
wrote can appear in a trailer, and a model cannot name a source it was not shown.

What each kind says, and what it does not:

* ``rule:<key>`` (``Rule used``): the finding cites that rule. A rule imported from another tool's rules file is cited the
  same way; whether it can make a P0 is the severity rule's business, not the trailer's.
* ``ctx:<kind>:<path>:<first>-<last>`` (``Context used``): the finding cites that excerpt of another file from the
  repository context pack, which the investigator chose to cite, so it relied on it. The target's own pointer and exact
  source are never listed: every finding has both, so they say nothing.
* ``static`` / ``check`` / ``artifact``: the finding is backed by a deterministic tool's finding, a recorded check or an
  attached artifact. Only the kind is named; the registry's label is a sentence about code, not an identifier.

``AGENTS.md`` and ``CLAUDE.md`` are not here on purpose. They are shown to the investigator as context, but a finding has
no ref to cite them by, so nothing proves it relied on them; "shown to the model" is not "relied on", and a trailer that
said so would be a guess (see :mod:`config_import`, "context only, never a rule").

The tokens are what a finding keeps in the summary's hidden state (``BotFinding.used``), so they are bounded: at most
:data:`MAX_STORED` of them and a ``more:N`` count of the rest, and the state parser accepts nothing out of this grammar.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

# lc-debt: AGENTS.md / CLAUDE.md conventions are shown to the investigator but are not in the evidence registry, so a finding
# cannot cite them and no trailer can say "Conventions used"; upgrade: give each conventions excerpt a registry ref
# (kind "convention", like repo_context) so a citing finding can be attributed to it.
#: Tokens kept per finding (the trailer shows :data:`MAX_SHOWN`), then ``more:N`` for the rest.
MAX_STORED: Final[int] = 5
#: Items a trailer names before it says ``+N more``.
MAX_SHOWN: Final[int] = 3
#: A path or key longer than this is not a path or key this bot minted; the token is dropped, not shortened.
MAX_PART: Final[int] = 160

_RULE_REF = re.compile(r"^rule:(?:advisory:)?[a-z_]+:(?P<key>.+)@sha256:[0-9a-f]{8,64}$", re.DOTALL)
_CONTEXT_REF = re.compile(
    r"^context:(?P<kind>caller|definition|test|config):(?P<path>.+):(?P<first>\d{1,7})-(?P<last>\d{1,7})@sha256:[0-9a-f]{8,64}$",
    re.DOTALL,
)
#: Evidence kinds named by their ref's prefix. ``target:`` and the exact-source ref are deliberately absent.
_KIND_PREFIX: Final[tuple[tuple[str, str], ...]] = (
    ("static:", "static"),
    ("check:", "check"),
    ("artifact:", "artifact"),
)
#: What the state may hold for a finding. Anything else read back is dropped.
TOKEN: Final = re.compile(
    r"^(?:rule:[^\x00-\x1f]{1,160}|ctx:(?:caller|definition|test|config):[^\x00-\x1f]{1,160}:\d{1,7}-\d{1,7}"
    r"|static|check|artifact|more:\d{1,4})$"
)
#: The order a trailer names things in: the rule first (the strongest claim), then tools, then context.
_ORDER: Final[tuple[str, ...]] = ("rule", "static", "check", "artifact", "ctx")


def used_from(rule_refs: Iterable[str], evidence_refs: Iterable[str]) -> tuple[str, ...]:
    """The tokens for a finding's validated rule and evidence refs; ``()`` when it cites none of the listed kinds."""

    found: dict[str, None] = {}
    for ref in rule_refs:
        rule = _RULE_REF.match(ref)
        if rule is not None and 0 < len(rule["key"]) <= MAX_PART:
            found[f"rule:{rule['key']}"] = None
    for ref in evidence_refs:
        context = _CONTEXT_REF.match(ref)
        if context is not None:
            if 0 < len(context["path"]) <= MAX_PART:
                found[f"ctx:{context['kind']}:{context['path']}:{context['first']}-{context['last']}"] = None
            continue
        for prefix, kind in _KIND_PREFIX:
            if ref.startswith(prefix):
                found[kind] = None
    ordered = sorted(found, key=lambda token: _ORDER.index(token.split(":", 1)[0]))
    return _bounded(ordered)


def _bounded(tokens: Sequence[str]) -> tuple[str, ...]:
    """At most :data:`MAX_STORED` tokens; the rest become ``more:N`` so the trailer's ``+N`` stays true."""

    real = [token for token in tokens if not token.startswith("more:")]
    extra = sum(int(token[5:]) for token in tokens if token.startswith("more:"))
    if len(real) <= MAX_STORED:
        return (*real, *((f"more:{extra}",) if extra else ()))
    return (*real[:MAX_STORED], f"more:{len(real) - MAX_STORED + extra}")


def clean_tokens(raw: object) -> tuple[str, ...]:
    """The tokens of a state row: only well-formed ones, bounded. The state comes from a comment, so it is checked."""

    if not isinstance(raw, list):
        return ()
    kept = [item for item in raw[:24] if isinstance(item, str) and TOKEN.match(item)]
    return _bounded(kept)


@dataclass(frozen=True, slots=True)
class Item:
    """One thing a trailer names: its group (a key of :data:`GROUPS`), its text and, for a context excerpt, its kind."""

    group: str
    text: str = ""
    detail: str = ""


GROUPS: Final[dict[str, str]] = {
    "rule": "Rule used",
    "static": "Static finding used",
    "check": "Check used",
    "artifact": "Artifact used",
    "ctx": "Context used",
}


def items(used: Sequence[str]) -> tuple[list[Item], int]:
    """``(items in trailer order, how many more were not stored)`` for a finding's tokens."""

    out: list[Item] = []
    more = 0
    for token in used:
        head, _, rest = token.partition(":")
        if head == "more":
            more += int(rest or 0)
        elif head == "rule":
            out.append(Item("rule", rest))
        elif head == "ctx":
            kind, _, place = rest.partition(":")
            out.append(Item("ctx", place, kind))
        elif head in GROUPS:
            out.append(Item(head))
    return sorted(out, key=lambda item: _ORDER.index(item.group)), more


__all__ = ["GROUPS", "MAX_SHOWN", "MAX_STORED", "TOKEN", "Item", "clean_tokens", "items", "used_from"]
