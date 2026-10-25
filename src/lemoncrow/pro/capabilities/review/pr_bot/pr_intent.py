"""What the author says the change is for: the pull request's description and the issues it links, as review context.

Two steps, both on the existing intent model (:mod:`lemoncrow.pro.capabilities.review.intent`):

1. :func:`gather` reads the title and description (already in the live pull request, no extra call) and the GitHub issues of
   *this repository* it names, with the workflow's own token: ``Fixes #12``, ``Closes owner/repo#12`` or a URL of an
   issue of this repository. It records them in the Snapshot's throwaway store with ``record_intent`` -- the
   description as the Change's description, each issue as a *bound* intent -- so the review sees them the way the Reader
   sees an intent given with ``--intent``. An issue the token cannot read is skipped, with a note, and the review goes on.
2. :func:`prompt_block` reads the bound intent back and makes the small, cleaned block an investigator is shown.

All of it is untrusted data: written by people, some of whom do not write the code. It is tagged as data in the
prompt, never an instruction, and the prompt says so. It is cleaned first (HTML comments, which a rendered pull
request hides from a human reader, are dropped; credentials are redacted), cut to a token budget, and kept away from
the categories where an author's word must never excuse a finding (:data:`WITHHELD_FROM`). It reaches the model
and nowhere else: nothing here is posted, and nothing in it can set a status, a severity or a marker.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.pro.capabilities.review.intent import (
    DESCRIPTION_REF,
    INTENT_WITHHELD_CATEGORIES,
    IntentContent,
    IntentObservation,
    IntentReference,
    intent_bindings,
    latest_description,
    parse_intent_references,
    record_intent,
)
from lemoncrow.pro.capabilities.review.repo_context import estimate_tokens
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.pr_bot.engine import Captured
    from lemoncrow.pro.capabilities.review.pr_bot.github import BotGitHub, PullFacts

#: ``LEMONCROW_REVIEW_INTENT=0`` reads no intent at all; ``..._TOKENS`` raises or lowers the budget.
ENABLE_ENV: Final[str] = "LEMONCROW_REVIEW_INTENT"
TOKENS_ENV: Final[str] = "LEMONCROW_REVIEW_INTENT_TOKENS"
#: Estimated tokens (four characters each) of the intent data in one investigator prompt, all of it together.
BUDGET_TOKENS: Final[int] = 1000
DESCRIPTION_TOKENS: Final[int] = 500
ISSUE_TOKENS: Final[int] = 250
MAX_ISSUES: Final[int] = 3
#: Categories whose investigator is never shown the intent (:data:`INTENT_WITHHELD_CATEGORIES`): those investigations
#: see the code only.
WITHHELD_FROM: Final[frozenset[str]] = INTENT_WITHHELD_CATEGORIES
#: Put in front of the title of a finding the investigator marked as contradicting the stated intent; written by this
#: code, never by the model, so the label means the same wherever it appears.
MISMATCH_PREFIX: Final[str] = "Differs from the stated intent: "
ACTOR: Final[str] = "lemoncrow-pr-review"

_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_BLANKS = re.compile(r"\n{3,}")
_SAME_REPO_URL = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d{1,9})(?![\w/])")
_CUT = "…[cut]"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return str((env if env is not None else os.environ).get(ENABLE_ENV, "1")).strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def budget_tokens(env: Mapping[str, str] | None = None) -> int:
    raw = str((env if env is not None else os.environ).get(TOKENS_ENV, "") or "").strip()
    return int(raw) if raw.isdigit() else BUDGET_TOKENS


# --------------------------------------------------------------------------- #
# gathering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntentNote:
    """What was read, and what was not, for the summary's receipt and the job log."""

    described: bool = False
    titled: bool = False
    issues: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    off: bool = False

    @property
    def stated(self) -> bool:
        return self.described or self.titled or bool(self.issues)

    def receipt(self) -> str:
        """The receipt line: what the review was told the change is for."""

        if self.off:
            return "Intent: not read (turned off)"
        parts: list[str] = []
        if self.described:
            parts.append("the pull request title and description")
        elif self.titled:
            parts.append("the pull request title (it has no description)")
        parts.extend(f"issue {ref}" for ref in self.issues)
        if parts:
            return "Intent: read " + ", ".join(parts) + " (untrusted text, used as context only)"
        return (
            "Intent: none stated (the pull request has no title or description and links no issue of this repository)"
        )


