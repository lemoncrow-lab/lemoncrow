"""Interactive provider authentication wizard."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROVIDER_CONFIGS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "name": "Anthropic (Claude)",
        "link": "https://console.anthropic.com/settings/keys",
        "fields": [{"name": "ANTHROPIC_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "claude-haiku-4-5",
        "litellm_prefix": "anthropic/",
    },
    "openai": {
        "name": "OpenAI (GPT-4o)",
        "link": "https://platform.openai.com/api-keys",
        "fields": [{"name": "OPENAI_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "gpt-4o-mini",
        "litellm_prefix": "openai/",
    },
    "google": {
        "name": "Google / Gemini",
        "link": "https://ai.google.dev/gemini-api/docs/api-key",
        "fields": [{"name": "GOOGLE_API_KEY", "label": "API Key (GOOGLE_API_KEY)", "secret": True}],
        "test_model": "gemini-2.0-flash",
        "litellm_prefix": "gemini/",
    },
    "groq": {
        "name": "Groq (Ultra-fast inference)",
        "link": "https://console.groq.com/keys",
        "fields": [{"name": "GROQ_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "llama-3.1-8b-instant",
        "litellm_prefix": "groq/",
    },
    "mistral": {
        "name": "Mistral AI",
        "link": "https://console.mistral.ai/api-keys",
        "fields": [{"name": "MISTRAL_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "mistral-small-latest",
        "litellm_prefix": "mistral/",
    },
    "openrouter": {
        "name": "OpenRouter (All providers, 1 key)",
        "link": "https://openrouter.ai/keys",
        "fields": [{"name": "OPENROUTER_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "openrouter/anthropic/claude-haiku-4-5",
        "litellm_prefix": "openrouter/",
    },
    "ollama": {
        "name": "Ollama (Local models, free)",
        "link": "https://ollama.ai",
        "fields": [
            {
                "name": "OLLAMA_HOST",
                "label": "Host URL (default: http://localhost:11434)",
                "secret": False,
                "default": "http://localhost:11434",
            }
        ],
        "test_model": "llama3.2",
        "litellm_prefix": "ollama/",
    },
    "together": {
        "name": "Together AI",
        "link": "https://api.together.xyz/settings/api-keys",
        "fields": [{"name": "TOGETHER_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "litellm_prefix": "together_ai/",
    },
    "bedrock": {
        "name": "AWS Bedrock (Claude/Llama via AWS)",
        "link": "https://docs.aws.amazon.com/bedrock/",
        "fields": [
            {"name": "AWS_ACCESS_KEY_ID", "label": "AWS Access Key ID", "secret": True},
            {"name": "AWS_SECRET_ACCESS_KEY", "label": "AWS Secret Access Key", "secret": True},
            {
                "name": "AWS_REGION_NAME",
                "label": "AWS Region (e.g. us-east-1)",
                "secret": False,
                "default": "us-east-1",
            },
        ],
        "test_model": "bedrock/anthropic.claude-haiku-4-5-v1:0",
        "litellm_prefix": "bedrock/",
    },
    "azure": {
        "name": "Azure OpenAI",
        "link": "https://portal.azure.com",
        "fields": [
            {"name": "AZURE_API_KEY", "label": "Azure API Key", "secret": True},
            {
                "name": "AZURE_API_BASE",
                "label": "Azure Endpoint URL (e.g. https://xxx.openai.azure.com/)",
                "secret": False,
            },
            {
                "name": "AZURE_API_VERSION",
                "label": "API Version (e.g. 2024-02-01)",
                "secret": False,
                "default": "2024-02-01",
            },
        ],
        "test_model": "azure/gpt-4o-mini",
        "litellm_prefix": "azure/",
    },
    "vertex": {
        "name": "GCP Vertex AI (Claude/Gemini via Google Cloud)",
        "link": "https://cloud.google.com/vertex-ai",
        "fields": [
            {"name": "VERTEXAI_PROJECT", "label": "GCP Project ID", "secret": False},
            {
                "name": "VERTEXAI_LOCATION",
                "label": "Region (e.g. us-central1)",
                "secret": False,
                "default": "us-central1",
            },
            {"name": "GOOGLE_APPLICATION_CREDENTIALS", "label": "Service account JSON path", "secret": False},
        ],
        "test_model": "vertex_ai/gemini-2.0-flash",
        "litellm_prefix": "vertex_ai/",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "link": "https://fireworks.ai/api-keys",
        "fields": [{"name": "FIREWORKS_API_KEY", "label": "API Key", "secret": True}],
        "test_model": "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct",
        "litellm_prefix": "fireworks_ai/",
    },
}


def credentials_path() -> Path:
    from lemoncrow.core.foundation.paths import default_store_root

    return default_store_root() / ".env"


def load_saved_credentials() -> dict[str, str]:
    """Load credentials from ~/.lemoncrow/.env"""
    path = credentials_path()
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def save_credentials(credentials: dict[str, str]) -> None:
    """Save credentials to ~/.lemoncrow/.env with owner-only permissions."""
    path = credentials_path()
    parent = path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    # The directory holds secret credentials; restrict it to the owner. Only
    # tighten permissions on a directory we just created so we never relax or
    # silently override an operator's deliberate setup on a pre-existing dir.
    if not parent_existed:
        os.chmod(parent, 0o700)
    # Merge with existing
    existing = load_saved_credentials()
    existing.update(credentials)
    lines = ["# LemonCrow provider credentials — managed by `lc auth`", ""]
    for k, v in sorted(existing.items()):
        lines.append(f'{k}="{v}"')
    # Create (or truncate) the file with owner-only permissions before writing
    # any secrets, so the keys are never briefly world-readable on disk.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    finally:
        # If the file already existed its mode is unchanged by os.open, so
        # enforce owner-only perms explicitly to repair any prior loose state.
        os.chmod(path, 0o600)
    # Set in current process env
    for k, v in credentials.items():
        os.environ[k] = v


def load_env_into_process() -> None:
    """Load ~/.lemoncrow/.env into the current process environment."""
    for k, v in load_saved_credentials().items():
        if k not in os.environ:
            os.environ[k] = v


def validate_provider(provider_id: str, credentials: dict[str, str]) -> tuple[bool, str]:
    """Validate credentials by making a test API call. Returns (ok, message)."""
    cfg = PROVIDER_CONFIGS.get(provider_id)
    if not cfg:
        return False, f"Unknown provider: {provider_id}"

    # Temporarily set env vars
    old_env: dict[str, str] = {}
    for field in cfg["fields"]:
        key = field["name"]
        if key in credentials:
            old_env[key] = os.environ.get(key, "")
            os.environ[key] = credentials[key]

    try:
        from lemoncrow.infra.internal_llm.litellm_client import chat_with_result

        test_model = cfg["test_model"]
        result = chat_with_result(
            [{"role": "user", "content": "hi"}],
            model=test_model,
            extra_kwargs={"max_tokens": 5},
        )
        return True, f"✓ Connected to {cfg['name']} ({result.model or test_model})"
    except Exception as exc:  # noqa: BLE001 - validation surfaces any failure to the user
        return False, f"✗ Failed: {str(exc)[:200]}"
    finally:
        # Restore original env
        for k, v in old_env.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def list_provider_models(provider_id: str) -> list[str]:
    """List available models for a provider (best-effort)."""
    from lemoncrow.core.capabilities.counterfactual.pricing import load_pricing_table

    table = load_pricing_table()
    candidates = table.candidates_for_vendor(provider_id)
    return [c.model_id for c in candidates]


__all__ = [
    "PROVIDER_CONFIGS",
    "credentials_path",
    "list_provider_models",
    "load_env_into_process",
    "load_saved_credentials",
    "save_credentials",
    "validate_provider",
]
