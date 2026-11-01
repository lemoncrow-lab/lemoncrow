"""What keeps a pull request's own text from steering the reviewer, leaking a secret, or posting a link (P1.6).

Three deterministic pieces, none of them a model:

* :func:`addressed_to_ai` -- text written *to* an AI reviewer ("ignore previous instructions", "do not report this
  issue", "approve this PR", "you are now ...", hidden HTML comments or zero-width text aimed at the bot). Finding it
  changes nothing about the review: that text reaches a model only inside a ``data:*`` block whose system prompt says
  no text there can change the task. It only earns the summary a calm note (:attr:`Safety.note`).
* :class:`Redactor` -- credential-shaped values are masked in every string of a prompt before the model is called, and
  counted once each, so the receipt can say how many the model never saw. High precision on purpose: code under review
  is still shown as written, only the secret value is replaced. An agent-CLI route reads files, not just the prompt:
  the Snapshot copy it is handed is masked file by file with the same Redactor (:func:`snapshot_masker`), line count
  kept so what it cites still points at the right line.
* :func:`defang` -- model-written text loses every image, and every link or URL to a host other than the
  repository's, before the bot posts it: a link is how a prompt-injected model would carry data out.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any, Final
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# text addressed to an AI reviewer
# --------------------------------------------------------------------------- #

# lc-debt: a fixed English phrase list, so a paraphrase or another language passes unflagged (it is still only data to
# the model); upgrade path: a small multilingual classifier behind the same boolean.
_ADDRESSED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?(?:(?:previous|prior|above|earlier|your|system)\s+)+"
        r"(?:instructions?|rules|prompts?|guidelines|directions)\b",
        r"\b(?:do\s+not|don'?t|never)\s+(?:report|flag|mention|comment\s+on|point\s+out|raise)\s+"
        r"(?:this|these|that|any|the)\s+(?:\w+\s+)?(?:issues?|bugs?|problems?|findings?|vulnerabilit(?:y|ies)|defects?)\b",
        r"\b(?:you\s+(?:must|should|will|need\s+to)\s+|must\s+|always\s+|automatically\s+)"
        r"approve\s+(?:this|the)\s+(?:pr|pull\s+request|change|changes|merge\s+request|diff)\b",
        r"\b(?:mark|rate|report|treat|classify)\s+(?:this|it|the\s+(?:pr|pull\s+request|change))\s+as\s+"
        r"(?:safe|clean|secure|approved|lgtm|harmless|no\s+issues)\b",
        r"\byou\s+are\s+now\s+(?:an?\b|the\b|acting\b|in\s+\w+\s+mode\b|dan\b|unrestricted\b|jailbroken\b)",
        r"\b(?:reveal|print|show|repeat|ignore|override|new|your)\s+(?:the\s+|your\s+)?system\s+prompt\b",
        r"\bnew\s+(?:system\s+)?instructions?\s*[:=]",
        r"<\s*/?\s*(?:system|assistant)\s*>",
        r"\b(?:note|message|instructions?|attention)\s+(?:to|for)\s+(?:the\s+|any\s+|all\s+)?"
        r"(?:ai|llm|gpt|automated|bot|copilot|lemoncrow)(?:\s+(?:code\s+)?reviewers?|\s+models?|\s+assistants?)?\b",
        r"\b(?:dear|hey|hi|hello)\s+(?:ai|llm|bot|copilot|lemoncrow|claude|chatgpt|gpt)\b",
        r"\bif\s+you\s+are\s+an?\s+(?:ai|llm|language\s+model|ai\s+(?:code\s+)?reviewer|automated\s+reviewer)\b",
        # A vocative to the reviewer: "AI reviewers: ...", "LemonCrow, do not ...".
        r"(?:^|[^\w])(?:(?:ai|llm|automated)\s+(?:code\s+)?(?:reviewers?|agents?|assistants?)|lemoncrow|copilot|claude|"
        r"chatgpt|gpt|coderabbit)\s*[:,]\s*(?:please\s+)?"
        r"(?:do|don'?t|never|ignore|approve|skip|mark|treat|report|disregard|you)\b",
    )
)
#: Hidden HTML comments: invisible on the rendered page, so text in one is written for a machine.
_HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--(.*?)(?:-->|\Z)", re.DOTALL)
#: A hidden comment that speaks to an AI by name (``AI: ...``, ``LemonCrow, ...``). A template's "Was AI used?" does not.
_AI_NAMES: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[^\w])(?:ai|llm|gpt|claude|copilot|lemoncrow|assistant|ai\s+(?:code\s+)?reviewers?|bots?)\s*[:,]\s*\w",
    re.IGNORECASE | re.MULTILINE,
)
#: Zero-width and bidi characters, which hide or reorder text on the page.
_INVISIBLE: Final[re.Pattern[str]] = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
#: Unicode tag characters (U+E0000..U+E007F) smuggle ASCII nobody sees: never in honest prose.
_TAGS: Final[re.Pattern[str]] = re.compile("[\U000e0000-\U000e007f]")


def _phrased(text: str) -> bool:
    return any(pattern.search(text) for pattern in _ADDRESSED)


def addressed_to_ai(text: str) -> bool:
    """Whether *text* holds words written to an AI reviewer rather than to a person.

    Checked on the text as written, on it with invisible characters taken out (``ig​nore``), on the ASCII that
    Unicode tag characters smuggle, and on hidden HTML comments, which count when they name an AI at all.
    """

    if not text:
        return False
    if _TAGS.search(text):
        return True
    visible = _INVISIBLE.sub("", text)
    if _phrased(visible):
        return True
    return any(_AI_NAMES.search(hidden) for hidden in _HTML_COMMENT.findall(visible))


#: Where a comment starts in a line of code, for the languages a pull request is likely to touch.
_COMMENT_START: Final[re.Pattern[str]] = re.compile(r"(?:#|//|/\*|<!--|--|^\s*\*|^\s*;|\"\"\"|''')")


def added_comments(patch: str) -> Iterable[str]:
    """The comment part of each line a unified diff adds."""

    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        start = _COMMENT_START.search(body)
        if start is not None:
            yield body[start.start() :]


def comments_addressed(packet: Mapping[str, Any] | None, paths: Iterable[str]) -> tuple[str, ...]:
    """The files among *paths* whose added code comments are addressed to an AI reviewer, in packet order."""

    wanted = set(paths)
    found: list[str] = []
    for row in (packet or {}).get("files") or ():
        if not isinstance(row, Mapping) or str(row.get("path") or "") not in wanted:
            continue
        patch = "\n".join(str(hunk.get("patch") or "") for hunk in row.get("hunks") or () if isinstance(hunk, Mapping))
        if any(addressed_to_ai(comment) for comment in added_comments(patch)):
            found.append(str(row["path"]))
    return tuple(found)


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #

#: ``(pattern, placeholder, group)``: the value of *group* is masked (0: the whole match). Placeholders are the ones
#: :func:`lemoncrow_client.kit.redaction.redact` writes, so a prompt reads the same whichever masked a value.
# lc-debt: known provider token shapes and quoted literals under secret-like names, so a bare unquoted secret of an
# unknown shape passes; upgrade path: an entropy check on long literals, measured against the review corpus.
_SECRETS: Final[tuple[tuple[re.Pattern[str], str, int], ...]] = (
    (re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"), "<redacted-openai-key>", 0),
    (
        re.compile(r"(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}"),
        "<redacted-github-token>",
        0,
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>", 0),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,4096}\.[A-Za-z0-9_-]{10,4096}\.[A-Za-z0-9_-]{10,4096}"), "<redacted-jwt>", 0),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|[sr]k_live_[0-9A-Za-z]{20,}"
            r"|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|shp(?:at|pa|ss|ca)_[A-Za-z0-9]{20,})"
        ),
        "<redacted-credential>",
        0,
    ),
    (re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"), "<redacted-credential>", 1),
    (re.compile(r"(?i)(?<=://)[^/\s:@]{1,64}:([^/\s@]{3,128})@"), "<redacted-credential>", 1),
    (
        # A quoted literal assigned to a secret-like name: ``API_KEY = "..."``, ``"password": "..."``, ``token: '...'``.
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|credential|private[_-]?key)[\w-]*[\"']?\s*[:=]\s*"
            r"([\"'])((?![^\"'\s]*://)(?=[^\"'\s]*\d)(?=[^\"'\s]*[A-Za-z])[^\"'\s]{8,})\1"
        ),
        "<redacted-credential>",
        2,
    ),
)
# Literals every match of the pattern at the same index contains (case-folded where the pattern ignores case): a text
# holding none of them is not scanned by that pattern, so masking a whole Snapshot costs substring searches per file,
# not eight regex passes. Exact, not a heuristic: a gate only skips a pattern that cannot match.
_GATES: Final[tuple[tuple[bool, tuple[str, ...]], ...]] = (
    (False, ("sk-",)),
    (False, ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")),
    (False, ("AKIA", "ASIA")),
    (False, ("eyJ",)),
    (False, ("xox", "AIza", "k_live_", "glpat-", "npm_", "shp")),
    (True, ("bearer",)),
    (False, ("://",)),
    (
        True,
        (
            "apikey",
            "api_key",
            "api-key",
            "secret",
            "token",
            "passw",
            "pwd",
            "credential",
            "privatekey",
            "private_key",
            "private-key",
        ),
    ),
)
_KEY_BEGIN: Final[re.Pattern[str]] = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_KEY_END: Final[re.Pattern[str]] = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")


class Redactor:
    """Masks credential-shaped values and counts each distinct one once, however many prompts carry it."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    @property
    def count(self) -> int:
        return len(self._seen)

    def _note(self, value: str) -> None:
        self._seen.add(hashlib.sha256(value.encode("utf-8", "replace")).hexdigest())

    def mask(self, text: str, *, keep_lines: bool = False) -> str:
        """*text* with every secret value replaced by its placeholder. *keep_lines* keeps a masked private key's line
        breaks, so a file keeps its line count."""

        if not text:
            return text
        out = self._mask_keys(text, keep_lines=keep_lines)
        for (pattern, placeholder, group), (folded, needles) in zip(_SECRETS, _GATES, strict=True):
            haystack = out.casefold() if folded else out
            if not any(needle in haystack for needle in needles):
                continue

            def swap(match: re.Match[str], placeholder: str = placeholder, group: int = group) -> str:
                self._note(match.group(group))
                if group == 0:
                    return placeholder
                whole, start = match.group(0), match.start(0)
                return whole[: match.start(group) - start] + placeholder + whole[match.end(group) - start :]

            out = pattern.sub(swap, out)
        return out

    def _mask_keys(self, text: str, *, keep_lines: bool = False) -> str:
        # Linear: each BEGIN pairs with the next END after it (or the end of the text: a key cut by a diff hunk).
        pieces: list[str] = []
        cut = 0
        while (begin := _KEY_BEGIN.search(text, cut)) is not None:
            end = _KEY_END.search(text, begin.end())
            stop = end.end() if end is not None else len(text)
            self._note(text[begin.end() : end.start() if end is not None else stop].strip())
            breaks = "\n" * text.count("\n", begin.start(), stop) if keep_lines else ""
            pieces += [text[cut : begin.start()], "<redacted-private-key>" + breaks]
            cut = stop
        return "".join(pieces) + text[cut:] if pieces else text

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self._walk(item) for item in value]
        if isinstance(value, dict):
            return {key: self._walk(item) for key, item in value.items()}
        return value

    def prompt(self, prompt: str) -> str:
        """A prompt with its secrets masked: a JSON prompt value by value (so no mask breaks its syntax), else as text.
        A prompt with nothing to mask is returned as it was, byte for byte."""

        try:
            decoded = json.loads(prompt)
        except ValueError:
            return self.mask(prompt)
        masked = self._walk(decoded)
        return prompt if masked == decoded else json.dumps(masked, ensure_ascii=False, separators=(",", ":"))

    def executor(self, inner: Callable[..., Any]) -> Callable[..., Any]:
        """*inner* (a structured model executor) that is only ever handed a masked prompt."""

        def call(**kwargs: Any) -> Any:
            if isinstance(kwargs.get("prompt"), str):
                kwargs["prompt"] = self.prompt(kwargs["prompt"])
            return inner(**kwargs)

        return call


