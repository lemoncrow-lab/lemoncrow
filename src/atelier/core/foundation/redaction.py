"""Redaction of secrets and sensitive content from traces.

Reasoning runtime never stores hidden chain-of-thought or user secrets.
This module is a defense-in-depth filter applied before any text is
written to the store.
"""

from __future__ import annotations

import re
from typing import Any

# Common secret patterns. Conservative — false positives are acceptable
# because we only mask, not drop, and the surrounding text remains.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*\S+"),
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
        "<redacted-hidden-reasoning><redacted-marker>",
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


def redact_failure_cluster(cluster: dict[str, Any] | object) -> dict[str, Any]:
    """Return a redacted dict view of a FailureCluster.

    Works on either a Pydantic ``FailureCluster`` instance or a plain
    dict snapshot. All free-text fields that may carry user data are
    routed through :func:`redact` before the result is exposed to a
    downstream sink (logs, MCP responses, persistence).
    """
    data: dict[str, Any]
    if hasattr(cluster, "model_dump"):
        data = cluster.model_dump()
    elif isinstance(cluster, dict):
        data = dict(cluster)
    else:
        raise TypeError(f"unsupported cluster type: {type(cluster).__name__}")

    for key in (
        "fingerprint",
        "suggested_block_title",
        "suggested_rubric_check",
        "suggested_eval_case",
    ):
        if key in data and isinstance(data[key], str):
            data[key] = redact(data[key])

    if "sample_errors" in data and isinstance(data["sample_errors"], list):
        data["sample_errors"] = redact_list([str(s) for s in data["sample_errors"]])

    return data


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
