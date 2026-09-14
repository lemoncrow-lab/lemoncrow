"""The ``providers.json`` writer: register self-hosted OpenAI-compatible endpoints.

Why this module exists: until ``lc model add`` there was no writer at all.
Every provider path in the repo *reads* ``providers.json``; the only thing that
ever wrote anything was ``discovery._write_example``, and it writes
``providers.json.example``. Introducing the first real writer means honouring
three properties the readers silently assume:

* **merge-preserving** -- the file is hand-edited, so a registration is a
  read-modify-write over the parsed document, never a rewrite from a partial
  model. An unknown top-level key survives untouched.
* **atomic and serialized** -- other processes read it (and a second ``lc
  model`` invocation may write it), so the critical section is guarded by an
  in-process lock plus a POSIX flock sidecar, and the new text arrives via
  ``os.replace`` from a sibling temp file.
* **0600** -- credentials can land here. The mode is set on the temp file
  *before* the rename, so the key is never briefly world-readable.

A malformed file is a hard stop rather than a degraded path: silently rewriting
JSON we failed to parse would delete the user's other providers. Read-only
queries (``list_endpoints``) still degrade to empty, because reporting must
never raise.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from lemoncrow.core.capabilities.providers.config import providers_config_path
from lemoncrow.core.capabilities.providers.discovery import _fetch_openai_compat, invalidate_cache

from .models import (
    CUSTOM_PREFIX,
    CUSTOM_PROVIDER,
    MODEL_TIER_VALUES,
    CustomEndpoint,
    CustomModel,
    ModelTier,
    ProbeResult,
    custom_model_id,
)
from .transport import active_store_root, resolve_endpoint_api_key

if TYPE_CHECKING:  # importing the routing table at module scope would be a cycle
    from lemoncrow.pro.capabilities.counterfactual.pricing import CandidateModel

_ENDPOINTS_KEY = "endpoints"
_MODELS_KEY = "models"
_SECRET_MODE = 0o600

# What routing assumes when nothing measured the real limit. Deliberately small:
# an over-stated window routes a turn to a model that will truncate it.
_ASSUMED_CONTEXT_WINDOW = 8192

# Serialize the read-modify-write critical section across threads in this
# process; the flock sidecar covers sibling processes. Precedent:
# pro/capabilities/team/workspace.py:47-108.
_WRITE_LOCK = threading.Lock()


def write_providers_config(root: Path | None, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """Read-modify-write ``providers.json`` atomically, preserving unknown keys.

    ``mutate`` receives the parsed document and edits it in place. It runs
    inside the lock, so it sees the newest content on disk -- a concurrent
    writer's endpoint is merged, not clobbered.
    """

    path = providers_config_path(_effective_root(root))
    with _WRITE_LOCK:
        lock_handle = _acquire_flock(path)
        try:
            raw = _read_config(path)
            mutate(raw)
            _atomic_write_secret(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        finally:
            _release_flock(lock_handle)
    return path


def add_endpoint(
    root: Path | None,
    base_url: str,
    *,
    name: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    models: Sequence[str] | None = None,
    tier: ModelTier = "cheap",
    context_window: int | None = None,
    timeout: float = 10.0,
) -> CustomEndpoint:
    """Register one OpenAI-compatible endpoint and the models it serves.

    ``models`` short-circuits discovery entirely, which is what makes an
    endpoint that does not implement ``GET /models`` registrable at all. When it
    is omitted the endpoint is asked, and an empty answer is an error: silently
    writing an endpoint with no models would produce a registration that can
    never route.

    Raises ``ValueError`` (never ``ClickException`` -- this is not a CLI module)
    for an unusable URL, a name collision, or an endpoint that reports nothing.
    """

    normalized = normalize_base_url(base_url)
    endpoint_name = (name or "").strip() or derive_endpoint_name(normalized)
    if not endpoint_name:
        raise ValueError(f"cannot derive an endpoint name from {base_url!r}; pass --name")

    existing = _endpoints_from_disk(root)
    if endpoint_name in existing:
        current = str((existing.get(endpoint_name) or {}).get("base_url") or "")
        raise ValueError(
            f"endpoint {endpoint_name!r} is already registered ({current}); "
            f"pass --name to register this one under a different alias"
        )

    raw_ids = [rid.strip() for rid in (models or []) if rid.strip()]
    if not raw_ids:
        raw_ids = discover_endpoint_models(
            normalized,
            _probe_key(api_key, api_key_env),
            timeout=timeout,
        )
    if not raw_ids:
        raise ValueError(
            f"no models reported by {normalized} -- is the server running and OpenAI-compatible? "
            f"Pass --model ID to register without discovery"
        )

    entry: dict[str, Any] = {
        "base_url": normalized,
        # Present-but-empty when the key lives in the environment: the shape
        # documented in providers.json.example stays stable either way.
        "api_key": api_key or "",
        "api_key_env": api_key_env or None,
        _MODELS_KEY: {raw_id: _new_model_entry(tier, context_window) for raw_id in raw_ids},
    }

    def _mutate(raw: dict[str, Any]) -> None:
        endpoints = _endpoints_map(raw, create=True)
        if endpoint_name in endpoints:
            # Lost the race with a sibling writer between the pre-check and the
            # lock. Refuse rather than overwrite someone else's credential.
            raise ValueError(f"endpoint {endpoint_name!r} was registered concurrently; pass --name")
        endpoints[endpoint_name] = entry

    write_providers_config(root, _mutate)
    invalidate_cache()
    return _endpoint_from_entry(endpoint_name, entry)


def list_endpoints(root: Path | None) -> list[CustomEndpoint]:
    """Every registered endpoint, name-sorted. Degrades to ``[]``, never raises."""

    endpoints = _endpoints_from_disk(root)
    return [_endpoint_from_entry(name, endpoints[name]) for name in sorted(endpoints)]


def get_endpoint(root: Path | None, name: str) -> CustomEndpoint | None:
    """One registered endpoint, or ``None`` when it is not registered."""

    entry = _endpoints_from_disk(root).get(name)
    return _endpoint_from_entry(name, entry) if entry is not None else None


def remove_endpoint(root: Path | None, name: str) -> bool:
    """Drop one endpoint. ``False`` when it was not there -- removal is idempotent."""

    if name not in _endpoints_from_disk(root):
        return False

    removed = False

    def _mutate(raw: dict[str, Any]) -> None:
        nonlocal removed
        endpoints = _endpoints_map(raw, create=False)
        removed = endpoints.pop(name, None) is not None

    write_providers_config(root, _mutate)
    if removed:
        invalidate_cache()
    return removed


def record_probe(root: Path | None, result: ProbeResult) -> bool:
    """Merge one probe into the endpoint's ``models`` map. ``False`` when it is gone.

    A capability the probe could not measure (``None``) leaves the stored value
    alone. Unknown must not erase knowledge: an endpoint that was down during a
    re-probe would otherwise silently lose the tool-calling support a previous
    run measured, and routing would quietly demote it forever. A *measured*
    negative (``unsupported`` -> ``False``) does overwrite -- that is evidence.

    Creates the model entry when the endpoint serves something registration did
    not see, so probing a newly loaded model is a one-liner.
    """

    if result.endpoint_name not in _endpoints_from_disk(root):
        return False

    persisted = False

    def _mutate(raw: dict[str, Any]) -> None:
        nonlocal persisted
        entry = _endpoints_map(raw, create=False).get(result.endpoint_name)
        if not isinstance(entry, dict):
            return
        models = entry.get(_MODELS_KEY)
        if not isinstance(models, dict):
            models = {}
            entry[_MODELS_KEY] = models
        current = models.get(result.raw_model_id)
        merged = dict(current) if isinstance(current, dict) else _new_model_entry("cheap", None)
        _apply_probe(merged, result)
        models[result.raw_model_id] = merged
        persisted = True

    write_providers_config(root, _mutate)
    if persisted:
        invalidate_cache()
    return persisted


def user_candidate_models(root: Path | None = None) -> list[CandidateModel]:
    """Registered models as routing candidates, in the shape the router already uses.

    This is the whole integration: ``counterfactual/pricing._build_candidates``
    appends this list, so a self-hosted box competes with the vendor models
    through the existing comparison instead of a parallel abstraction.

    ``supports_tool_use`` collapses unknown to ``False`` **here and only here**.
    ``supports_turn`` rejects a candidate that cannot call tools, so an unknown
    routed optimistically would fail the turn at dispatch; declining to route to
    it costs nothing but a probe. The stored value stays ``None``.
    """

    from lemoncrow.core.capabilities.pricing import get_model_pricing
    from lemoncrow.pro.capabilities.counterfactual.pricing import CandidateModel

    candidates: list[CandidateModel] = []
    for endpoint in list_endpoints(root):
        for model in endpoint.models:
            model_id = custom_model_id(endpoint.name, model.raw_id)
            candidates.append(
                CandidateModel(
                    vendor=CUSTOM_PROVIDER,
                    model_id=model_id,
                    tier=model.tier,
                    # Unknown to the price table => zero-cost, which is what a
                    # box the user already owns actually costs per token.
                    pricing=get_model_pricing(model_id),
                    supports_tool_use=bool(model.supports_tool_use),
                    context_window=model.context_window or _ASSUMED_CONTEXT_WINDOW,
                )
            )
    return candidates


def discover_endpoint_models(base_url: str, api_key: str, *, timeout: float = 10.0) -> list[str]:
    """Raw model ids reported by ``GET {base_url}/models``; ``[]`` when unreachable.

    ``_fetch_openai_compat`` returns litellm-namespaced ids. Registration keys
    models by the id the *server* reports, so the prefix comes back off here.
    """

    fetched = _fetch_openai_compat(CUSTOM_PROVIDER, normalize_base_url(base_url), api_key, timeout=timeout)
    raw_ids: list[str] = []
    for model_id in fetched:
        raw = model_id[len(CUSTOM_PREFIX) :] if model_id.startswith(CUSTOM_PREFIX) else model_id
        if raw and raw not in raw_ids:
            raw_ids.append(raw)
    return raw_ids


def normalize_base_url(base_url: str) -> str:
    """Canonicalise a user-typed endpoint URL. ``localhost:8000`` gets a scheme."""

    text = base_url.strip().rstrip("/")
    if not text:
        raise ValueError("endpoint URL is empty")
    if "://" not in text:
        text = f"http://{text}"
    if not urlparse(text).netloc:
        raise ValueError(f"{base_url!r} is not a usable endpoint URL")
    return text


def derive_endpoint_name(base_url: str) -> str:
    """``http://localhost:8000/v1`` -> ``localhost-8000``.

    Any ``user:password@`` prefix is dropped before the name is derived: an
    endpoint alias is an identifier and must not carry a credential.
    """

    host = urlparse(normalize_base_url(base_url)).netloc.rsplit("@", 1)[-1]
    cleaned = host.lower().replace(".", "-").replace(":", "-")
    return "".join(ch for ch in cleaned if ch.isalnum() or ch in "-_").strip("-")


def _probe_key(api_key: str | None, api_key_env: str | None) -> str:
    """The credential to use for discovery, before anything is persisted."""

    return resolve_endpoint_api_key({"api_key": api_key or "", "api_key_env": api_key_env or ""})


def _new_model_entry(tier: ModelTier, context_window: int | None) -> dict[str, Any]:
    """A freshly registered, unprobed model: every capability honestly unknown."""

    return {
        "supports_tool_use": None,
        "supports_structured_output": None,
        "supports_vision": None,
        "context_window": context_window,
        "tier": tier,
        "observed_tokens_per_second": None,
        "probed_at": None,
    }


def _apply_probe(entry: dict[str, Any], result: ProbeResult) -> None:
    """Overwrite only what this probe actually measured; keep the rest."""

    measured: tuple[tuple[str, Any], ...] = (
        ("supports_tool_use", result.supports_tool_use),
        ("supports_structured_output", result.supports_structured_output),
        ("supports_vision", result.supports_vision),
        ("context_window", result.context_window),
        ("observed_tokens_per_second", result.observed_tokens_per_second),
    )
    for key, value in measured:
        if value is not None:
            entry[key] = value
    # The timestamp records that a probe ran, not that it succeeded -- "probed
    # 10 minutes ago and still unknown" is the sentence the user needs.
    entry["probed_at"] = result.probed_at or None
    entry.setdefault("tier", "cheap")


def _endpoints_map(raw: dict[str, Any], *, create: bool) -> dict[str, Any]:
    """The ``custom.endpoints`` sub-document, repairing a non-dict shape in place."""

    custom = raw.get(CUSTOM_PROVIDER)
    if not isinstance(custom, dict):
        if not create:
            return {}
        custom = {}
        raw[CUSTOM_PROVIDER] = custom
    endpoints = custom.get(_ENDPOINTS_KEY)
    if not isinstance(endpoints, dict):
        if not create:
            return {}
        endpoints = {}
        custom[_ENDPOINTS_KEY] = endpoints
    return endpoints


def _effective_root(root: Path | None) -> Path | None:
    """An explicit root wins; otherwise the root ``lc --root`` pointed us at.

    ``user_candidate_models()`` is called with no argument from the cached
    routing table, so without this the router looks for a ``--root X``
    endpoint in ~/.lemoncrow and reports no candidates. Still ``None`` when
    nothing redirected the process, which is the pre-existing default.
    """

    return root if root is not None else active_store_root()


def _endpoints_from_disk(root: Path | None) -> dict[str, Any]:
    """Registered endpoints as raw dicts. A broken file reads as "none"."""

    path = providers_config_path(_effective_root(root))
    try:
        raw = _read_config(path)
    except ValueError:
        return {}
    endpoints = _endpoints_map(raw, create=False)
    return {name: entry for name, entry in endpoints.items() if isinstance(entry, dict)}


def _endpoint_from_entry(name: str, entry: dict[str, Any]) -> CustomEndpoint:
    models_raw = entry.get(_MODELS_KEY)
    models_map: dict[str, Any] = models_raw if isinstance(models_raw, dict) else {}
    return CustomEndpoint(
        name=name,
        base_url=str(entry.get("base_url") or ""),
        api_key_env=(str(entry.get("api_key_env")) if entry.get("api_key_env") else None),
        models=tuple(_model_from_entry(raw_id, models_map[raw_id]) for raw_id in sorted(models_map)),
    )


def _model_from_entry(raw_id: str, entry: Any) -> CustomModel:
    data: dict[str, Any] = entry if isinstance(entry, dict) else {}
    return CustomModel(
        raw_id=raw_id,
        supports_tool_use=_opt_bool(data.get("supports_tool_use")),
        supports_structured_output=_opt_bool(data.get("supports_structured_output")),
        supports_vision=_opt_bool(data.get("supports_vision")),
        context_window=_opt_int(data.get("context_window")),
        tier=_tier(data.get("tier")),
        observed_tokens_per_second=_opt_float(data.get("observed_tokens_per_second")),
        probed_at=(str(data.get("probed_at")) if data.get("probed_at") else None),
    )


def _opt_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _tier(value: Any) -> ModelTier:
    """Anything unrecognised reads as "cheap" -- the conservative routing tier."""

    return "high" if isinstance(value, str) and value == MODEL_TIER_VALUES[1] else "cheap"


def _read_config(path: Path) -> dict[str, Any]:
    """Parse ``providers.json``. Raises ``ValueError`` rather than lose content."""

    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return {}
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or move it aside first") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} must hold a JSON object, found {type(parsed).__name__}")
    return parsed


def _atomic_write_secret(path: Path, text: str) -> None:
    """Write via a sibling temp file so no reader ever sees a truncated config.

    The temp name is unique per writer (``mkstemp``), so two concurrent writers
    cannot write through the same scratch file, and the 0600 mode is applied
    *before* the rename so the credential is never momentarily world-readable.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, _SECRET_MODE)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _acquire_flock(path: Path) -> Any:
    """Best-effort cross-process lock; ``None`` when the platform has no flock."""

    try:
        import fcntl
    except ImportError:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.parent / f"{path.name}.lock", "w", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle
    except OSError:
        return None


def _release_flock(handle: Any) -> None:
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass
    try:
        handle.close()
    except OSError:
        pass


__all__ = [
    "add_endpoint",
    "derive_endpoint_name",
    "discover_endpoint_models",
    "get_endpoint",
    "list_endpoints",
    "normalize_base_url",
    "record_probe",
    "remove_endpoint",
    "user_candidate_models",
    "write_providers_config",
]
