"""Redaction of secrets and sensitive content from traces.

Reasoning runtime never stores hidden chain-of-thought or user secrets.
This module is a defense-in-depth filter applied before any text is
written to the store.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Delimited spans (PEM private keys, chain-of-thought blocks)
# ---------------------------------------------------------------------------
# A single regex for an opener...closer span is a choice between two failures:
#
#   * unbounded ``.*?`` -- catastrophic backtracking. An opener with no closer
#     forces a scan to end-of-string, once per opener, so a blob densely seeded
#     with unmatched openers is quadratic (#38: 9-12 min hangs on ~20MB).
#   * bounded ``.{0,N}?`` -- linear, but it silently stops matching any span
#     longer than N. For a redaction pattern that is not a performance
#     trade-off, it is a leak: a 20KB PEM body or a 100KB reasoning block
#     would sail through unredacted.
#
# So don't use one regex. Enumerate openers and closers separately (two
# C-level passes) and pair them with a monotonic two-pointer walk: linear in
# the input, with no ceiling on how long a span may be.
@dataclass(frozen=True)
class _DelimitedSpan:
    """A set of opener/closer tag pairs redacted as one unit, of any length.

    ``opener`` matches any tag in the set and must expose the tag name as
    group 1; ``closers`` maps the lower-cased tag name to its closing pattern.
    Keeping the whole set in ONE matcher is load-bearing: scanning tag by tag
    instead loses the leftmost semantics of ``<(think|thinking)>.*?</\\1>`` and
    leaks content that regex masked (an inner ``<think>`` span consuming past
    the ``</thinking>`` that would have closed an earlier ``<thinking>``).
    """

    opener: re.Pattern[str]
    closers: dict[str, re.Pattern[str]]


_PRIVATE_KEY_SPAN = _DelimitedSpan(
    # No capture group: PEM headers vary ("RSA ", "EC ", "OPENSSH ", "") and a
    # BEGIN of one kind is closed by an END of any kind here, exactly as the
    # single pattern this replaced did, so one untagged closer covers them all.
    opener=re.compile(r"-----BEGIN [A-Z ]{1,40}PRIVATE KEY-----"),
    closers={"": re.compile(r"-----END [A-Z ]{1,40}PRIVATE KEY-----")},
)
_THINK_SPAN = _DelimitedSpan(
    opener=re.compile(r"<(think|thinking)>", re.IGNORECASE),
    # A ``<think>`` is not closed by ``</thinking>``: each tag keeps its own
    # closer, mirroring the backreference in the pattern this replaced.
    closers={tag: re.compile(rf"</{tag}>", re.IGNORECASE) for tag in ("think", "thinking")},
)


def _redact_span(text: str, span: _DelimitedSpan, replacement: str) -> str:
    """Replace every ``opener...closer`` region with ``replacement``.

    Reproduces lazy ``opener.*?closer`` semantics -- each opener paired with
    the earliest closer of its own tag that starts at or after the opener's
    end, scanning resumes past that closer, openers inside an already redacted
    region are skipped -- without the backtracking or the size ceiling. One
    C-level pass per closer tag plus a monotonic pointer walk, so it is linear
    in the input and a span may be arbitrarily long.
    """
    closer_hits = {tag: list(pattern.finditer(text)) for tag, pattern in span.closers.items()}
    if not any(closer_hits.values()):
        # No closer anywhere means no span can match: exact, not a heuristic.
        return text
    cursor = dict.fromkeys(closer_hits, 0)
    out: list[str] = []
    cut = 0  # everything before this index is emitted or already consumed
    for opener in span.opener.finditer(text):
        if opener.start() < cut:
            continue  # inside a region we already redacted
        tag = opener.group(1).lower() if span.opener.groups else ""
        hits = closer_hits.get(tag)
        if hits is None:
            hits = closer_hits[""] if "" in closer_hits else []
            tag = ""
        index = cursor[tag]
        while index < len(hits) and hits[index].start() < opener.end():
            index += 1
        cursor[tag] = index
        if index == len(hits):
            # This tag is exhausted, but another tag may still close a later
            # opener -- keep scanning rather than stopping here.
            continue
        out.append(text[cut : opener.start()])
        out.append(replacement)
        cut = hits[index].end()
    if not out:
        return text
    out.append(text[cut:])
    return "".join(out)


# Common secret patterns. Conservative — false positives are acceptable
# because we only mask, not drop, and the surrounding text remains.
_Matcher = re.Pattern[str] | _DelimitedSpan

_PATTERNS: list[tuple[_Matcher, str]] = [
    (
        # Generic ``key=value`` / ``key: value`` credential pairs. The value is
        # masked to the end of the line rather than a single ``\S+`` token: a
        # bare ``\S+`` stops at the first space and *leaks* multi-word secret
        # values past that edge (e.g. ``token: Bearer <secret>`` would mask
        # only ``Bearer`` and leak ``<secret>``). ``re.sub`` (no ``count``)
        # replaces *every* occurrence, so a secret repeated in one string is
        # fully masked, not just the first hit. The leading ``\b`` keeps this
        # from swallowing ordinary identifiers like ``AWS_SECRET`` (whose value
        # is caught by the dedicated high-entropy patterns below).
        re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*\S[^\r\n]*"),
        "<redacted-credential>",
    ),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<redacted-openai-key>"),
    (re.compile(r"shppa_[A-Za-z0-9]{20,}"), "<redacted-shopify-token>"),
    (re.compile(r"shpat_[A-Za-z0-9]{20,}"), "<redacted-shopify-token>"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<redacted-github-token>"),
    (_PRIVATE_KEY_SPAN, "<redacted-private-key>"),
    # JWT-ish tokens (3 base64url segments). Segments bounded to 4KB each --
    # real JWTs are a few KB at most -- so a huge base64url blob containing
    # a stray "eyJ" (common by chance) can't force an O(n) failed scan per
    # occurrence (#38: unbounded ``{10,}`` here is quadratic on such input).
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,4096}\.[A-Za-z0-9_\-]{10,4096}\.[A-Za-z0-9_\-]{10,4096}\b"),
        "<redacted-jwt>",
    ),
    # AWS-style access keys.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # Email addresses — the most common PII in transcripts indexed into the
    # cross-session recall store. High-precision pattern; IP/phone are deliberately
    # omitted so version numbers and digit literals in code stay searchable.
    #
    # Local-part/domain/TLD are bounded to RFC-generous lengths (64 / 253 / 24
    # chars). This is the #38 root cause: an unbounded greedy class directly
    # before a required literal ("@", then later ".") that never appears in
    # a huge base64/base64url blob makes Python's backtracking engine retry
    # the full greedy scan independently at every word-boundary position --
    # O(n^2) on multi-MB tool-output blobs (9-12+ min hangs on ~20MB input).
    # Bounding the quantifiers caps the per-position retry cost to a
    # constant, restoring ~linear behavior.
    (re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b"), "<redacted-email>"),
]

# Phrases that signal hidden chain-of-thought.
_COT_PATTERNS: list[tuple[_Matcher, str]] = [
    (_THINK_SPAN, "<redacted-hidden-reasoning>"),
    (
        re.compile(
            r"\b(?:chain of thought|chain-of-thought|internal reasoning|private thoughts):[^\n\r]*",
            re.IGNORECASE,
        ),
        "<redacted-hidden-reasoning>",
    ),
]


def _apply_patterns(patterns: list[tuple[_Matcher, str]], text: str) -> str:
    """Substitute each ``(matcher, replacement)`` in turn.

    A matcher is either a plain regex (substituted directly) or a
    :class:`_DelimitedSpan`, redacted by the linear opener/closer scan so an
    arbitrarily long key body or reasoning block is still masked in full.
    """
    out = text
    for matcher, replacement in patterns:
        if isinstance(matcher, _DelimitedSpan):
            out = _redact_span(out, matcher, replacement)
        else:
            out = matcher.sub(replacement, out)
    return out


def redact(text: str) -> str:
    """Return text with secrets and chain-of-thought removed."""
    if not text:
        return text
    # Redact CoT blocks/markers without truncating the entire string
    return _apply_patterns(_COT_PATTERNS, _apply_patterns(_PATTERNS, text))


def redact_list(items: list[str]) -> list[str]:
    return [redact(i) for i in items]


# Characters that are valid *inside* a JSON string but that Python's
# ``str.splitlines()`` treats as line breaks. ``json.dumps(ensure_ascii=False)``
# emits them raw, which silently splits one JSONL record into two unparseable
# halves for every reader that iterates lines -- observed on real sessions that
# quote web content (U+2028 is common in scraped copy).
_JSONL_LINE_BREAKS = str.maketrans(
    {  # chr(), not literals: these characters are invisible in source
        chr(0x2028): "\\u2028",  # LINE SEPARATOR
        chr(0x2029): "\\u2029",  # PARAGRAPH SEPARATOR
        "\x0b": "\\u000b",
        "\x0c": "\\u000c",
        "\x85": "\\u0085",
    }
)


# Presence test for the characters above. str.translate walks every character
# in Python, which profiled at 7.6s of a 21.5s import; a C-level search first
# makes the common case (no such character anywhere) essentially free.
_JSONL_LINE_BREAK_RE = re.compile("[" + chr(0x2028) + chr(0x2029) + "\x0b\x0c\x85]")


def escape_jsonl_line_breaks(line: str) -> str:
    """Escape in-string characters that ``splitlines()`` would treat as breaks.

    Safe on a serialized record: these characters never occur in JSON syntax
    itself, only inside string values, and the escaped form decodes identically.
    """
    if not _JSONL_LINE_BREAK_RE.search(line):
        return line
    return line.translate(_JSONL_LINE_BREAKS)


def _redact_json_values(value: Any) -> Any:
    """Redact every string *value* in a decoded JSON document, in place-ish.

    Keys are left alone: they are field names, not payload, and rewriting them
    would change the record's schema.
    """
    if isinstance(value, str):
        # Same candidate gate as redact_jsonl: most values in a session record
        # (ids, types, timestamps, code) hold no anchor at all, and skipping
        # the ~11 pattern passes on those is the difference between one regex
        # search and eleven substitutions per value.
        if not _REDACTION_CANDIDATE_RE.search(value):
            return value
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_json_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_values(item) for item in value]
    return value


# Cheap necessary condition for "this text contains something redactable":
# the literal anchors every pattern above requires. Deliberately over-broad
# (IGNORECASE, no structure) -- it only selects candidate lines, and the real
# patterns still decide. One pass of this over the whole document replaces
# running all ~11 patterns per line, which on a 170k-line session meant 1.5M
# regex calls and doubled import wall-clock.
_REDACTION_CANDIDATE_RE = re.compile(
    r"api[_-]?key|secret|token|password|passwd|pwd"
    r"|sk-|shppa_|shpat_|ghp_|eyJ|AKIA|ASIA|@"
    r"|-----BEGIN |</think"
    r"|chain[ -]of[ -]thought|internal reasoning|private thoughts",
    re.IGNORECASE,
)


def redact_jsonl(text: str) -> str:
    """Redact a JSONL document without breaking its records.

    Running :func:`redact` over serialized JSON corrupts it: the patterns know
    nothing about JSON escaping or structure, so a match can straddle a ``\\n``
    escape (``"...\\n@pytest.fixture"`` matches the email rule and leaves a bare
    ``\\`` before the replacement) and the credential rule consumes to
    end-of-*line* -- which, in JSONL, is the rest of the record including its
    closing braces. Measured on a real store: 1.35% of stored lines were
    unparseable, silently dropped by every reader.

    So decode first and redact the string *values*, where "end of line" means
    the end of that value. Lines that are not JSON fall back to :func:`redact`,
    so this is safe on mixed content.
    """
    if not text:
        return text
    # Per-line candidate search, not a whole-document finditer: search() stops
    # at the first anchor, while finditer had to enumerate every '@' and
    # 'token' in the document and cost more than the redaction it was meant to
    # avoid (profiled: 6.7s of a 13.8s import).
    out: list[str] = []
    for line in text.split("\n"):
        if not _REDACTION_CANDIDATE_RE.search(line):
            out.append(escape_jsonl_line_breaks(line))
            continue
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except ValueError:
                out.append(escape_jsonl_line_breaks(redact(line)))
                continue
            # Straight to the values -- running redact() over the whole
            # serialized line first would duplicate every pattern pass the
            # per-value redaction is about to do, on the longest strings in
            # the document.
            out.append(escape_jsonl_line_breaks(json.dumps(_redact_json_values(decoded), ensure_ascii=False)))
        else:
            out.append(escape_jsonl_line_breaks(redact(line)))
    return "\n".join(out)


# Env kill-switch for live tool-output redaction (G8). Default ON; set
# LEMONCROW_OUTPUT_REDACTION to one of the falsey tokens below to disable.
_OUTPUT_REDACTION_OFF = {"0", "false", "no", "off"}


def output_redaction_enabled() -> bool:
    """Return whether live tool-output redaction is enabled (default True)."""
    raw = os.getenv("LEMONCROW_OUTPUT_REDACTION")
    if raw is None:
        return True
    return raw.strip().lower() not in _OUTPUT_REDACTION_OFF


def redact_tool_output(text: str) -> str:
    """Scrub secrets from tool OUTPUT before it reaches the model.

    This is the live-output dual of the persistence-boundary :func:`redact`.
    It reuses the same conservative mask-not-drop credential patterns so a
    read/grep/search/bash result that incidentally contains an AWS key, a
    JWT, a private key, or a ``token=...`` pair is masked rather than handed
    verbatim to the model. Honors the ``LEMONCROW_OUTPUT_REDACTION`` kill-switch
    (default ON) and never raises: on any failure it returns the input
    unchanged so output is never lost.
    """
    if not text or not output_redaction_enabled():
        return text
    return _apply_patterns(_PATTERNS, text)


# Characters and substrings that are never legitimate inside a
# ``cached_grep`` invocation and indicate a shell-injection attempt
# even though we always invoke ``subprocess.run`` with a list argv
# (defense-in-depth in case a future change introduces ``shell=True``
# or pipes the value into a shell command).
_SHELL_INJECTION_TOKENS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")


def is_shell_injection(value: str) -> bool:
    """Return True if ``value`` contains shell metacharacters."""
    if not isinstance(value, str):
        return True
    return any(token in value for token in _SHELL_INJECTION_TOKENS)


# Prompt-injection needles for inbound (index-time) trust labelling (N15).
# Conservative and deterministic: these phrases are the canonical instruction-
# override patterns used in indirect prompt-injection against doc/RAG content.
# We FLAG (never drop) matching chunks so the label can ride along in
# retrieval results; callers that ignore the flag are unaffected.
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?\b"),
    re.compile(r"(?i)\bdisregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\b"),
    re.compile(r"(?i)\byou\s+are\s+now\b.{0,40}\b(?:dan|do\s+anything\s+now|unrestricted|jailbroken)\b"),
    re.compile(r"(?i)\bnew\s+(?:system\s+)?(?:prompt|instructions?)\s*[:=]"),
    re.compile(r"(?i)<\s*/?\s*(?:system|assistant)\s*>"),
    re.compile(r"(?i)\bsystem\s+override\b"),
    re.compile(r"(?i)\bdeveloper\s+mode\b"),
    re.compile(r"(?i)\boverride\s+(?:your\s+|the\s+)?(?:safety|guard\s*rails?|instructions?)\b"),
)


def is_prompt_injection(text: str) -> bool:
    """Return True if ``text`` matches a known prompt-injection needle.

    Inbound dual of :func:`redact_tool_output`. Deterministic and
    conservative — matches only canonical instruction-override phrasing so a
    legitimate code/doc chunk is rarely flagged. Intended for index-time
    trust labelling: the caller attaches the boolean to indexed content; it
    never alters or drops the content itself.
    """
    if not isinstance(text, str) or not text:
        return False
    return any(pattern.search(text) for pattern in _PROMPT_INJECTION_PATTERNS)


def assert_safe_grep_args(pattern: str, path: str) -> None:
    """Raise ``ValueError`` if pattern/path contain shell metacharacters
    or look like attempts to smuggle additional flags into ``grep``.
    """
    if is_shell_injection(pattern) or is_shell_injection(path):
        raise ValueError("cached_grep rejected: shell metacharacters not allowed")
    # Reject obvious flag smuggling. ``--`` is allowed as a separator
    # when set explicitly by the wrapper; user-supplied values must not
    # start with a dash.
    if pattern.startswith("-") or path.startswith("-"):
        raise ValueError("cached_grep rejected: arguments must not start with '-'")