def _issue_refs(body: str, slug: str, number: int) -> tuple[list[tuple[int, str]], int]:
    """``([(issue number, how it was named)], links not read)``: the issues of this repository the description names, in
    order, without the pull request itself, and the count of links to other repositories or documents."""

    mine = slug.lower()
    found: dict[int, str] = {}
    elsewhere = 0
    references = list(parse_intent_references(body, repository=slug))
    for match in _SAME_REPO_URL.finditer(body[:65536]):
        link = f"github:{match.group(1).lower()}#{match.group(2)}"
        references.append(IntentReference("issue", link, "body_link", match.group(0)))
    for reference in references:
        owner_repo, _, digits = reference.ref.removeprefix("github:").partition("#")
        if reference.kind != "issue" or not reference.ref.startswith("github:") or not digits.isdigit():
            elsewhere += 1
        elif owner_repo != mine:
            elsewhere += 1
        elif int(digits) != number:
            found.setdefault(int(digits), reference.origin)
    return list(found.items()), elsewhere


def gather(github: BotGitHub, facts: PullFacts, captured: Captured, *, budget: int | None = None) -> IntentNote:
    """Read the description and the linked issues, record them on the Snapshot, and say what happened.

    Never raises for a source that cannot be read: that issue is skipped with a note. The description costs no call;
    each issue costs one ``GET`` with the workflow's token (``issues: read`` is needed for a private repository, and
    without it the issue is skipped).
    """

    if (budget if budget is not None else budget_tokens()) <= 0 or not enabled():
        return IntentNote(off=True)
    body = facts.body or ""
    named, elsewhere = _issue_refs(body, github.pr.slug, github.pr.number)
    skipped = (
        [f"{elsewhere} link{'s' if elsewhere != 1 else ''} to other repositories or documents not read"]
        if elsewhere
        else []
    )
    observations: list[IntentObservation] = []
    read: list[str] = []
    now = datetime.now(UTC).isoformat()
    for number, origin in named[:MAX_ISSUES]:
        reference = IntentReference(
            "issue",
            f"github:{github.pr.slug.lower()}#{number}",
            origin,
            f"https://{github.pr.host}/{github.pr.slug}/issues/{number}",
        )
        try:
            row = github.issue(number)
        except GitHubCliError as exc:
            skipped.append(f"issue #{number} could not be read ({_why(str(exc))}); skipped")
            continue
        if "pull_request" in row:
            skipped.append(f"#{number} is a pull request, not an issue; skipped")
            continue
        observations.append(
            IntentObservation(
                reference,
                IntentContent(
                    title=str(row.get("title") or ""),
                    body=str(row.get("body") or ""),
                    source_revision=str(row.get("updated_at") or ""),
                    fetched_at=now,
                    url=str(row.get("html_url") or reference.url),
                ),
            )
        )
        read.append(f"#{number}")
    if len(named) > MAX_ISSUES:
        skipped.append(f"{len(named) - MAX_ISSUES} more linked issues not read (the limit is {MAX_ISSUES})")
    description = IntentContent(title=facts.title, body=body, fetched_at=now, url=facts.html_url)
    described, titled = bool(body.strip()), bool(facts.title.strip())
    try:
        record_intent(
            captured.store,
            review_id=captured.session.id,
            revision_id=captured.revision.id,
            observations=observations,
            description=description if described or titled else None,
            actor=ACTOR,
        )
    except Exception as exc:  # intent is context: a store that refuses it never fails a review
        skipped.append(f"intent could not be recorded ({type(exc).__name__}); skipped")
        return IntentNote(skipped=tuple(skipped))
    return IntentNote(described=described, titled=titled, issues=tuple(read), skipped=tuple(skipped))


def _why(message: str) -> str:
    """A short, safe reason from a ``gh`` error: its HTTP status or its first words."""

    status = re.search(r"\(HTTP (\d{3})\)", message)
    if status is not None:
        return {"403": "no permission: the token needs issues: read", "404": "not found or not visible"}.get(
            status.group(1), f"HTTP {status.group(1)}"
        )
    return " ".join(message.split())[:60] or "unknown error"


