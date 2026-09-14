"""Rewrite ``custom/<endpoint>/<model>`` requests into litellm's OpenAI form.

Why this module exists: litellm has no ``custom`` provider. A user-registered
endpoint is just an OpenAI-compatible server, so the request has to become
``openai/<raw_id>`` plus an explicit ``api_base`` at the call site -- exactly
what ``core/capabilities/providers/zen.py::apply_zen_transport`` does for Zen.
Keeping the rewrite in one function is what lets both litellm call sites
(``infra/internal_llm/litellm_client.py`` and ``gateway/cli/runtime.py``) chain
it after the Zen rewrite with a single line each.

Every request this module cannot resolve is returned **unchanged** rather than
raised on. This runs inside the completion path: an endpoint that was removed
from ``providers.json`` between routing and dispatch must surface as the
provider's own error, not as a LemonCrow traceback.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import CUSTOM_PREFIX, CUSTOM_PROVIDER, split_custom_model_id

_ENDPOINTS_KEY = "endpoints"

# The store root ``lc --root X`` was pointed at, or ``None`` when the process
# was never redirected. Registration always has a root in hand (the CLI passes
# ``ctx.obj["root"]`` down), but the two *readers* that matter do not: the
# litellm transport chain sees only ``request_kwargs``, and the routing
# candidate builder is a cached process-global. Both used to fall through to
# ``default_store_root()``, so an endpoint registered under ``--root X`` was
# written to X and then looked for in ~/.lemoncrow -- never rewritten at
# dispatch, never offered as a routing candidate.
#
# This is deliberately *not* ``LEMONCROW_ROOT``: exporting the resolved root
# would redirect every other ``default_store_root()`` reader in the process
# (the daemon, the licensing store, the OAuth token store, spawned hooks) as a
# side effect of a model-setup fix. Only ``lc``'s own entry point writes it,
# and only when ``--root`` was actually typed.
_active_store_root: Path | None = None

# Local servers (vLLM, LM Studio, llama.cpp) ignore the bearer token, but
# litellm's OpenAI path refuses to dispatch without *some* api_key. This is the
# placeholder it gets -- never a real credential, never persisted.
NO_KEY_PLACEHOLDER = "no-key"


def set_active_store_root(root: Path | str | None) -> None:
    """Record the store root this process was explicitly pointed at.

    Called from the ``lc`` group callback when ``--root`` was actually typed --
    never for a defaulted root, so an ordinary invocation keeps resolving
    through ``default_store_root()`` byte for byte as before. The same callback
    clears it with ``None`` on context close, because the scope is one CLI
    invocation and not the interpreter: a host that drives the group twice in
    one process must not inherit the previous run's root.
    """

    global _active_store_root
    _active_store_root = Path(root).expanduser() if root is not None else None


def active_store_root() -> Path | None:
    """The recorded ``--root``, or ``None`` when nothing redirected this process.

    ``None`` is passed straight through to ``providers_config_path``, which
    falls back to ``default_store_root()`` -- so the ambient behaviour is byte
    for byte what it was before this hook existed.
    """

    return _active_store_root


def is_custom_model(model: str) -> bool:
    return model.startswith(CUSTOM_PREFIX)


def resolve_endpoint_api_key(entry: Mapping[str, Any]) -> str:
    """Return the credential for one endpoint entry, or ``""`` when it has none.

    The literal ``api_key`` wins when the user chose to store one; otherwise the
    named environment variable is read *now*, at request time, which is the
    whole point of ``--api-key-env``: the value never lands on disk.
    """

    literal = str(entry.get("api_key") or "").strip()
    if literal:
        return literal
    env_name = str(entry.get("api_key_env") or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return ""


def endpoint_entry(name: str, cfg: Any | None = None) -> dict[str, Any] | None:
    """The raw ``providers.json`` entry for one endpoint, or ``None``."""

    config: Any = cfg
    if config is None:
        from lemoncrow.core.capabilities.providers.config import load_providers_config

        config = load_providers_config(active_store_root())
    try:
        endpoints = config.get(CUSTOM_PROVIDER, _ENDPOINTS_KEY)
    except Exception:  # a caller-supplied cfg stub is not ours to trust
        return None
    if not isinstance(endpoints, dict):
        return None
    entry = endpoints.get(name)
    return entry if isinstance(entry, dict) else None


def apply_custom_transport(request_kwargs: dict[str, Any], cfg: Any | None = None) -> dict[str, Any]:
    """Rewrite a ``custom/<endpoint>/<model>`` request; pass anything else through.

    Safe to call on every litellm invocation -- a non-custom model short-circuits
    before ``providers.json`` is touched.
    """

    model = str(request_kwargs.get("model") or "")
    if not is_custom_model(model):
        return request_kwargs
    parts = split_custom_model_id(model)
    if parts is None:
        return request_kwargs
    endpoint_name, raw_id = parts
    entry = endpoint_entry(endpoint_name, cfg)
    if entry is None:
        return request_kwargs
    base_url = str(entry.get("base_url") or "").strip()
    if not base_url:
        return request_kwargs
    patched = dict(request_kwargs)
    patched["model"] = f"openai/{raw_id}"
    patched["api_base"] = base_url
    patched.setdefault("api_key", resolve_endpoint_api_key(entry) or NO_KEY_PLACEHOLDER)
    return patched


__all__ = [
    "CUSTOM_PREFIX",
    "NO_KEY_PLACEHOLDER",
    "active_store_root",
    "apply_custom_transport",
    "endpoint_entry",
    "is_custom_model",
    "resolve_endpoint_api_key",
    "set_active_store_root",
]
