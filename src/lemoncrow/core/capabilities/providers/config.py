"""Load and save ~/.lemoncrow/providers.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

from lemoncrow.core.foundation.paths import default_store_root

# litellm provider-prefix map; bare model names get auto-prefixed.
# Single source of truth shared by route resolution (cli/commands/run.py),
# model discovery, and rate limiting.
LITELLM_PREFIX: dict[str, str] = {
    "anthropic": "anthropic/",
    "openai": "openai/",
    "google": "gemini/",
    "gemini": "gemini/",
    "azure": "azure/",
    "bedrock": "bedrock/",
    "cohere": "cohere/",
    "mistral": "mistral/",
    "ollama": "ollama/",
    "together": "together_ai/",
    "groq": "groq/",
    "fireworks": "fireworks_ai/",
    "vertex": "vertex_ai/",
    "huggingface": "huggingface/",
    "replicate": "replicate/",
    "deepinfra": "deepinfra/",
    "perplexity": "perplexity/",
    "openrouter": "openrouter/",
}


def providers_config_path(root: Path | str | None = None) -> Path:
    base = Path(root).expanduser().resolve() if root is not None else default_store_root()
    return base / "providers.json"


class ProviderConfig:
    """Merged view of providers.json + environment variables.

    Schema (providers.json)::

        {
          "anthropic":  {"api_key": "sk-ant-...", "base_url": null},
          "openai":     {"api_key": "sk-...",      "base_url": "https://..."},
          "google":     {"api_key": "AIza..."},
          "bedrock":    {"aws_access_key_id": "...", "aws_secret_access_key": "...",
                         "aws_default_region": "us-east-1"},
          "vertex":     {"project": "my-gcp-project",
                         "application_credentials": "/path/to/sa.json"},
          "azure":      {"api_key": "...", "endpoint": "https://my-resource.openai.azure.com"},
          "openrouter": {"api_key": "sk-or-..."},
          "groq":       {"api_key": "gsk_..."},
          "mistral":    {"api_key": "..."},
          "ollama":     {"base_url": "http://localhost:11434"},
          "together":   {"api_key": "..."},
          "fireworks":  {"api_key": "..."}
        }

    Keys in the file supplement (but never override) environment variables.
    """

    # env var fallbacks per provider key
    _ENV: ClassVar[dict[str, dict[str, str]]] = {
        "anthropic": {"api_key": "ANTHROPIC_API_KEY"},
        "openai": {"api_key": "OPENAI_API_KEY"},
        "google": {"api_key": "GOOGLE_API_KEY"},
        "bedrock": {
            "aws_access_key_id": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "aws_default_region": "AWS_DEFAULT_REGION",
            "aws_region": "AWS_REGION",
            "aws_profile": "AWS_PROFILE",
            "aws_bearer_token_bedrock": "AWS_BEARER_TOKEN_BEDROCK",
        },
        "vertex": {
            "project": "VERTEXAI_PROJECT",
            "application_credentials": "GOOGLE_APPLICATION_CREDENTIALS",
        },
        "azure": {"api_key": "AZURE_OPENAI_API_KEY", "endpoint": "AZURE_OPENAI_ENDPOINT"},
        "openrouter": {"api_key": "OPENROUTER_API_KEY"},
        "groq": {"api_key": "GROQ_API_KEY"},
        "mistral": {"api_key": "MISTRAL_API_KEY"},
        "ollama": {"base_url": "OLLAMA_HOST"},
        "together": {"api_key": "TOGETHER_API_KEY"},
        "fireworks": {"api_key": "FIREWORKS_API_KEY"},
    }

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def get(self, provider: str, key: str) -> str | None:
        """Return config value for provider.key, env var takes priority."""
        env_key = self._ENV.get(provider, {}).get(key)
        if env_key:
            v = os.environ.get(env_key, "").strip()
            if v:
                return v
        return (self._raw.get(provider) or {}).get(key)

    def is_configured(self, provider: str) -> bool:
        """True when at least one key for this provider is non-empty."""
        provider_env = self._ENV.get(provider, {})
        for field_name, env_var in provider_env.items():
            if os.environ.get(env_var, "").strip():
                return True
            if ((self._raw.get(provider) or {}).get(field_name) or "").strip():
                return True
        if provider == "ollama":
            return bool(self.get("ollama", "base_url"))
        return False

    def configured_providers(self) -> list[str]:
        return [p for p in self._ENV if self.is_configured(p)]

    def export_env(self) -> None:
        """Push file-only values into os.environ (env vars always win)."""
        for provider, field_map in self._ENV.items():
            for field, env_var in field_map.items():
                if os.environ.get(env_var, "").strip():
                    continue
                v = (self._raw.get(provider) or {}).get(field, "")
                if v:
                    os.environ[env_var] = v


def load_providers_config(root: Path | str | None = None) -> ProviderConfig:
    path = providers_config_path(root)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return ProviderConfig(raw)