# --------------------------------------------------------------------------- #
# the prompt block
# --------------------------------------------------------------------------- #


def clean_text(text: str) -> str:
    """*text* as a model should read it: no HTML comments (invisible to a human reading the rendered page), no credentials."""

    from lemoncrow_client.kit.redaction import redact

    without = _COMMENT.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANKS.sub("\n\n", redact(without)).strip()


def _cut(text: str, tokens: int) -> tuple[str, bool]:
    limit = max(0, tokens * 4)
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - len(_CUT))].rstrip() + _CUT, True


def _tokens(value: object) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def prompt_block(store: Any, review_id: str, *, budget: int | None = None) -> dict[str, Any] | None:
    """The bound intent of the Review as the small data block investigators are shown, or ``None`` when there is none.

    ``{"description": {title, body, truncated}, "linked_issues": [{ref, title, state, body, truncated}]}``, the two
    parts together within *budget* estimated tokens (:data:`BUDGET_TOKENS`): the description is cut to
    :data:`DESCRIPTION_TOKENS`, each issue to :data:`ISSUE_TOKENS`, and the whole is then shrunk until it fits, the
    last issue first. Cleaned by :func:`clean_text`.
    """

    room = budget if budget is not None else budget_tokens()
    if room <= 0:
        return None
    evidence = store.list_evidence(review_id)
    written = latest_description(evidence)
    description: dict[str, Any] | None = None
    if written is not None and (written.title.strip() or written.body.strip()):
        body, cut = _cut(clean_text(written.body), min(DESCRIPTION_TOKENS, room))
        # (the store titles a row with no title by its reference)
        title = "" if written.title == DESCRIPTION_REF else clean_text(written.title)[:200]
        description = {"title": title, "body": body, "truncated": cut or written.truncated}
    issues: list[dict[str, Any]] = []
    for binding in intent_bindings(evidence)[:MAX_ISSUES]:
        snapshot = binding.bound
        if not snapshot.captured:
            continue
        body, cut = _cut(clean_text(snapshot.text), min(ISSUE_TOKENS, room))
        issues.append(
            {
                "ref": "#" + snapshot.ref.rsplit("#", 1)[-1],
                "title": clean_text(snapshot.title)[:200],
                "body": body,
                "truncated": cut or snapshot.truncated,
            }
        )
    if description is None and not issues:
        return None
    return _fit({"description": description, "linked_issues": issues}, room)


def _fit(block: dict[str, Any], room: int) -> dict[str, Any] | None:
    """Shrink *block* until its JSON is within *room* estimated tokens: the longest body loses a quarter, and a body
    that is gone takes its issue with it. Deterministic, and it ends: every pass removes text."""

    for _ in range(200):
        if _tokens(_present(block)) <= room:
            return _present(block)
        bodies = [row for row in (block["description"], *block["linked_issues"]) if row is not None and row["body"]]
        if bodies:
            longest = max(bodies, key=lambda row: len(row["body"]))
            keep = len(longest["body"]) * 3 // 4
            longest["body"] = longest["body"][:keep].rstrip() + _CUT if keep > len(_CUT) else ""
            longest["truncated"] = True
        elif block["linked_issues"]:
            block["linked_issues"].pop()
        elif block["description"] is not None and block["description"]["title"]:
            block["description"]["title"] = block["description"]["title"][: len(block["description"]["title"]) // 2]
        else:
            return None
    return None


def _present(block: Mapping[str, Any]) -> dict[str, Any]:
    """The block as the prompt carries it: only the parts that exist."""

    out: dict[str, Any] = {}
    if block["description"] is not None:
        out["description"] = block["description"]
    if block["linked_issues"]:
        out["linked_issues"] = block["linked_issues"]
    return out


def tokens_of(block: Mapping[str, Any] | None) -> int:
    return 0 if not block else _tokens(block)


__all__ = [
    "BUDGET_TOKENS",
    "MISMATCH_PREFIX",
    "WITHHELD_FROM",
    "IntentNote",
    "budget_tokens",
    "clean_text",
    "enabled",
    "gather",
    "prompt_block",
    "tokens_of",
]