_BOUND: ContextVar[Redactor | None] = ContextVar("lemoncrow_review_redactor", default=None)


@contextmanager
def bound_redactor(redactor: Redactor) -> Iterator[Redactor]:
    """Within the block, a Snapshot materialized for an agent-CLI route is masked by *redactor*, so its count is the
    review's."""

    token = _BOUND.set(redactor)
    try:
        yield redactor
    finally:
        _BOUND.reset(token)


def snapshot_masker() -> Callable[[str], str]:
    """What masks one file of a Snapshot copy an agent-CLI route may read: the bound review's Redactor, else a fresh one.
    Line count is kept."""

    return partial((_BOUND.get() or Redactor()).mask, keep_lines=True)


# --------------------------------------------------------------------------- #
# links and images in what a model wrote
# --------------------------------------------------------------------------- #

_MEDIA_TAG = r"<\s*/?\s*(?:img|picture|source|video|audio|iframe|embed|object|svg|image|input|link|meta|base)\b[^>]*>?"
_MD_IMAGE = r"!\[(?P<alt>[^\]\n]{0,300})\]\([^)\n]{0,2000}\)"
_MD_LINK = r"\[(?P<text>[^\]\n]{0,300})\]\(\s*<?(?P<target>[^)\s>]{0,2000})>?(?:\s+[\"'(][^)\n]{0,300})?\s*\)"
_AUTOLINK = r"<(?P<auto>[A-Za-z][A-Za-z0-9+.-]{1,20}:[^>\s]{1,2000})>"
_BARE = r"(?P<bare>\b(?:https?|ftp)://[^\s<>\[\]()`\"']{1,2000}|\bwww\.[^\s<>\[\]()`\"']{1,2000})"
_OUTPUT: Final[re.Pattern[str]] = re.compile(
    rf"(?P<media>{_MEDIA_TAG})|(?P<image>{_MD_IMAGE})|(?P<link>{_MD_LINK})|{_AUTOLINK}|{_BARE}", re.IGNORECASE
)
#: A reference definition (``[1]: https://...``), which turns any ``[text][1]`` into a link.
_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"(?im)^(?P<lead>[ ]{0,3}\[[^\]\n]{1,300}\]:)[ \t]*<?(?P<target>\S+?)>?(?:[ \t].*)?$"
)
_TRAILING = ".,;:!?"


