"""Who wrote the change, when it is declared exactly, and whether the reviewer is a different model family.

The summary's independence line says three things: the reviewer's model family (from the model route the review ran
on), the author's family *only when the pull request declares it*, and ``independent`` / ``same family`` / ``unknown``.
Nothing is guessed:

* The author is read from ``Co-authored-by:`` trailers on the pull request's commits whose address belongs to a coding
  agent (``noreply@anthropic.com`` for Claude, ``@openai.com`` for Codex, GitHub Copilot's and Cursor's agent accounts,
  Aider's). A trailer with any other address, however its name reads, declares nothing: "Claude Monet" is a person.
  An agent whose trailer names no model (Copilot, Cursor) is an author of *unknown* family.
* The reviewer's family is the configured route's model (``model_identity.model_family``); a CLI default that names no
  model has no provable family.
* ``independent`` needs both known and provably different (``independence.provably_distinct``); ``same family`` needs a
  declared author of a family the reviewer has; everything else is ``unknown``.

Other exact sources (Entire ``Entire-Checkpoint`` trailers with their checkpoint metadata, Agent Trace records) are read by
:mod:`provenance` and plug in through the *extra* argument of :func:`declared_authors`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from lemoncrow.pro.capabilities.review.independence import provably_distinct, reviewer_families
from lemoncrow.pro.capabilities.review.model_identity import ModelIdentity, clean_model_name, model_family

#: How a family is named in the summary.
FAMILY_NAMES: Final[dict[str, str]] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "meta": "Meta",
    "mistral": "Mistral",
    "alibaba": "Alibaba",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "moonshot": "Moonshot",
    "zhipu": "Zhipu",
    "microsoft": "Microsoft",
}
STATUSES: Final[tuple[str, ...]] = ("independent", "same family", "unknown")

_TRAILER: Final = re.compile(r"^\s*co-authored-by:\s*(?P<name>[^<\n]*?)\s*<(?P<email>[^>\n]+)>\s*$", re.I | re.M)
#: ``(address pattern, agent, family the address itself declares)``. The family is ``""`` when the agent runs models of
#: more than one vendor and the trailer does not say which (its name may still name the model, see :func:`_family`).
_AGENTS: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    (re.compile(r"@anthropic\.com$"), "Claude", "anthropic"),
    (re.compile(r"@openai\.com$"), "Codex", "openai"),
    (re.compile(r"(?:^|\+)copilot(?:-swe-agent)?(?:\[bot\])?@users\.noreply\.github\.com$"), "GitHub Copilot", ""),
    (re.compile(r"@cursor\.(?:com|sh)$"), "Cursor", ""),
    (re.compile(r"@aider\.chat$"), "Aider", ""),
)
_MODEL_IN_NAME: Final = re.compile(r"\(([^)]{1,128})\)")


@dataclass(frozen=True, slots=True)
class DeclaredAuthor:
    """A coding agent the pull request declares as an author, and the model family it declares (``""``: not declared)."""

    agent: str
    family: str = ""
    #: Where the declaration came from, in words (``Co-authored-by trailer``).
    source: str = "Co-authored-by trailer"
    #: How many of the pull request's commits declare it.
    commits: int = 1


def _family(declared: str, name: str) -> str:
    """The family the address declares, else the one a model named in the trailer's name declares (``aider (gpt-4o)``)."""

    if declared:
        return declared
    found = _MODEL_IN_NAME.search(name)
    model = clean_model_name(found[1].strip()) if found else ""
    return model_family(model) if model else ""


def trailer_authors(message: str) -> list[DeclaredAuthor]:
    """The coding agents one commit message declares in its ``Co-authored-by:`` trailers, once each."""

    found: dict[tuple[str, str], DeclaredAuthor] = {}
    for match in _TRAILER.finditer(message or ""):
        email, name = match["email"].strip().lower(), match["name"]
        for pattern, agent, family in _AGENTS:
            if pattern.search(email):
                author = DeclaredAuthor(agent, _family(family, name))
                found.setdefault((author.agent, author.family), author)
                break
    return list(found.values())


def declared_authors(
    messages: Sequence[str] | None, extra: Iterable[DeclaredAuthor] = ()
) -> tuple[DeclaredAuthor, ...] | None:
    """Every agent the pull request's commit *messages* declare, with how many commits declare it, plus *extra*.

    *messages* is ``None`` when the commits could not be read: the author is then unknown, not "none declared". *extra*
    is the hook for other exact provenance sources; an entry of the same agent and family adds to the count.
    """

    if messages is None:
        return None
    counts: dict[tuple[str, str, str], int] = {}
    for message in messages:
        for author in trailer_authors(message):
            key = (author.agent, author.family, author.source)
            counts[key] = counts.get(key, 0) + 1
    for author in extra:
        key = (author.agent, author.family, author.source)
        counts[key] = counts.get(key, 0) + max(author.commits, 1)
    return tuple(DeclaredAuthor(agent, family, source, n) for (agent, family, source), n in counts.items())


def reviewer_identities(model_label: str) -> tuple[ModelIdentity, ...]:
    """The route(s) a review ran on, from its model label (``litellm/anthropic/claude-x, claude``): driver, then model."""

    identities: list[ModelIdentity] = []
    for part in (model_label or "").split(","):
        text = part.strip()
        if not text:
            continue
        driver, _, model = text.partition("/")
        identities.append(ModelIdentity(driver=driver, requested_model=clean_model_name(model.rsplit("/", 1)[-1])))
    return tuple(identities)


@dataclass(frozen=True, slots=True)
class Independence:
    """The reviewer, the declared authors (``None``: the commits could not be read) and the verdict on the two."""

    reviewer_models: tuple[str, ...]
    #: The reviewer's families, sorted; empty when not provable (a CLI default, an unknown model).
    reviewer_families: tuple[str, ...]
    authors: tuple[DeclaredAuthor, ...] | None
    commits: int
    status: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reviewer": {"families": list(self.reviewer_families), "models": list(self.reviewer_models)},
            "authors": (
                None
                if self.authors is None
                else [
                    {"agent": a.agent, "family": a.family, "source": a.source, "commits": a.commits}
                    for a in self.authors
                ]
            ),
            "commits": self.commits,
        }


def assess(model_label: str, authors: tuple[DeclaredAuthor, ...] | None, *, commits: int = 0) -> Independence:
    """``independent``, ``same family`` or ``unknown`` for a review on *model_label* of a change by *authors*."""

    identities = reviewer_identities(model_label)
    families = (
        frozenset().union(*(reviewer_families(identity) for identity in identities)) if identities else frozenset()
    )
    known = bool(families) and all(identity.provable for identity in identities) and "" not in families
    models = tuple(model for identity in identities for model in identity.effective_models)
    status = "unknown"
    if known and authors:
        if any(author.family and author.family in families for author in authors):
            status = "same family"
        elif all(
            author.family and all(provably_distinct(author.family, identity) for identity in identities)
            for author in authors
        ):
            status = "independent"
    return Independence(models, tuple(sorted(families)) if known else (), authors, commits, status)


__all__ = [
    "FAMILY_NAMES",
    "STATUSES",
    "DeclaredAuthor",
    "Independence",
    "assess",
    "declared_authors",
    "reviewer_identities",
    "trailer_authors",
]
