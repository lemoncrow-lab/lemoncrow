"""Trusted-base authority for repository-owned Review documents.

Repository configuration can affect how a Change is reviewed or executed, so the
Change itself is never an authority for the current Review. This module keeps
the trust decision separate from any one policy parser so browser/runtime policy,
review rules, context documents, and review instructions can share the same
authority model later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TrustedBaseState = Literal["present", "absent", "provisional"]


@dataclass(frozen=True)
class TrustedBaseDocument:
    """One repository-owned document resolved only from the trusted base.

    None at the resolver boundary means the trusted base is not currently
    readable. Once resolved, content is always safe to consume: either the
    trusted base's bytes or the empty built-in/default input.
    """

    path: str
    content: str
    state: TrustedBaseState
    source: str = ""

    @property
    def base_known(self) -> bool:
        return self.state != "provisional"

    @property
    def provisional(self) -> bool:
        return self.state == "provisional"


def resolve_trusted_base_document(path: str, base_content: str | None) -> TrustedBaseDocument:
    """Resolve repository policy without ever promoting Change/head content.

    None means the base is unavailable, while an empty string means the base was
    inspected and does not contain the document. Both consume defaults for the
    current Review; only the latter is final/cacheable.
    """

    if base_content is None:
        return TrustedBaseDocument(path=path, content="", state="provisional")
    if not base_content:
        return TrustedBaseDocument(path=path, content="", state="absent")
    return TrustedBaseDocument(
        path=path,
        content=base_content,
        state="present",
        source=f"base revision {path}",
    )