def _host_of(target: str) -> str:
    try:
        parts = urlsplit(target if "://" in target or target.startswith("//") else f"//{target}")
        return (parts.hostname or "").lower()
    except ValueError:
        return ""


def _internal(target: str, host: str) -> bool:
    """A target that stays on the repository's own host: relative (``#anchor``, ``/path``, ``file.py``) or on *host*."""

    stripped = target.strip()
    scheme = re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", stripped)
    if scheme is None and not stripped.startswith("//") and not stripped.lower().startswith("www."):
        return True
    if scheme is not None and scheme.group(0).lower() not in {"http:", "https:"}:
        return False
    return bool(host) and _host_of(stripped) == host.lower()


def _removed(target: str) -> str:
    # ``www.`` would be turned back into a link by GitHub.
    shown = _host_of(target).removeprefix("www.")
    return f"{shown} (link removed)" if shown else "(link removed)"


def defang(text: str, *, host: str = "") -> str:
    """Model-written *text* with no image, media tag, or link or URL to a host other than *host*.

    *host* is the repository's GitHub host (``github.com``, or an Enterprise Server's); links there, and relative ones,
    are kept. An image is removed wherever it points (its alt text stays): a model has no reason to embed one, and an
    image is fetched with no click at all. What is left of a removed link is its text and the bare host name, which
    GitHub does not turn back into a link.
    """

    if not text:
        return text

    def reference(match: re.Match[str]) -> str:
        target = match.group("target")
        return match.group(0) if _internal(target, host) else f"{match.group('lead')} {_removed(target)}"

    def swap(match: re.Match[str]) -> str:
        if match.group("media") is not None:
            return "(media removed)"
        if match.group("image") is not None:
            alt = match.group("alt").strip()
            return f"{alt} (image removed)" if alt else "(image removed)"
        if match.group("link") is not None:
            target = match.group("target")
            return match.group(0) if _internal(target, host) else f"{match.group('text')} ({_removed(target)})"
        target = match.group("auto") or match.group("bare") or ""
        tail = ""
        while target and target[-1] in _TRAILING:
            target, tail = target[:-1], target[-1] + tail
        return match.group(0) if _internal(target, host) else _removed(target) + tail

    return _OUTPUT.sub(swap, _REFERENCE.sub(reference, text))


