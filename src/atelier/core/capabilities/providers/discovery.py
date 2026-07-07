"""Dynamic model discovery from each configured provider's API.

Each provider is polled once per process (cached in memory).
Returns litellm-compatible model IDs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

from .config import LITELLM_PREFIX

logger = logging.getLogger(__name__)

_cache: list[str] | None = None
_cache_lock = asyncio.Lock()


def _litellm_id(provider: str, model: str) -> str:
    """Return a litellm-compatible model ID for the given provider/model pair."""
    if provider in ("anthropic", "openai", "google"):
        return model  # litellm resolves these from the bare model id
    prefix = LITELLM_PREFIX.get(provider, "")
    if not prefix or model.startswith(prefix):
        return model
    return f"{prefix}{model}"


def _fetch_openai_compat(
    provider: str,
    base_url: str,
    api_key: str,
    *,
    filter_prefix: str | None = None,
) -> list[str]:
    """Fetch models from an OpenAI-compatible /v1/models endpoint."""
    import urllib.request

    url = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data: dict[str, Any] = json.loads(resp.read())
        items = data.get("data") or []
        ids = []
        for item in items:
            mid = item.get("id") or ""
            if filter_prefix and not mid.startswith(filter_prefix):
                continue
            ids.append(_litellm_id(provider, mid))
        return ids
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery %s: %s", provider, exc)
        return []


def _fetch_anthropic(cfg: Any) -> list[str]:
    import urllib.request

    api_key = cfg.get("anthropic", "api_key")
    if not api_key:
        return []
    base = cfg.get("anthropic", "base_url") or "https://api.anthropic.com"
    url = f"{base.rstrip('/')}/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data: dict[str, Any] = json.loads(resp.read())
        models = []
        for item in data.get("data") or []:
            mid = item.get("id") or ""
            if mid:
                models.append(mid)
        return models
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery anthropic: %s", exc)
        return []


def _fetch_google(cfg: Any) -> list[str]:
    import urllib.request

    api_key = cfg.get("google", "api_key")
    if not api_key:
        return []
    # Send the key via header, not the URL query string (URLs land in proxy/
    # server logs and redirect headers).
    url = "https://generativelanguage.googleapis.com/v1/models"
    req = urllib.request.Request(url, headers={"x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data: dict[str, Any] = json.loads(resp.read())
        models = []
        for item in data.get("models") or []:
            name = item.get("name") or ""
            # name is "models/gemini-2.0-flash" → strip prefix
            mid = name.replace("models/", "") if name.startswith("models/") else name
            if mid:
                models.append(mid)
        return models
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery google: %s", exc)
        return []


def _fetch_ollama(cfg: Any) -> list[str]:
    import urllib.request

    base = cfg.get("ollama", "base_url") or "http://localhost:11434"
    url = f"{base.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data: dict[str, Any] = json.loads(resp.read())
        models = []
        for item in data.get("models") or []:
            name = item.get("name") or item.get("model") or ""
            if name:
                models.append(f"ollama/{name}")
        return models
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery ollama: %s", exc)
        return []


def _fetch_bedrock(cfg: Any) -> list[str]:
    """List Bedrock foundation models via boto3."""
    try:
        import boto3  # type: ignore[import-untyped]
        import botocore  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("model discovery bedrock: boto3 not installed")
        return []
    try:
        import os

        # Allow AWS_REGION as alias for AWS_DEFAULT_REGION (Claude Code convention)
        region = cfg.get("bedrock", "aws_default_region") or cfg.get("bedrock", "aws_region") or "us-east-1"
        # If using bearer token, set it as env var for boto3
        bearer = cfg.get("bedrock", "aws_bearer_token_bedrock")
        if bearer and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer
        client = boto3.client("bedrock", region_name=region)
        resp = client.list_foundation_models(byOutputModality="TEXT")
        models = []
        for s in resp.get("modelSummaries") or []:
            mid = s.get("modelId") or ""
            if mid and "ON_DEMAND" in (s.get("inferenceTypesSupported") or []):
                models.append(f"bedrock/{mid}")
        return models
    except botocore.exceptions.NoCredentialsError:
        logger.debug("model discovery bedrock: no credentials found")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery bedrock: %s", exc)
        return []


def _fetch_vertex(cfg: Any) -> list[str]:
    """List Vertex AI publisher models via the REST discovery API."""
    import urllib.error
    import urllib.request

    project = cfg.get("vertex", "project")
    if not project:
        return []
    sa_file = cfg.get("vertex", "application_credentials")
    try:
        if sa_file:
            # Use service-account token exchange
            import base64
            import time

            sa_data = json.loads(Path(sa_file).read_text())
            email = sa_data["client_email"]
            key_pem = sa_data["private_key"]
            scope = "https://www.googleapis.com/auth/cloud-platform"
            now = int(time.time())
            header_b64 = (
                base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
            )
            payload_b64 = (
                base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "iss": email,
                            "scope": scope,
                            "aud": "https://oauth2.googleapis.com/token",
                            "exp": now + 3600,
                            "iat": now,
                        }
                    ).encode()
                )
                .rstrip(b"=")
                .decode()
            )
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            priv = load_pem_private_key(key_pem.encode(), password=None)
            sig = cast(RSAPrivateKey, priv).sign(
                f"{header_b64}.{payload_b64}".encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
            jwt = f"{header_b64}.{payload_b64}.{sig_b64}"
            tok_req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=f"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={jwt}".encode(),
                method="POST",
            )
            with urllib.request.urlopen(tok_req, timeout=5) as r:
                token = json.loads(r.read())["access_token"]
        else:
            # Try gcloud ADC via metadata server (GCE/GKE)
            meta_req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(meta_req, timeout=2) as r:
                token = json.loads(r.read())["access_token"]

        url = "https://us-central1-aiplatform.googleapis.com/v1beta1/publishers/google/models?pageSize=100"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        models = []
        for item in data.get("publisherModels") or []:
            name = item.get("name") or ""
            mid = name.split("/")[-1] if "/" in name else name
            if mid:
                models.append(f"vertex_ai/{mid}")
        return models
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery vertex: %s", exc)
        return []


def _fetch_azure(cfg: Any) -> list[str]:
    """List Azure OpenAI deployments via the management REST API."""
    import urllib.request

    api_key = cfg.get("azure", "api_key")
    endpoint = cfg.get("azure", "endpoint")
    if not api_key or not endpoint:
        return []
    # List deployments
    url = f"{endpoint.rstrip('/')}/openai/deployments?api-version=2024-05-01-preview"
    req = urllib.request.Request(url, headers={"api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models = []
        for item in data.get("data") or []:
            did = item.get("id") or ""
            if did:
                models.append(f"azure/{did}")
        return models
    except Exception as exc:  # noqa: BLE001
        logger.debug("model discovery azure: %s", exc)
        return []


async def _discover_all(cfg: Any) -> list[str]:
    # The fetchers are synchronous (urllib/boto3); run each in a worker thread
    # so the event loop stays free and providers are polled concurrently.
    tasks: list[Any] = []
    # Anthropic
    if cfg.is_configured("anthropic"):
        tasks.append(asyncio.to_thread(_fetch_anthropic, cfg))
    # OpenAI
    if cfg.is_configured("openai"):
        api_key = cfg.get("openai", "api_key") or ""
        base = cfg.get("openai", "base_url") or "https://api.openai.com/v1"
        tasks.append(asyncio.to_thread(_fetch_openai_compat, "openai", base, api_key))
    # Groq
    if cfg.is_configured("groq"):
        api_key = cfg.get("groq", "api_key") or ""
        tasks.append(asyncio.to_thread(_fetch_openai_compat, "groq", "https://api.groq.com/openai/v1", api_key))
    # Mistral
    if cfg.is_configured("mistral"):
        api_key = cfg.get("mistral", "api_key") or ""
        tasks.append(asyncio.to_thread(_fetch_openai_compat, "mistral", "https://api.mistral.ai/v1", api_key))
    # OpenRouter
    if cfg.is_configured("openrouter"):
        api_key = cfg.get("openrouter", "api_key") or ""
        tasks.append(asyncio.to_thread(_fetch_openai_compat, "openrouter", "https://openrouter.ai/api/v1", api_key))
    # Together
    if cfg.is_configured("together"):
        api_key = cfg.get("together", "api_key") or ""
        tasks.append(asyncio.to_thread(_fetch_openai_compat, "together", "https://api.together.xyz/v1", api_key))
    # Fireworks
    if cfg.is_configured("fireworks"):
        api_key = cfg.get("fireworks", "api_key") or ""
        tasks.append(
            asyncio.to_thread(_fetch_openai_compat, "fireworks", "https://api.fireworks.ai/inference/v1", api_key)
        )
    # Google Gemini
    if cfg.is_configured("google"):
        tasks.append(asyncio.to_thread(_fetch_google, cfg))
    # Ollama
    if cfg.is_configured("ollama"):
        tasks.append(asyncio.to_thread(_fetch_ollama, cfg))
    # Bedrock
    if cfg.is_configured("bedrock"):
        tasks.append(asyncio.to_thread(_fetch_bedrock, cfg))
    # Vertex AI
    if cfg.is_configured("vertex"):
        tasks.append(asyncio.to_thread(_fetch_vertex, cfg))
    # Azure OpenAI
    if cfg.is_configured("azure"):
        tasks.append(asyncio.to_thread(_fetch_azure, cfg))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    models: list[str] = []
    seen: set[str] = set()
    for result in results:
        if isinstance(result, list):
            for m in result:
                if m and m not in seen:
                    models.append(m)
                    seen.add(m)
    return models


async def discover_models(root: Path | str | None = None) -> list[str]:
    """Return litellm-compatible model IDs for all configured providers.

    Results are cached in process memory after the first call.
    On first run, creates ``~/.atelier/providers.json.example`` if it doesn't exist.
    """
    global _cache
    async with _cache_lock:
        if _cache is not None:
            return list(_cache)
        from .config import load_providers_config, providers_config_path

        cfg = load_providers_config(root)
        cfg.export_env()
        _cache = await _discover_all(cfg)
        if not _cache:
            _emit_setup_hint(providers_config_path(root))
        return list(_cache)


def _emit_setup_hint(config_path: Path) -> None:
    """Log a one-time hint when no providers are configured."""
    import sys

    example_path = config_path.parent / "providers.json.example"
    if not example_path.exists():
        _write_example(example_path)
    hint = (
        "\nNo provider credentials found — /v1/models is empty.\n"
        f"  1. Edit {config_path}  (create from example: {example_path})\n"
        "  2. Add your API key(s) for anthropic, openai, groq, ollama, etc.\n"
        "  3. Restart the service, or call GET /v1/models/refresh\n"
        "  Docs: see docs/openai-gateway.md#configuring-providers\n"
    )
    sys.stderr.write(hint)


def _write_example(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        '  "_comment": "Copy to providers.json and fill in your keys. '
        'Values here supplement (but never override) environment variables.",\n\n'
        '  "anthropic":  {"api_key": "sk-ant-..."},\n'
        '  "openai":     {"api_key": "sk-...", "base_url": "https://api.openai.com/v1"},\n'
        '  "google":     {"api_key": "AIza..."},\n'
        '  "bedrock":    {"aws_bearer_token_bedrock": "...", "aws_region": "us-east-1"},\n'
        '  "vertex":     {"project": "my-gcp-project", "application_credentials": "/path/to/sa.json"},\n'
        '  "azure":      {"api_key": "...", "endpoint": "https://my-resource.openai.azure.com"},\n'
        '  "openrouter": {"api_key": "sk-or-..."},\n'
        '  "groq":       {"api_key": "gsk_..."},\n'
        '  "mistral":    {"api_key": "..."},\n'
        '  "ollama":     {"base_url": "http://localhost:11434"},\n'
        '  "together":   {"api_key": "..."},\n'
        '  "fireworks":  {"api_key": "..."}\n'
        "}\n",
        encoding="utf-8",
    )


def invalidate_cache() -> None:
    """Clear the cached model list (e.g., after config change)."""
    global _cache
    _cache = None
