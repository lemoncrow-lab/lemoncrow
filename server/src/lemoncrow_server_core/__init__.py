"""Shared LemonCrow server engine primitives.

This package is public and deployment-neutral.  Hosted/enterprise composition
lives in ``enterprise/server`` and depends on this package, never the reverse.
"""

from .context import Principal, TenantContext, new_opaque_id, validate_opaque_id
from .errors import AgentAction, ErrorCode, ServerError

__all__ = [
    "AgentAction",
    "ErrorCode",
    "Principal",
    "ServerError",
    "TenantContext",
    "new_opaque_id",
    "validate_opaque_id",
]