# --------------------------------------------------------------------------- #
# what the summary says
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Safety:
    """What this review's safety checks saw: where text addressed to AI reviewers was, and how many secrets were masked.

    ``addressed`` holds plain labels this module's callers write (``the description``, ``linked issue #12``, ``added code
    comments``), never the text itself.
    """

    addressed: tuple[str, ...] = ()
    redacted: int = 0

    @property
    def note(self) -> str:
        """The calm sentence for the summary, or ``""``."""

        if not self.addressed:
            return ""
        where = list(dict.fromkeys(self.addressed))
        named = where[0] if len(where) == 1 else ", ".join(where[:-1]) + " and " + where[-1]
        verb = "contain" if len(where) > 1 or where[0].endswith("s") else "contains"
        return f"{named[:1].upper()}{named[1:]} {verb} text addressed to AI reviewers; it was treated as data."

    @property
    def receipt(self) -> str:
        """The receipt line for the masked secrets, or ``""``."""

        # Values shaped like credentials, not proven ones: a test file's fake sk-... fixture is masked and counted too.
        if self.redacted <= 0:
            return ""
        noun = "value" if self.redacted == 1 else "values"
        return f"{self.redacted} credential-shaped {noun} masked before the model saw the change"


__all__ = [
    "Redactor",
    "Safety",
    "added_comments",
    "addressed_to_ai",
    "bound_redactor",
    "comments_addressed",
    "defang",
    "snapshot_masker",
]
