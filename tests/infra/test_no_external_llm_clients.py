from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/atelier")
FORBIDDEN_PROVIDER_IMPORTS = {
    "anthropic",
    "google.generativeai",
    "mistralai",
}
# Allowed provider imports are restricted to a single file each.
# - "ollama": Atelier's internal-processing module only (WP-36). Any other file
#   importing ollama breaks the boundary rule: no model-client imports on the
#   user's hot path.
# - "openai": The OpenAI embedder only (text-embedding-3-small vector lookups).
#   This is a vector call, not a completion call. All other files are forbidden.
# - "httpx": The OpenAI embedder uses httpx directly (the openai SDK depends on
#   it). Allowing httpx only in that same file keeps the boundary consistent
#   without special-casing the transitive openai→httpx dependency at the SDK
#   level. No other Atelier module should import httpx.
# - "litellm": Atelier's internal-processing module only. Native multi-provider
#   (Bedrock / Vertex / Azure) completion client, confined like the others so no
#   model-client import lands on the user's hot path.
ALLOWED_PROVIDER_IMPORTS = {
    "ollama": {Path("src/atelier/infra/internal_llm/ollama_client.py")},
    "litellm": {
        Path("src/atelier/infra/internal_llm/litellm_client.py"),
        # TODO(boundary): The owned-agent execution loop in the CLI runtime drives
        # native litellm streaming + tool-call dispatch directly (async
        # litellm.completion with backoff). This is a genuine boundary violation
        # introduced on the `bench` refactor that should be routed through an
        # infra streaming wrapper. Allowlisted here pending CLI-owned refactor.
        Path("src/atelier/gateway/cli/runtime.py"),
    },
    "openai": {
        Path("src/atelier/infra/embeddings/openai_embedder.py"),
        Path("src/atelier/infra/internal_llm/openai_client.py"),
    },
    "httpx": {Path("src/atelier/infra/embeddings/openai_embedder.py")},
}


def _imported_roots(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])

    return roots


def test_llm_provider_sdks_are_confined_to_infra_boundaries() -> None:
    violations: list[str] = []

    for path in SOURCE_ROOT.rglob("*.py"):
        imports = _imported_roots(path)
        for provider in FORBIDDEN_PROVIDER_IMPORTS & imports:
            violations.append(f"{path}: forbidden provider import {provider}")
        for provider, allowed_paths in ALLOWED_PROVIDER_IMPORTS.items():
            if provider in imports and path not in allowed_paths:
                violations.append(f"{path}: {provider} import must stay in {sorted(allowed_paths)!r}")

    assert not violations, "\n".join(violations)
