"""The entitlement contract every Pro gate calls.

Single source of truth for "is this feature unlocked?". The only entitlement
source is the OAuth session created by ``lc account login``: the auth server
reports the account's plan via ``/api/auth/me``, cached on disk for 6 h.
Results are cached in-process until the next cache boundary. Fail-closed: no
session, or no fresh server answer, means Free.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from lemoncrow.core.capabilities.licensing import store
from lemoncrow.core.capabilities.licensing.features import (
    FREE_FEATURES,
    PRO_FEATURES,
    describe,
    features_for_plan,
    minimum_plan,
)
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
    _persist_cap_verdict_token(data)
    return data


def auth_user() -> dict[str, object] | None:
    """Return the current OAuth account, fetching it when the cache is stale."""
    token = store.load_auth_token()
    if token is None:
        return None
    return store.load_auth_user() or _fetch_auth_user(token)


def _persist_cap_verdict_token(data: dict[str, object]) -> None:
    """Copy the server's signed cap verdict from /api/auth/me to the gate's store.

    The compiled gate reads ``capVerdictToken`` from ``auth.json`` /
    ``subscription.json``; ``/api/auth/me`` delivers it (top-level, or nested
    under ``subscriptionStatus``). Best-effort — never raises into the fetch.
    """
    try:
        tok = data.get("capVerdictToken")
        if not isinstance(tok, str) or not tok:
            sub = data.get("subscriptionStatus")
            tok = sub.get("capVerdictToken") if isinstance(sub, dict) else None
        if isinstance(tok, str) and tok:
            from lemoncrow.core.capabilities.plugin_runtime import persist_cap_verdict_token
            from lemoncrow.core.foundation.paths import default_store_root

            persist_cap_verdict_token(default_store_root(), tok)
    except Exception:  # noqa: BLE001 — token persistence must never break auth
        pass


def _verified_plan(data: dict[str, object]) -> str | None:
    """Return the server-signed account plan, or ``None`` when unverified."""
    raw_token = data.get("plan_token")
    token = raw_token if isinstance(raw_token, str) and raw_token else None
    account_id = data.get("user_id")
    server_device_id = data.get("device_id")
    try:
        local_device_id = store.load_or_create_device_id()
        if (
            not isinstance(account_id, str)
            or not account_id
            or not isinstance(server_device_id, str)
            or server_device_id != local_device_id
        ):
            return None
        from lemoncrow.pro.capabilities.licensing_gate import plan_from_token

        signed = plan_from_token(
            token,
            now=int(_now()),
            account_id=account_id,
            device_id=local_device_id,
        )
    except Exception:  # noqa: BLE001 — verification must never crash entitlement
        signed = None
    return signed


def _entitled_plan(data: dict[str, object]) -> str:
    """Resolve paid feature access; unverified state always falls back to Free."""
    return _verified_plan(data) or "free"


def _resolve() -> _Resolved:
    global _cache
    token = store.load_auth_token()
    now = _now()
    if _cache is not None and _cache.token == token and (_cache.next_check_at is None or now < _cache.next_check_at):
        return _cache
    if token is None:
        _cache = _Resolved(token=None, license=None, reason="not signed in")
        return _cache
    data = auth_user()
    if data is None:
        _cache = _Resolved(
            token=token,
            license=None,
            reason="could not verify the subscription (offline?)",
            next_check_at=now + _OFFLINE_RETRY_SECONDS,
        )
        return _cache
    plan = _entitled_plan(data)
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
        device_id=str(data.get("device_id") or ""),
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


def current_identity() -> tuple[str, str, str] | None:
    """Return the server-signed account, local device, and canonical plan."""

    data = auth_user()
    if data is None:
        return None
    plan = _verified_plan(data)
    account_id = data.get("user_id")
    device_id = data.get("device_id")
    if (
        plan not in {"free", "lite", "pro", "enterprise"}
        or not isinstance(account_id, str)
        or not account_id
        or not isinstance(device_id, str)
        or not device_id
    ):
        return None
    return account_id, device_id, plan


def is_pro() -> bool:
    lic = current_license()
    return lic is not None and lic.plan in PRO_PLANS


def has_feature(feature: str) -> bool:
    """Return whether a registered feature is unlocked.

    Free names are explicit. Unknown names fail closed so adding a gate with a
    typo cannot accidentally make a paid surface public.
    """
    if feature in FREE_FEATURES:
        return True
    if feature not in PRO_FEATURES:
        return False
    lic = current_license()
    return lic is not None and lic.grants(feature)


def require(feature: str) -> None:
    """Raise FeatureLocked unless the registered feature is unlocked."""
    if not has_feature(feature):
        tier = minimum_plan(feature)
        raise FeatureLocked(feature, f"{describe(feature)} requires LemonCrow {tier}")


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
            features=lic.features or tuple(sorted(features_for_plan(lic.plan))),
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
