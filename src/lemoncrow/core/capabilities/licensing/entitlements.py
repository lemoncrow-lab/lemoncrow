"""The entitlement contract every Pro gate calls.

Single source of truth for "is this feature unlocked?". The only entitlement
source is the OAuth session created by ``lemon login``: the auth server
reports the account's plan via ``/api/auth/me``, cached on disk for 6 h.
Results are cached in-process until the next cache boundary. Fail-closed: no
session, or no fresh server answer, means Free.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from lemoncrow.core.capabilities.licensing import store
from lemoncrow.core.capabilities.licensing.features import PRO_FEATURES, describe
from lemoncrow.core.capabilities.licensing.models import (
    PRO_PLANS,
    FeatureLocked,
    License,
    LicenseStatus,
)

_OFFLINE_RETRY_SECONDS = 3600

# Sent on every auth-server call: Cloudflare's bot protection rejects the
# default "Python-urllib/x.y" user-agent with 403.
USER_AGENT = "LemonCrow-CLI/1.0"


@dataclass
class _Resolved:
    token: str | None
    license: License | None
    reason: str
    next_check_at: int | None = None


_cache: _Resolved | None = None


def reload() -> None:
    """Drop the cached entitlement state (call after login/logout)."""
    global _cache
    _cache = None


def _now() -> int:
    return int(time.time())


def _fetch_auth_user(auth_token: str) -> dict[str, object] | None:
    """Fetch ``/api/auth/me`` (also renews the server-side CLI token) and cache it.

    Returns ``None`` on any failure -- the caller decides how to degrade.
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{store.load_auth_base()}/api/auth/me",
            # Explicit UA: Cloudflare bot protection 403s python-urllib's default.
            headers={"Authorization": f"Bearer {auth_token}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    store.save_auth_user(data)
    return data


def _resolve() -> _Resolved:
    global _cache
    token = store.load_auth_token()
    now = _now()
    if _cache is not None and _cache.token == token and (_cache.next_check_at is None or now < _cache.next_check_at):
        return _cache
    if token is None:
        _cache = _Resolved(token=None, license=None, reason="not signed in")
        return _cache
    data = store.load_auth_user()
    if data is None:
        data = _fetch_auth_user(token)
    if data is None:
        _cache = _Resolved(
            token=token,
            license=None,
            reason="could not verify the subscription (offline?)",
            next_check_at=now + _OFFLINE_RETRY_SECONDS,
        )
        return _cache
    plan = str(data.get("plan") or "free")
    if plan not in PRO_PLANS:
        _cache = _Resolved(
            token=token,
            license=None,
            reason="signed in on the free plan",
            next_check_at=now + store.AUTH_USER_CACHE_TTL,
        )
        return _cache
    lic = License(
        license_id=str(data.get("user_id") or ""),
        email=str(data.get("email") or ""),
        plan=plan,
    )
    _cache = _Resolved(token=token, license=lic, reason="active", next_check_at=now + store.AUTH_USER_CACHE_TTL)
    return _cache


def refresh_plan() -> None:
    """Bypass the disk cache once: fetch ``/api/auth/me`` live and re-resolve.

    Call before showing an upsell — the user may have purchased seconds ago.
    No-op when signed out; failures leave the previous state (fail-closed).
    """
    token = store.load_auth_token()
    if token:
        _fetch_auth_user(token)
    reload()


def current_license() -> License | None:
    return _resolve().license


def is_pro() -> bool:
    lic = current_license()
    return lic is not None and lic.plan in PRO_PLANS


def has_feature(feature: str) -> bool:
    """True if ``feature`` is unlocked. Non-Pro features are always allowed.

    There is deliberately NO dev backdoor: the only unlock path is the OAuth
    session plan. Developer machines get Pro the same way customers do — sign
    in with an account whose plan is pro (fail-closed by construction).
    """
    if feature not in PRO_FEATURES:
        return True
    lic = current_license()
    return lic is not None and lic.grants(feature)


def require(feature: str) -> None:
    """Raise :class:`FeatureLocked` unless ``feature`` is unlocked."""
    if not has_feature(feature):
        raise FeatureLocked(feature, f"{describe(feature)} requires LemonCrow Pro")


def status() -> LicenseStatus:
    resolved = _resolve()
    if os.environ.get(store.AUTH_TOKEN_ENV_VAR, "").strip():
        source = "env"
    elif store.auth_token_path().exists():
        source = "file"
    else:
        source = "none"
    lic = resolved.license
    if lic is not None:
        return LicenseStatus(
            licensed=True,
            valid=True,
            plan=lic.plan,
            email=lic.email,
            features=lic.features or tuple(PRO_FEATURES),
            reason="active",
            source=source,
        )
    return LicenseStatus(
        licensed=resolved.token is not None,
        valid=False,
        plan=None,
        email=None,
        features=(),
        reason=resolved.reason,
        source=source,
    )
