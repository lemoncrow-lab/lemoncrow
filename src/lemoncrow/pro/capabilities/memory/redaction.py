"""Memory-specific redaction guard shared by host surfaces."""

from __future__ import annotations

import re

from lemoncrow_client.kit.redaction import redact

_REDACTION_PLACEHOLDER_RE = re.compile(r"<redacted[^>]*>")


def redact_memory_input(text: str, field_name: str) -> str:
    """Redact memory text and reject writes that are mostly secret material.

    Existing redaction placeholders pass through unchanged so repeated host
    boundaries do not progressively erase an already-sanitized value.
    """
    if _REDACTION_PLACEHOLDER_RE.search(text):
        return text
    redacted = redact(text)
    if not text:
        return redacted
    remaining = _REDACTION_PLACEHOLDER_RE.sub("", redacted)
    if len(remaining.strip()) < len(text.strip()) * 0.5:
        raise ValueError(f"{field_name} rejected: likely secret leakage")
    return redacted


__all__ = ["redact_memory_input"]
