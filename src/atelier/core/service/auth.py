"""Auth helpers for the service API.

- When ATELIER_REQUIRE_AUTH=false (the local default), all requests pass without a key.
- When ATELIER_REQUIRE_AUTH=true, requests must carry:
    Authorization: Bearer <ATELIER_API_KEY>
- API key values are NEVER logged.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from atelier.core.service.config import cfg


def verify_api_key(authorization: str = Header(default="")) -> None:
    """FastAPI dependency that enforces Bearer auth.

    Skipped entirely when ``ATELIER_REQUIRE_AUTH=false``.
    Uses ``secrets.compare_digest`` to prevent timing attacks.
    """
    if not cfg.require_auth:
        return

    expected = cfg.api_key
    if not expected:
        # Auth required but no key configured — lock everything down.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service API key not configured. Set ATELIER_API_KEY.",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison — never log token or expected.
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )
