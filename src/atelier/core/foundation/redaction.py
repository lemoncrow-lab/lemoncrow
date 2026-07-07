"""Redaction of secrets and sensitive content from traces.

Reasoning runtime never stores hidden chain-of-thought or user secrets.
This module is a defense-in-depth filter applied before any text is
written to the store.
"""

from __future__ import annotations

import os
import re

# Common secret patterns. Conservative — false positives are acceptable
# because we only mask, not drop, and the surrounding text remains.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
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
    (
        re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL),
        "<redacted-private-key>",
    ),
    # JWT-ish tokens (3 base64url segments).
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "<redacted-jwt>",
    ),
    # AWS-style access keys.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    # Email addresses — the most common PII in transcripts indexed into the
    # cross-session recall store. High-precision pattern; IP/phone are deliberately
    # omitted so version numbers and digit literals in code stay searchable.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<redacted-email>"),
]

# Phrases that signal hidden chain-of-thought.
_COT_PATTERNS = [
    (
        re.compile(r"<(think|thinking)>.*?</\1>", re.DOTALL | re.IGNORECASE),
        "<redacted-hidden-reasoning>",
    ),
    (
        re.compile(
            r"\b(?:chain of thought|chain-of-thought|internal reasoning|private thoughts):[^\n\r]*",
            re.IGNORECASE,
        ),
        "<redacted-hidden-reasoning>",
    ),
]


def redact(text: str) -> str:
    """Return text with secrets and chain-of-thought removed."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)

    # Redact CoT blocks/markers without truncating the entire string
    for pattern, replacement in _COT_PATTERNS:
        out = pattern.sub(replacement, out)

    return out


def redact_list(items: list[str]) -> list[str]:
    return [redact(i) for i in items]


# Env kill-switch for live tool-output redaction (G8). Default ON; set
# ATELIER_OUTPUT_REDACTION to one of the falsey tokens below to disable.
_OUTPUT_REDACTION_OFF = {"0", "false", "no", "off"}


def output_redaction_enabled() -> bool:
    """Return whether live tool-output redaction is enabled (default True)."""
    raw = os.getenv("ATELIER_OUTPUT_REDACTION")
    if raw is None:
        return True
    return raw.strip().lower() not in _OUTPUT_REDACTION_OFF


def redact_tool_output(text: str) -> str:
    """Scrub secrets from tool OUTPUT before it reaches the model.

    This is the live-output dual of the persistence-boundary :func:`redact`.
    It reuses the same conservative mask-not-drop credential patterns so a
    read/grep/search/bash result that incidentally contains an AWS key, a
    JWT, a private key, or a ``token=...`` pair is masked rather than handed
    verbatim to the model. Honors the ``ATELIER_OUTPUT_REDACTION`` kill-switch
    (default ON) and never raises: on any failure it returns the input
    unchanged so output is never lost.
    """
    if not text or not output_redaction_enabled():
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


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
