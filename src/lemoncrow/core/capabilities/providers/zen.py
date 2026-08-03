"""OpenCode Zen provider — keyless free-tier access plus optional account key.

Zen is an OpenAI-compatible gateway (``https://opencode.ai/zen/v1``). When no
credential is present the upstream client sends the literal bearer token
``public``; the gateway then serves only the zero-cost models. LemonCrow uses
the same contract so a fresh install can run a real model with no signup.

Model IDs are namespaced ``zen/<model>`` so they never collide with a real
OpenAI/Anthropic model of the same name. :func:`apply_zen_transport` rewrites
that namespace into the litellm ``openai/<model>`` + ``api_base`` form at the
single call site that reaches litellm.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_PUBLIC_KEY = "public"
ZEN_PREFIX = "zen/"
ZEN_CATALOG_URL = "https://models.dev/api.json"
# The Zen edge rejects the default urllib user agent with 403.
ZEN_USER_AGENT = "lemoncrow"

# Fallback when models.dev is unreachable. Zero-cost Zen models as of 2026-08.
_FALLBACK_FREE_MODELS: tuple[str, ...] = (
    "big-pickle",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "laguna-s-2.1-free",
    "ling-3.0-flash-free",
    "north-mini-code-free",
    "nemotron-3-ultra-free",
)

# Preferred default when LemonCrow falls back to the keyless public tier.
ZEN_DEFAULT_FREE_MODEL = "zen/big-pickle"

_free_cache: tuple[str, ...] | None = None


def public_tier_enabled() -> bool:
    """False when the operator opted out of the keyless fallback."""
    return os.environ.get("LEMONCROW_ZEN_PUBLIC", "1").strip().lower() not in ("0", "false", "no")


def _opencode_auth_key() -> str | None:
    """Return a Zen key from an existing OpenCode login, if the user has one."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(data_home) / "opencode" / "auth.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = raw.get("opencode")
    if isinstance(entry, dict):
        key = entry.get("key") or entry.get("access")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def zen_api_key(cfg: Any | None = None) -> str:
    """Resolve the Zen credential: explicit key, OpenCode login, else ``public``."""
    env_key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if env_key:
        return env_key
    if cfg is not None:
        file_key = cfg.get("zen", "api_key")
        if isinstance(file_key, str) and file_key.strip():
            return file_key.strip()
    return _opencode_auth_key() or ZEN_PUBLIC_KEY


def has_account_key(cfg: Any | None = None) -> bool:
    """True when a real Zen credential (not the public sentinel) is available."""
    return zen_api_key(cfg) != ZEN_PUBLIC_KEY


def free_model_ids() -> tuple[str, ...]:
    """Zero-cost Zen model IDs, read from the public models.dev catalog."""
    global _free_cache
    if _free_cache is not None:
        return _free_cache
    _free_cache = _fetch_free_model_ids() or _FALLBACK_FREE_MODELS
    return _free_cache


def _fetch_free_model_ids() -> tuple[str, ...]:
    import urllib.request

    request = urllib.request.Request(ZEN_CATALOG_URL, headers={"User-Agent": ZEN_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            catalog: dict[str, Any] = json.loads(resp.read())
    except Exception as exc:  # catalog is advisory; fall back to the static list
        logger.debug("zen catalog fetch failed: %s", exc)
        return ()
    models = (catalog.get("opencode") or {}).get("models") or {}
    free: list[str] = []
    for model_id, meta in models.items():
        cost = (meta or {}).get("cost") or {}
        if cost.get("input", 1) == 0 and cost.get("output", 1) == 0:
            free.append(str(model_id))
    return tuple(free)


def invalidate_cache() -> None:
    global _free_cache
    _free_cache = None


def fallback_model() -> str:
    """Model to fall back to when route selection fails outright.

    The historical default was ``gpt-4o-mini``, which turns a routing failure
    into a confusing "Missing credentials ... OPENAI_API_KEY" error on a
    machine that never had an OpenAI key. Prefer the keyless Zen free tier,
    which can actually execute the turn.
    """
    explicit = os.environ.get("LEMONCROW_LITELLM_MODEL", "").strip()
    if explicit:
        return explicit
    if not os.environ.get("OPENAI_API_KEY", "").strip() and public_tier_enabled():
        return ZEN_DEFAULT_FREE_MODEL
    return "gpt-4o-mini"


def is_zen_model(model: str) -> bool:
    return model.startswith(ZEN_PREFIX)


def apply_zen_transport(request_kwargs: dict[str, Any], cfg: Any | None = None) -> dict[str, Any]:
    """Rewrite a ``zen/<model>`` request into litellm's OpenAI-compatible form.

    Non-Zen requests are returned unchanged, so this is safe to call on every
    litellm invocation.
    """
    model = str(request_kwargs.get("model") or "")
    if not is_zen_model(model):
        return request_kwargs
    patched = dict(request_kwargs)
    patched["model"] = f"openai/{model[len(ZEN_PREFIX) :]}"
    patched["api_base"] = ZEN_BASE_URL
    patched.setdefault("api_key", zen_api_key(cfg))
    return patched


__all__ = [
    "ZEN_BASE_URL",
    "ZEN_DEFAULT_FREE_MODEL",
    "ZEN_PREFIX",
    "ZEN_PUBLIC_KEY",
    "ZEN_USER_AGENT",
    "apply_zen_transport",
    "fallback_model",
    "free_model_ids",
    "has_account_key",
    "invalidate_cache",
    "is_zen_model",
    "public_tier_enabled",
    "zen_api_key",
]
