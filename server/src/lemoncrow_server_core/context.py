"""Deployment-neutral actor and request context for the shared server engine.

The public engine needs only stable actor identifiers and the session/view scope
of one operation. Authentication mechanisms, roles, permissions, RBAC scopes,
and bindings belong to the enterprise composition and intentionally do not live
in this package.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Final

from .errors import ErrorCode, ServerError

__all__ = ["Principal", "TenantContext", "new_opaque_id", "validate_opaque_id"]

_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def validate_opaque_id(value: object, field: str) -> str:
    """Accept only bounded, printable, path-free identifiers."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ServerError(
            ErrorCode.PAYLOAD_INVALID,
            f"{field} must be an opaque identifier of 1-128 characters from [A-Za-z0-9_.:-]",
            details={"field": field},
        )
    return value


def new_opaque_id(prefix: str) -> str:
    """Mint a server-issued identifier. Callers never supply their own."""
    return f"{prefix}_{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class Principal:
    """Deployment-neutral actor identity carried by the shared engine."""

    subject: str
    org_id: str
    token_id: str
    expires_at: float | None = None
    auth_method: str = "unspecified"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Scoping for one shared-engine operation."""

    org_id: str
    principal: Principal
    session_id: str
    request_id: str
    view_id: str | None = None
    view_revision: int | None = None

    def __post_init__(self) -> None:
        if self.principal.org_id != self.org_id:
            raise ServerError(
                ErrorCode.FORBIDDEN,
                "principal organization does not match the request organization",
            )

    def with_view(self, view_id: str, view_revision: int | None) -> TenantContext:
        return TenantContext(
            org_id=self.org_id,
            principal=self.principal,
            session_id=self.session_id,
            request_id=self.request_id,
            view_id=view_id,
            view_revision=view_revision,
        )

    def audit_fields(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "principal": self.principal.subject,
            "token_id": self.principal.token_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "view_id": self.view_id or "",
            "view_revision": int(self.view_revision or 0),
        }
